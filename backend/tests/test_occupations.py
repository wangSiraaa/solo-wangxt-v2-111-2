"""虚拟批次占用验收（端到端，依赖已播种 PostgreSQL）。

覆盖验收①~④：
  ① 甲占 800t 后乙申请 300t 被拒（点名原料/缺口 300，无半条乙占用）；
  ② 甲原子替换为 500t 小方案：旧占用留痕 released，乙 300t 可确认；
  ③ 确认重试幂等不双扣；两笔并发申请仅一方成功，另一方得可恢复版本冲突；
  ④ 重启后过期占用被释放而原方案追溯不变；缺测化验与无解试算不占量。
"""
import threading
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import models
from app.database import SessionLocal
from app.main import app

c = TestClient(app)

# 宽率值窗口：VK01/VK02 单料方案可行（其 KH 约 0.92，SM 约 1.9）
WIDE_T = {"SM": {"min": 1.0, "max": 3.0},
          "IM": {"min": 1.0, "max": 3.0},
          "KH": {"min": 0.5, "max": 1.2}}

# seed.py：VK01=material 9（容量 800t 湿基，最低掺量 100%），
# VK02=material 10，SP01=material 7（缺测 Fe2O3）
VK01, VK02, SP01 = 9, 10, 7


def _spec(batch, mid=VK01):
    return {
        "scenario_name": "占用验收", "batch_t_dry": batch,
        "candidates": [{"material_id": mid}],
        "targets": WIDE_T, "mode": "min_cost",
    }


def _confirm(spec, key, **kw):
    body = {"spec": spec, "idempotency_key": key, "ttl_minutes": 60}
    body.update(kw)
    return c.post("/api/occupations/confirm", json=body)


def _cap(code):
    mats = {m["material_code"]: m for m in c.get("/api/capacity").json()["materials"]}
    return mats[code]


@pytest.fixture(autouse=True)
def _clean_occupations():
    """每个测试前后都清空占用相关表（保留原料/化验单/试算历史）。"""
    # 释放并删除全部占用，账本版本归零，保证测试互不干扰
    db = SessionLocal()
    try:
        db.query(models.OccupationEvent).delete()
        db.query(models.OccupationItem).delete()
        db.query(models.Occupation).delete()
        db.query(models.MaterialCapacityLedger).delete()
        db.commit()
    finally:
        db.close()
    yield
    db = SessionLocal()
    try:
        db.query(models.OccupationEvent).delete()
        db.query(models.OccupationItem).delete()
        db.query(models.Occupation).delete()
        db.query(models.MaterialCapacityLedger).delete()
        db.commit()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# ① 甲 800t 占满，乙 300t 被拒，点明原料与缺口，且无半条乙占用
# ---------------------------------------------------------------------------

def test_01_overbooking_rejected_with_named_gap():
    # 甲：800t（恰好用完 VK01 的 800t 湿基容量）
    r = _confirm(_spec(800), "acc-01-jia-aaaa")
    assert r.status_code == 200
    jia = r.json()["occupation"]
    assert jia["status"] == "occupied"
    item = jia["items"][0]
    assert item["material_code"] == "VK01"
    assert item["mass_t_wet"] == pytest.approx(800.0)
    assert item["remaining_t_wet_after"] == pytest.approx(0.0)
    # 逐原料快照保存干湿基换算与来源
    assert item["conversion_trace"]["mass_balance"]["mass_t_wet"]
    jia_id = jia["id"]

    # 乙：再申请 300t 被拒
    bad = _confirm(_spec(300), "acc-01-yi-bbbb")
    assert bad.status_code == 409
    body = bad.json()
    assert body["error_code"] == "CAPACITY_EXCEEDED"
    assert "VK01" in body["message"]
    gap = body["details"]["gaps"][0]
    assert gap["material_code"] == "VK01"
    assert gap["availability_t_wet"] == pytest.approx(800.0)
    assert gap["already_occupied_t_wet"] == pytest.approx(800.0)
    assert gap["remaining_t_wet"] == pytest.approx(0.0)
    assert gap["requested_t_wet"] == pytest.approx(300.0)
    assert gap["gap_t_wet"] == pytest.approx(300.0)

    # 没有半条乙占用：仍只有甲一条 occupied
    occs = c.get("/api/occupations", params={"status": "occupied"}).json()
    assert [o["id"] for o in occs] == [jia_id]
    # 乙的申请作为被拒事件留痕（冲突原因持久化）
    evs = c.get("/api/occupation-events").json()
    assert any(e["event_type"] == "rejected"
               and e["event_reason"] == "CAPACITY_EXCEEDED"
               and e["idempotency_key"] == "acc-01-yi-bbbb" for e in evs)


# ---------------------------------------------------------------------------
# ② 原子替换为较小可行方案：旧占用留痕 released，乙随后可确认
# ---------------------------------------------------------------------------

def test_02_atomic_replace_then_second_succeeds():
    # 甲占 800t；乙 300t 先被拒（复用①的情形）
    jia = _confirm(_spec(800), "acc-02-jia-aaaa")
    assert jia.status_code == 200
    jia_id = jia.json()["occupation"]["id"]
    bad = _confirm(_spec(300), "acc-02-yi-bbbb")
    assert bad.status_code == 409
    assert bad.json()["error_code"] == "CAPACITY_EXCEEDED"

    # 把甲替换为 500t 的较小可行方案（同事务验证后才释放旧量）
    r = _confirm(_spec(500), "acc-02-small-cc",
                 replace_occupation_id=jia_id)
    assert r.status_code == 200, r.text
    small_id = r.json()["occupation"]["id"]
    assert small_id != jia_id

    # 旧占用留痕为 released，且保留 confirmed + replaced 事件
    old = c.get(f"/api/occupations/{jia_id}").json()
    assert old["status"] == "released"
    kinds = {e["event_type"] for e in old["events"]}
    assert {"confirmed", "replaced"} <= kinds
    assert any(e["event_reason"] == "REPLACED_BY_NEW_OCCUPATION"
               and e["detail"]["replaced_by_occupation_id"] == small_id
               for e in old["events"])

    # 新占用记录指向旧占用；释放后容量恢复到 300t
    small = c.get(f"/api/occupations/{small_id}").json()
    assert small["replaces_occupation_id"] == jia_id
    assert _cap("VK01")["remaining_t_wet"] == pytest.approx(300.0)

    # 乙再申请 300t：成功
    ok = _confirm(_spec(300), "acc-02-yi-dd")
    assert ok.status_code == 200, ok.text
    yi = ok.json()["occupation"]
    assert yi["status"] == "occupied"
    assert _cap("VK01")["occupied_t_wet"] == pytest.approx(800.0)
    assert _cap("VK01")["remaining_t_wet"] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# ③ 幂等重试不双扣；并发仅一方成功，另一方得到可恢复版本冲突
# ---------------------------------------------------------------------------

def test_03_idempotent_retry_does_not_double_charge():
    r1 = _confirm(_spec(100), "acc-03-idem-ee")
    assert r1.status_code == 200
    assert r1.json()["replay"] is False
    id1 = r1.json()["occupation"]["id"]
    occupied1 = _cap("VK01")["occupied_t_wet"]

    r2 = _confirm(_spec(100), "acc-03-idem-ee")  # 同 key 重试
    assert r2.status_code == 200
    assert r2.json()["replay"] is True
    assert r2.json()["occupation"]["id"] == id1
    # 占用量没有第二次扣减
    assert _cap("VK01")["occupied_t_wet"] == pytest.approx(occupied1)


def test_03_concurrent_confirms_only_one_wins():
    # 释放前序占用，腾出干净容量
    for oid in _cap("VK01")["active_occupation_ids"]:
        c.post(f"/api/occupations/{oid}/release", json={})
    assert _cap("VK01")["remaining_t_wet"] == pytest.approx(800.0)

    barrier = threading.Barrier(2)
    results = {}

    def worker(key):
        local = TestClient(app)
        barrier.wait()
        r = local.post("/api/occupations/confirm",
                       json={"spec": _spec(600), "idempotency_key": key,
                             "ttl_minutes": 60})
        results[key] = (r.status_code, r.json())

    threads = [threading.Thread(target=worker, args=(k,))
               for k in ("acc-03-race-f", "acc-03-race-g")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    statuses = [v[0] for v in results.values()]
    assert sorted(statuses) == [200, 409]
    loser = next(v[1] for v in results.values() if v[0] == 409)
    assert loser["error_code"] == "VERSION_CONFLICT"
    # 可恢复信息：当前版本 + 容量缺口
    assert loser["details"]["conflicts"][0]["material_code"] == "VK01"
    assert loser["details"]["conflicts"][0]["current_version"] >= 2
    gap = loser["details"]["gaps"][0]
    assert gap["gap_t_wet"] == pytest.approx(400.0)

    # 输方用返回的新版本 + 缩小到 200t 重试：成功（可恢复）
    cur_v = loser["details"]["current_versions"]["VK01"]
    rec = _confirm(_spec(200), "acc-03-race-recover",
                   expected_versions={"VK01": cur_v})
    assert rec.status_code == 200, rec.text

    # 用过期版本号再申请：必被版本冲突拦下
    stale = _confirm(_spec(10), "acc-03-stale-h",
                     expected_versions={"VK01": 1})
    assert stale.status_code == 409
    assert stale.json()["error_code"] == "VERSION_CONFLICT"


# ---------------------------------------------------------------------------
# ④ 重启/刷新后过期占用被释放，原方案追溯不变；缺测/无解不占量
# ---------------------------------------------------------------------------

def test_04_expired_occupation_swept_but_trace_kept():
    for oid in _cap("VK01")["active_occupation_ids"]:
        c.post(f"/api/occupations/{oid}/release", json={})
    r = _confirm(_spec(300), "acc-04-exp-ii", ttl_minutes=60)
    oid = r.json()["occupation"]["id"]

    # 模拟服务重启：TTL 已过，容量查询（与启动扫描同一路径）释放过期占用
    db = SessionLocal()
    occ = db.get(models.Occupation, oid)
    occ.expires_at = datetime.utcnow() - timedelta(minutes=1)
    db.commit()
    db.close()

    resp = c.get("/api/capacity").json()
    assert oid in resp["expired_released"]
    vk = next(m for m in resp["materials"] if m["material_code"] == "VK01")
    assert vk["occupied_t_wet"] == pytest.approx(0.0)
    assert vk["remaining_t_wet"] == pytest.approx(800.0)

    # 原占用与来源方案追溯不变（明细/快照/事件都保留）
    detail = c.get(f"/api/occupations/{oid}").json()
    assert detail["status"] == "expired"
    assert len(detail["items"]) == 1
    assert detail["items"][0]["mass_t_wet"] == pytest.approx(300.0)
    assert detail["solution"]["indicators"]["SM"]  # 方案快照未被改写
    assert {e["event_type"] for e in detail["events"]} >= {"confirmed", "expired"}


def test_04_missing_assay_never_occupies():
    before = {o["id"] for o in
              c.get("/api/occupations", params={"status": "occupied"}).json()}
    r = _confirm(
        {"scenario_name": "缺测", "batch_t_dry": 100,
         "candidates": [{"material_id": SP01}],
         "targets": WIDE_T, "mode": "min_cost"},
        "acc-04-missing-jj",
    )
    assert r.status_code == 422
    assert r.json()["error_code"] == "MISSING_ASSAY"

    after = {o["id"] for o in
             c.get("/api/occupations", params={"status": "occupied"}).json()}
    assert before == after  # 没有产生任何占用
    evs = c.get("/api/occupation-events").json()
    assert any(e["event_type"] == "rejected"
               and e["event_reason"] == "MISSING_ASSAY" for e in evs)


def test_04_infeasible_never_occupies():
    before = {o["id"] for o in
              c.get("/api/occupations", params={"status": "occupied"}).json()}
    impossible = {"SM": {"min": 9.0, "max": 10.0},
                  "IM": {"min": 1.0, "max": 3.0},
                  "KH": {"min": 0.5, "max": 1.2}}
    r = _confirm(
        {"scenario_name": "无解", "batch_t_dry": 100,
         "candidates": [{"material_id": VK01}],
         "targets": impossible, "mode": "min_cost"},
        "acc-04-infeas-kk",
    )
    assert r.status_code == 422
    assert r.json()["error_code"] == "NOT_FEASIBLE"
    after = {o["id"] for o in
             c.get("/api/occupations", params={"status": "occupied"}).json()}
    assert before == after  # 无解试算不占量


# ---------------------------------------------------------------------------
# 预览不落库、不占量
# ---------------------------------------------------------------------------

def test_preview_does_not_occupy():
    for oid in _cap("VK01")["active_occupation_ids"]:
        c.post(f"/api/occupations/{oid}/release", json={})
    rem0 = _cap("VK01")["remaining_t_wet"]
    r = c.post("/api/occupations/preview", json={"spec": _spec(400)})
    assert r.status_code == 200
    pv = r.json()
    assert pv["fits"] is True
    assert pv["items"][0]["mass_t_wet"] == pytest.approx(400.0)
    assert pv["current_versions"]["VK01"] >= 1
    # 预览后容量不变
    assert _cap("VK01")["remaining_t_wet"] == pytest.approx(rem0)
    assert c.get("/api/occupations", params={"status": "occupied"}).json() == []


def test_preview_reports_gap_without_persistence():
    _confirm(_spec(800), "acc-preview-full")
    r = c.post("/api/occupations/preview", json={"spec": _spec(300)})
    assert r.status_code == 200
    pv = r.json()
    assert pv["fits"] is False
    assert pv["gaps"][0]["gap_t_wet"] == pytest.approx(300.0)


# ---------------------------------------------------------------------------
# 替换必须整体成立：替换失败不得先释放旧占用
# ---------------------------------------------------------------------------

def test_failed_replace_leaves_old_occupation_intact():
    r = _confirm(_spec(800), "acc-repl-base")
    base_id = r.json()["occupation"]["id"]
    # 替换为一个超容量方案（VK01 总共 800，900 不可能）——应被拒
    bad = _confirm(_spec(900), "acc-repl-fail",
                   replace_occupation_id=base_id)
    assert bad.status_code in (409, 422)
    detail = c.get(f"/api/occupations/{base_id}").json()
    # 旧占用仍然 occupied，没有被先释放
    assert detail["status"] == "occupied"
    assert _cap("VK01")["occupied_t_wet"] == pytest.approx(800.0)


def test_release_is_idempotent_and_version_checked():
    r = _confirm(_spec(100), "acc-rel-base")
    rid = r.json()["occupation"]["id"]
    a = c.post(f"/api/occupations/{rid}/release", json={})
    assert a.status_code == 200 and a.json()["replay"] is False
    b = c.post(f"/api/occupations/{rid}/release", json={})
    assert b.status_code == 200 and b.json()["replay"] is True  # 重复释放回放
