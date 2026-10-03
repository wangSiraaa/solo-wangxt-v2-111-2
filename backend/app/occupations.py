"""虚拟批次占用服务：跨批次湿基原料超订防护。

不变式（任何时刻、任何事务结果下都成立）：
  对每种原料 m：所有 status=occupied 的占用湿料量之和 ≤ 湿基可用量（NULL 视为不限）。

实现要点：
- 确认/替换在同一事务内先锁原料行（SELECT ... FOR UPDATE，按 id 排序，
  防死锁）再汇总有效占用，使并发确认串行化，后到者看到先到者已提交的占用；
- 替换必须“先验证新方案 + 全部占用成立，再在同一提交内以新占旧”，
  绝不在验证前释放旧量（不暴露并发超订窗口）；
- 容量不足/版本冲突/缺测/无解一律在写入前拒绝并持久化原因事件，
  失败路径不留下任何部分占用；
- occupied 占用带 expires_at，过期清扫（启动/查询/确认前）把其转为
  expired 并释放容量，但关联试算（run/solution）与事件留痕不变。
"""
import uuid
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import crud, models, optimizer
from .chemistry import BlendError

CAP_EPS = 1e-6  # 湿基吨比较容差


class OccupationError(BlendError):
    """占用业务异常；http_status 供 API 层映射（409 居多）。"""

    def __init__(self, code: str, message: str, details: dict | None = None,
                 http_status: int = 409):
        super().__init__(code, message, details or {})
        self.http_status = http_status


# ---------- 求解：把占用请求还原成单次 LP 试算，取指定模式方案 ----------

def _to_blend_request(preq):
    from .schemas import BlendRequest

    return BlendRequest(
        scenario_name=preq.scenario_name,
        batch_t_dry=preq.batch_t_dry,
        candidates=preq.candidates,
        targets=preq.targets,
        hazard_limits_pct=preq.hazard_limits_pct,
        modes=[preq.mode],
        cheap_material_id=preq.cheap_material_id,
        save=False,
    )


def solve_plan(db: Session, preq):
    """返回 (rows, blend_request, solution_dict)。

    缺测等硬性错误直接抛 MissingAssayError（调用方负责落事件后转交 422）。
    """
    pairs = crud.resolve_candidates(db, preq.candidates)
    rows = optimizer.prepare_rows(pairs)
    if not rows:
        raise BlendError("EMPTY_CANDIDATES", "候选原料为空。")
    req = _to_blend_request(preq)
    solutions = optimizer.solve(rows, req)
    sol = next((s for s in solutions if s["mode"] == preq.mode), None)
    if sol is None:
        raise BlendError("MODE_NOT_SOLVED",
                         f"试算结果中不存在模式 {preq.mode} 的方案。",
                         {"mode": preq.mode})
    return rows, req, sol


# ---------- 事件 ----------

def _log(db, occ, event_type: str, detail: dict | None = None,
         idem: str | None = None, occ_code: str | None = None):
    ev = models.OccupationEvent(
        occupation_id=occ.id if occ is not None else None,
        occ_code=occ.occ_code if occ is not None else occ_code,
        event_type=event_type,
        detail=detail or {},
        idempotency_key=idem,
    )
    db.add(ev)
    return ev


def _new_code() -> str:
    return f"OCC-{uuid.uuid4().hex[:10].upper()}"


# ---------- 逐原料占用行构建 ----------

def _item_kwargs(rows_by_id: dict, it: dict) -> dict:
    r = rows_by_id[it["_material_id"]]
    trace = it["conversion_trace"]
    return dict(
        material_id=r.material_id,
        assay_version_id=it["_assay_version_id"],
        material_code=r.code,
        material_name=r.name,
        share_pct_dry=it["share_pct_dry"],
        mass_t_dry=it["mass_t_dry"],
        mass_t_wet=it["mass_t_wet"],
        water_t=it["water_t"],
        cost=it["cost"],
        moisture_pct=r.moisture_pct,
        dry_factor=trace["dry_factor"],
        conversion_trace=trace,
        assay_composition_snapshot=trace["steps"],
        occupied_before_t_wet=0.0,
    )


def _new_draft(preq, sol: dict) -> models.VirtualBatchOccupation:
    return models.VirtualBatchOccupation(
        occ_code=_new_code(),
        scenario_name=preq.scenario_name,
        status=models.OCC_DRAFT,
        version=1,
        idempotency_key=preq.idempotency_key,
        request_kind="confirm",
        batch_t_dry=preq.batch_t_dry,
        mode=preq.mode,
        total_cost=sol.get("total_cost"),
        request_snapshot=preq.model_dump(mode="json"),
        result_snapshot={
            "indicators": sol.get("indicators"),
            "composition_dry_pct": sol.get("composition_dry_pct"),
            "composition_wet_pct": sol.get("composition_wet_pct"),
            "water_pct_in_wet_mix": sol.get("water_pct_in_wet_mix"),
            "diagnostic": sol.get("diagnostic"),
            "items": [{k: v for k, v in it.items() if not k.startswith("_")}
                      for it in sol.get("items", [])],
        },
    )


# ---------- 过期 ----------

def sweep_expired(db: Session, now: datetime | None = None,
                  commit: bool = True) -> list[models.VirtualBatchOccupation]:
    """把已过 expires_at 的 occupied 占用原子转为 expired（释放容量）。"""
    now = now or datetime.utcnow()
    rows = list(db.scalars(
        select(models.VirtualBatchOccupation)
        .where(models.VirtualBatchOccupation.status == models.OCC_OCCUPIED)
        .where(models.VirtualBatchOccupation.expires_at.is_not(None))
        .where(models.VirtualBatchOccupation.expires_at <= now)
        .order_by(models.VirtualBatchOccupation.id)
        .with_for_update()
    ))
    for occ in rows:
        occ.status = models.OCC_EXPIRED
        occ.released_at = now
        occ.version += 1
        _log(db, occ, "expired", {
            "expires_at": occ.expires_at.isoformat(timespec="seconds"),
            "swept_at": now.isoformat(timespec="seconds"),
        })
    if rows and commit:
        db.commit()
    return rows


# ---------- 容量 ----------

def _lock_materials(db: Session, material_ids: list[int]):
    """按 id 排序锁原料行——并发确认在同一集合上串行化，杜绝超订。"""
    if not material_ids:
        return []
    return list(db.scalars(
        select(models.Material)
        .where(models.Material.id.in_(sorted(set(material_ids))))
        .order_by(models.Material.id)
        .with_for_update()
    ))


def _effective_items(db: Session, material_ids: list[int],
                     exclude_occ_id: int | None = None
                     ) -> list[models.OccupationItem]:
    stmt = (
        select(models.OccupationItem)
        .join(models.VirtualBatchOccupation,
              models.VirtualBatchOccupation.id
              == models.OccupationItem.occupation_id)
        .where(models.VirtualBatchOccupation.status == models.OCC_OCCUPIED)
        .where(models.OccupationItem.material_id.in_(material_ids))
    )
    if exclude_occ_id is not None:
        stmt = stmt.where(models.OccupationItem.occupation_id != exclude_occ_id)
    return list(db.scalars(stmt))


def _demand_by_material(draft_items) -> dict[int, float]:
    """待生效占用（未提交的 OccupationItem 列表或可迭代 kwargs/对象）的湿料需求。"""
    demand: dict[int, float] = {}
    for it in draft_items:
        mid = it.material_id if hasattr(it, "material_id") else it["material_id"]
        wet = it.mass_t_wet if hasattr(it, "mass_t_wet") else it["mass_t_wet"]
        demand[mid] = demand.get(mid, 0.0) + wet
    return demand


def _shortages(mat_avail: dict[int, float | None],
               occupied: dict[int, float],
               demand: dict[int, float]) -> list[dict]:
    out = []
    for mid, requested in demand.items():
        avail = mat_avail.get(mid)
        used = occupied.get(mid, 0.0)
        if avail is None:
            continue  # 湿基可用量 NULL = 不限
        gap = used + requested - avail
        if gap > CAP_EPS:
            out.append({
                "material_id": mid,
                "requested_t_wet": round(requested, 4),
                "availability_t_wet": round(avail, 4),
                "occupied_t_wet": round(used, 4),
                "remaining_t_wet": round(max(avail - used, 0.0), 4),
                "shortage_t_wet": round(gap, 4),
            })
    return out


def _annotate_material_names(db: Session, rows: list[dict]):
    for row in rows:
        m = db.get(models.Material, row["material_id"])
        row["material_code"] = m.code
        row["material_name"] = m.name
    rows.sort(key=lambda r: r["material_id"])
    return rows


def _check_and_snapshot(db: Session, occ: models.VirtualBatchOccupation,
                        exclude_occ_id: int | None = None):
    """锁原料行 → 汇总有效占用 → 校验新申请；通过则写逐原料容量快照。

    返回 None 表示通过；否则返回带缺口信息的 shortages 列表（不抛异常，
    由调用方落拒绝事件后再转成 OccupationError）。
    """
    mids = [it.material_id for it in occ.items]
    mats = _lock_materials(db, mids)
    mat_by_id = {m.id: m for m in mats}
    eff = _effective_items(db, mids, exclude_occ_id=exclude_occ_id)
    occupied: dict[int, float] = {}
    contributors: dict[int, list[dict]] = {}
    for it in eff:
        occupied[it.material_id] = occupied.get(it.material_id, 0.0) + it.mass_t_wet
        contributors.setdefault(it.material_id, []).append({
            "occupation_id": it.occupation_id,
            "mass_t_wet": it.mass_t_wet,
        })

    demand = _demand_by_material(occ.items)
    avail = {mid: mat_by_id[mid].availability_t_wet for mid in mids}
    shortages = _shortages(avail, occupied, demand)
    if shortages:
        return _annotate_material_names(db, shortages)

    # 通过：逐原料写确认时刻快照
    per_mat_before = dict(occupied)
    per_mat_running = dict(occupied)
    for it in occ.items:
        m = mat_by_id[it.material_id]
        before = per_mat_running.get(it.material_id, 0.0)
        after = before + it.mass_t_wet
        it.available_t_wet_snapshot = m.availability_t_wet
        it.occupied_before_t_wet = round(per_mat_before.get(it.material_id, 0.0), 4)
        it.remaining_after_t_wet_snapshot = (
            None if m.availability_t_wet is None
            else round(m.availability_t_wet - after, 4)
        )
        per_mat_running[it.material_id] = after
    return None


# ---------- 预览（建草稿，不占量） ----------

def preview(db: Session, preq, now: datetime | None = None) -> dict:
    now = now or datetime.utcnow()
    rows, req, sol = solve_plan(db, preq)
    rows_by_id = {r.material_id: r for r in rows}

    if not sol["success"]:
        # 无解：落拒绝事件，绝不建草稿/不占量
        _log(db, None, "infeasible_rejected", {
            "scenario_name": preq.scenario_name,
            "mode": preq.mode,
            "diagnostic": sol.get("diagnostic"),
        }, occ_code=None)
        db.commit()
        return {
            "occupation_id": None, "occ_code": None, "status": "rejected",
            "feasible": False, "mode": preq.mode,
            "total_cost": None, "items": [], "capacity": [],
            "indicators": None, "diagnostic": sol.get("diagnostic"),
            "ttl_seconds": None,
        }

    draft = _new_draft(preq, sol)
    db.add(draft)
    db.flush()
    for it in sol["items"]:
        db.add(models.OccupationItem(occupation_id=draft.id,
                                     **_item_kwargs(rows_by_id, it)))
    db.flush()
    _log(db, draft, "created", {
        "ttl_seconds": preq.ttl_seconds,
        "items": [
            {"material_code": it.material_code, "mass_t_wet": it.mass_t_wet}
            for it in draft.items
        ],
    }, idem=preq.idempotency_key)
    db.commit()
    db.refresh(draft)
    return _preview_payload(db, draft, preq.ttl_seconds, sol)


def _prospective_capacity(db: Session,
                          draft: models.VirtualBatchOccupation) -> list[dict]:
    """若该草稿即刻确认，逐原料（仅草稿涉及原料 + 全部原料概览由调用方决定）。"""
    mids = sorted({it.material_id for it in draft.items})
    eff = _effective_items(db, mids)
    occupied: dict[int, float] = {}
    contrib: dict[int, list] = {}
    for it in eff:
        occupied[it.material_id] = occupied.get(it.material_id, 0.0) + it.mass_t_wet
        o = db.get(models.VirtualBatchOccupation, it.occupation_id)
        contrib.setdefault(it.material_id, []).append({
            "occupation_id": o.id, "occ_code": o.occ_code,
            "scenario_name": o.scenario_name, "mass_t_wet": round(it.mass_t_wet, 4),
            "occupied_at": o.occupied_at.isoformat(timespec="seconds")
            if o.occupied_at else None,
        })
    out = []
    for mid in mids:
        m = db.get(models.Material, mid)
        used = round(occupied.get(mid, 0.0), 4)
        req = round(sum(i.mass_t_wet for i in draft.items if i.material_id == mid), 4)
        avail = m.availability_t_wet
        out.append({
            "material_id": mid, "material_code": m.code, "material_name": m.name,
            "availability_t_wet": avail,
            "occupied_t_wet": used,
            "requested_t_wet": req,
            "remaining_t_wet": (None if avail is None
                                else round(max(avail - used - req, 0.0), 4)),
            "unlimited": avail is None,
            "would_fit": avail is None or used + req <= avail + CAP_EPS,
            "effective_occupations": contrib.get(mid, []),
        })
    return out


def _preview_payload(db, draft, ttl_seconds, sol=None) -> dict:
    return {
        "occupation_id": draft.id,
        "occ_code": draft.occ_code,
        "status": draft.status,
        "feasible": True,
        "mode": draft.mode,
        "total_cost": draft.total_cost,
        "items": [_item_out(db, it) for it in draft.items],
        "capacity": _prospective_capacity(db, draft),
        "indicators": draft.result_snapshot.get("indicators"),
        "diagnostic": None,
        "ttl_seconds": ttl_seconds,
        "version": draft.version,
    }


# ---------- 持久化来源方案（不提交，供确认/替换同事务使用） ----------

def _persist_run(db, breq, sol) -> tuple[int, int]:
    run = crud.save_run(db, breq, [sol], commit=False)
    srec = db.scalars(
        select(models.BlendSolution).where(models.BlendSolution.run_id == run.id)
    ).one()
    return run.id, srec.id


# ---------- 确认草稿 ----------

def confirm(db: Session, occ_id: int, expected_version: int,
            idem: str | None = None, note: str | None = None,
            now: datetime | None = None) -> models.VirtualBatchOccupation:
    now = now or datetime.utcnow()
    sweep_expired(db, now=now, commit=False)

    occ = _locked_occ(db, occ_id)
    if occ is None:
        raise OccupationError("OCC_NOT_FOUND",
                              f"占用 id={occ_id} 不存在。",
                              {"occupation_id": occ_id}, http_status=404)

    # 已占用：幂等重试（同一确认请求）→ 原样返回，绝不双扣
    if occ.status == models.OCC_OCCUPIED:
        if idem and occ.idempotency_key == idem:
            db.commit()  # 仅提交可能发生的过期清扫
            db.refresh(occ)
            return occ
        _log(db, occ, "confirm_rejected", {
            "reason": "VERSION_CONFLICT", "detail": "重复/并发确认已占用方案",
            "expected_version": expected_version,
            "current_version": occ.version,
        }, idem=idem)
        db.commit()
        raise OccupationError(
            "VERSION_CONFLICT",
            f"占用 {occ.occ_code} 已确认占用（当前版本 {occ.version}），"
            "重复或并发确认被拒绝；请刷新后基于最新版本重试。",
            {"occupation_id": occ.id, "expected_version": expected_version,
               "current_version": occ.version, "status": occ.status,
               "recoverable": True},
        )
    if occ.status in models.OCC_TERMINAL_STATES:
        raise OccupationError(
            "BAD_OCCUPATION_STATE",
            f"占用 {occ.occ_code} 已处于 {occ.status} 终态，不能确认。",
            {"occupation_id": occ.id, "status": occ.status,
             "current_version": occ.version},
            http_status=422,
        )
    if occ.version != expected_version:
        _log(db, occ, "confirm_rejected", {
            "reason": "VERSION_CONFLICT",
            "expected_version": expected_version,
            "current_version": occ.version,
        }, idem=idem)
        db.commit()
        raise OccupationError(
            "VERSION_CONFLICT",
            f"版本冲突：草稿 {occ.occ_code} 当前版本为 {occ.version}，"
            f"请求基于 {expected_version}；请刷新容量与版本后重试（可恢复）。",
            {"occupation_id": occ.id, "expected_version": expected_version,
             "current_version": occ.version, "status": occ.status,
             "recoverable": True},
        )

    shortages = _check_and_snapshot(db, occ)
    if shortages:
        _log(db, occ, "confirm_rejected", {
            "reason": "CAPACITY_CONFLICT", "shortages": shortages,
        }, idem=idem)
        db.commit()  # 拒绝事件与快照尝试前无任何占用写入，可直接提交
        raise OccupationError(
            "CAPACITY_CONFLICT",
            _capacity_message(shortages),
            {"shortages": shortages, "recoverable": True},
        )

    # 全部成立 → 落来源方案、翻转状态（同一提交）
    preq = _draft_request(occ)
    rows, breq, sol = solve_plan(db, preq)
    run_id, sol_id = _persist_run(db, breq, sol)
    occ.run_id, occ.solution_id = run_id, sol_id
    occ.status = models.OCC_OCCUPIED
    occ.occupied_at = now
    occ.expires_at = now + timedelta(seconds=preq.ttl_seconds)
    occ.version += 1
    if idem:
        occ.idempotency_key = idem
    _log(db, occ, "confirmed", {
        "run_id": run_id, "solution_id": sol_id,
        "ttl_seconds": preq.ttl_seconds,
        "expires_at": occ.expires_at.isoformat(timespec="seconds"),
        "note": note,
        "items": [
            {"material_code": it.material_code, "mass_t_wet": it.mass_t_wet,
             "remaining_after_t_wet_snapshot": it.remaining_after_t_wet_snapshot}
            for it in occ.items
        ],
    }, idem=idem)
    db.commit()
    db.refresh(occ)
    return occ


def _locked_occ(db, occ_id) -> models.VirtualBatchOccupation | None:
    return db.scalars(
        select(models.VirtualBatchOccupation)
        .where(models.VirtualBatchOccupation.id == occ_id)
        .with_for_update()
    ).first()


def _draft_request(occ: models.VirtualBatchOccupation):
    """从草稿快照重建 Pydantic 请求（确认时据此重算并持久化来源方案）。"""
    from .schemas import OccupationPreviewRequest

    snap = dict(occ.request_snapshot)
    snap.pop("idempotency_key", None)
    return OccupationPreviewRequest(**snap)


# ---------- 释放（含草稿撤销） ----------

def release(db: Session, occ_id: int, expected_version: int,
            idem: str | None = None, note: str | None = None,
            now: datetime | None = None) -> models.VirtualBatchOccupation:
    now = now or datetime.utcnow()
    sweep_expired(db, now=now, commit=False)
    occ = _locked_occ(db, occ_id)
    if occ is None:
        raise OccupationError("OCC_NOT_FOUND",
                              f"占用 id={occ_id} 不存在。",
                              {"occupation_id": occ_id}, http_status=404)

    if occ.status in models.OCC_TERMINAL_STATES:
        # 同一释放请求重试 → 幂等返回；否则版本冲突可恢复
        if idem and any(
            ev.event_type in ("released", "expired", "replaced")
            and ev.idempotency_key == idem
            for ev in occ.events
        ):
            db.commit()
            db.refresh(occ)
            return occ
        raise OccupationError(
            "VERSION_CONFLICT",
            f"占用 {occ.occ_code} 已为终态 {occ.status}（版本 {occ.version}）。",
            {"occupation_id": occ.id, "current_version": occ.version,
             "status": occ.status, "recoverable": True},
        )
    if occ.version != expected_version:
        _log(db, occ, "release_rejected", {
            "reason": "VERSION_CONFLICT",
            "expected_version": expected_version,
            "current_version": occ.version,
        }, idem=idem)
        db.commit()
        raise OccupationError(
            "VERSION_CONFLICT",
            f"版本冲突：{occ.occ_code} 当前版本为 {occ.version}，请求基于 "
            f"{expected_version}；请刷新后重试（可恢复）。",
            {"occupation_id": occ.id, "expected_version": expected_version,
             "current_version": occ.version, "status": occ.status,
             "recoverable": True},
        )

    from_draft = occ.status == models.OCC_DRAFT
    occ.status = models.OCC_RELEASED
    occ.released_at = now
    occ.version += 1
    occ.replace_reason = note
    _log(db, occ, "released", {
        "from_status": models.OCC_DRAFT if from_draft else models.OCC_OCCUPIED,
        "note": note,
        "items": [
            {"material_code": it.material_code, "mass_t_wet": it.mass_t_wet}
            for it in occ.items
        ],
    }, idem=idem)
    db.commit()
    db.refresh(occ)
    return occ


# ---------- 原子替换 ----------

def replace(db: Session, old_id: int, preq,
            now: datetime | None = None) -> tuple[
                models.VirtualBatchOccupation, models.VirtualBatchOccupation]:
    """以新方案原子替换已占用方案。

    顺序：锁旧占用 → 求解新方案（缺测外抛）→ 锁原料行并验证
    「新需求 + 除旧占用外全部有效占用」→ 全部成立后才在同一事务内
    翻转旧占用、建立新占用。任何一步失败旧占用保持 occupied、不留半成品。
    """
    now = now or datetime.utcnow()
    sweep_expired(db, now=now, commit=False)

    old = _locked_occ(db, old_id)
    if old is None:
        raise OccupationError("OCC_NOT_FOUND",
                              f"被替换占用 id={old_id} 不存在。",
                              {"occupation_id": old_id}, http_status=404)
    if old.status != models.OCC_OCCUPIED:
        raise OccupationError(
            "BAD_OCCUPATION_STATE",
            f"占用 {old.occ_code} 当前状态为 {old.status}，只有已占用方案可替换。",
            {"occupation_id": old.id, "status": old.status,
             "current_version": old.version},
            http_status=422,
        )
    if old.version != preq.expected_version:
        _log(db, old, "replace_rejected", {
            "reason": "VERSION_CONFLICT",
            "expected_version": preq.expected_version,
            "current_version": old.version,
        })
        db.commit()
        raise OccupationError(
            "VERSION_CONFLICT",
            f"版本冲突：{old.occ_code} 当前版本为 {old.version}，替换请求基于 "
            f"{preq.expected_version}；请刷新后重试（可恢复）。",
            {"occupation_id": old.id,
             "expected_version": preq.expected_version,
             "current_version": old.version, "status": old.status,
             "recoverable": True},
        )

    # 求解新方案（缺测 MissingAssayError 直接外抛，由路由落事件）
    rows, breq, sol = solve_plan(db, preq)
    rows_by_id = {r.material_id: r for r in rows}
    if not sol["success"]:
        _log(db, old, "replace_rejected", {
            "reason": "SOLUTION_INFEASIBLE",
            "scenario_name": preq.scenario_name, "mode": preq.mode,
            "diagnostic": sol.get("diagnostic"),
        })
        db.commit()
        raise OccupationError(
            "SOLUTION_INFEASIBLE",
            "新方案无可行解，替换被拒绝；旧占用保持不变。",
            {"diagnostic": sol.get("diagnostic"), "old_occ_code": old.occ_code},
            http_status=422,
        )

    # 先在内存构建新占用（不写库），用它做容量校验
    new_occ = _new_draft(preq, sol)
    new_items = [models.OccupationItem(**_item_kwargs(rows_by_id, it))
                 for it in sol["items"]]
    new_occ.items = new_items

    # 关键：校验时排除旧占用（旧量将在同一事务内被新量替代），
    # 但绝不提前释放/提交旧占用
    mids = sorted({it.material_id for it in new_items}
                  | {it.material_id for it in old.items})
    _lock_materials(db, mids)
    eff = _effective_items(db, mids, exclude_occ_id=old.id)
    occupied: dict[int, float] = {}
    for it in eff:
        occupied[it.material_id] = occupied.get(it.material_id, 0.0) + it.mass_t_wet
    mats = {m.id: m for m in db.scalars(
        select(models.Material).where(models.Material.id.in_(mids)))}
    demand = _demand_by_material(new_items)
    avail = {mid: mats[mid].availability_t_wet for mid in mids}
    shortages = _annotate_material_names(db, _shortages(avail, occupied, demand))
    if shortages:
        _log(db, old, "replace_rejected", {
            "reason": "CAPACITY_CONFLICT",
            "scenario_name": preq.scenario_name, "shortages": shortages,
        })
        db.commit()
        raise OccupationError(
            "CAPACITY_CONFLICT",
            _capacity_message(shortages) + "（旧占用保持不变。）",
            {"shortages": shortages, "old_occ_code": old.occ_code},
        )

    # ---- 全部成立：同一事务内“以新占旧” ----
    run_id, sol_id = _persist_run(db, breq, sol)
    new_occ.run_id, new_occ.solution_id = run_id, sol_id
    new_occ.status = models.OCC_OCCUPIED
    new_occ.occupied_at = now
    new_occ.expires_at = now + timedelta(seconds=preq.ttl_seconds)
    new_occ.request_kind = "replace"
    new_occ.version = 2  # created(1)→confirmed 等价跃迁
    db.add(new_occ)
    db.flush()

    # 新占用逐原料快照
    per_mat_before = dict(occupied)
    per_mat_running = dict(occupied)
    for it in new_occ.items:
        m = mats[it.material_id]
        before = per_mat_running.get(it.material_id, 0.0)
        after = before + it.mass_t_wet
        it.occupation_id = new_occ.id
        it.occupied_before_t_wet = round(per_mat_before.get(it.material_id, 0.0), 4)
        it.available_t_wet_snapshot = m.availability_t_wet
        it.remaining_after_t_wet_snapshot = (
            None if m.availability_t_wet is None
            else round(m.availability_t_wet - after, 4)
        )
        per_mat_running[it.material_id] = after
        db.add(it)

    old.status = models.OCC_RELEASED
    old.released_at = now
    old.replace_reason = (
        f"被 {new_occ.occ_code} 原子替换：{preq.replace_note or preq.scenario_name}"
    )
    old.version += 1
    _log(db, old, "replaced", {
        "replaced_by": new_occ.occ_code,
        "new_occupation_id": new_occ.id,
        "new_run_id": run_id, "note": preq.replace_note,
        "items": [
            {"material_code": it.material_code, "mass_t_wet": it.mass_t_wet}
            for it in old.items
        ],
    })
    _log(db, new_occ, "created", {"via": "replace", "replaces": old.occ_code,
                                  "ttl_seconds": preq.ttl_seconds},
         idem=preq.idempotency_key)
    _log(db, new_occ, "confirmed", {
        "via": "replace", "replaces": old.occ_code,
        "run_id": run_id, "solution_id": sol_id,
        "expires_at": new_occ.expires_at.isoformat(timespec="seconds"),
        "items": [
            {"material_code": it.material_code, "mass_t_wet": it.mass_t_wet,
             "remaining_after_t_wet_snapshot": it.remaining_after_t_wet_snapshot}
            for it in new_occ.items
        ],
    })
    db.commit()
    db.refresh(old)
    db.refresh(new_occ)
    return old, new_occ


# ---------- 查询 ----------

def capacity(db: Session, now: datetime | None = None) -> dict:
    now = now or datetime.utcnow()
    swept = sweep_expired(db, now=now)
    mats = list(db.scalars(
        select(models.Material).order_by(models.Material.id)))
    eff = _effective_items(db, [m.id for m in mats])
    occ_by_mat: dict[int, list[models.OccupationItem]] = {}
    for it in eff:
        occ_by_mat.setdefault(it.material_id, []).append(it)
    rows = []
    for m in mats:
        items = occ_by_mat.get(m.id, [])
        used = round(sum(i.mass_t_wet for i in items), 4)
        detail = []
        for it in items:
            o = db.get(models.VirtualBatchOccupation, it.occupation_id)
            detail.append({
                "occupation_id": o.id, "occ_code": o.occ_code,
                "scenario_name": o.scenario_name,
                "mass_t_wet": round(it.mass_t_wet, 4),
                "occupied_at": o.occupied_at.isoformat(timespec="seconds")
                if o.occupied_at else None,
                "expires_at": o.expires_at.isoformat(timespec="seconds")
                if o.expires_at else None,
            })
        rows.append({
            "material_id": m.id, "material_code": m.code, "material_name": m.name,
            "availability_t_wet": m.availability_t_wet,
            "occupied_t_wet": used,
            "remaining_t_wet": (None if m.availability_t_wet is None
                                else round(m.availability_t_wet - used, 4)),
            "unlimited": m.availability_t_wet is None,
            "effective_occupations": detail,
        })
    return {"swept_expired": len(swept), "materials": rows}


def list_occupations(db: Session, limit: int = 100,
                     status: str | None = None) -> list[models.VirtualBatchOccupation]:
    stmt = select(models.VirtualBatchOccupation).order_by(
        models.VirtualBatchOccupation.id.desc()).limit(limit)
    if status:
        stmt = stmt.where(models.VirtualBatchOccupation.status == status)
    return list(db.scalars(stmt))


def _item_out(db: Session, it: models.OccupationItem) -> dict:
    ass = db.get(models.AssayVersion, it.assay_version_id)
    return {
        "material_id": it.material_id,
        "material_code": it.material_code,
        "material_name": it.material_name,
        "assay_version_id": it.assay_version_id,
        "assay_version": ass.version if ass else "?",
        "lab_report_no": ass.lab_report_no if ass else "?",
        "moisture_pct": it.moisture_pct,
        "dry_factor": it.dry_factor,
        "share_pct_dry": it.share_pct_dry,
        "mass_t_dry": it.mass_t_dry,
        "mass_t_wet": it.mass_t_wet,
        "water_t": it.water_t,
        "cost": it.cost,
        "conversion_trace": it.conversion_trace,
        "available_t_wet_snapshot": it.available_t_wet_snapshot,
        "occupied_before_t_wet": it.occupied_before_t_wet,
        "remaining_after_t_wet_snapshot": it.remaining_after_t_wet_snapshot,
    }


def occupation_out(db: Session, occ: models.VirtualBatchOccupation) -> dict:
    return {
        "id": occ.id, "occ_code": occ.occ_code,
        "scenario_name": occ.scenario_name, "status": occ.status,
        "version": occ.version, "batch_t_dry": occ.batch_t_dry,
        "mode": occ.mode, "total_cost": occ.total_cost,
        "run_id": occ.run_id, "solution_id": occ.solution_id,
        "occupied_at": occ.occupied_at, "expires_at": occ.expires_at,
        "released_at": occ.released_at, "replace_reason": occ.replace_reason,
        "created_at": occ.created_at,
        "items": [_item_out(db, it) for it in occ.items],
        "events": [
            {"id": e.id, "event_type": e.event_type,
             "created_at": e.created_at.isoformat(timespec="seconds"),
             "idempotency_key": e.idempotency_key, "detail": e.detail}
            for e in occ.events
        ],
    }


def list_events(db: Session, limit: int = 200,
                occ_id: int | None = None) -> list[dict]:
    stmt = select(models.OccupationEvent).order_by(
        models.OccupationEvent.id.desc()).limit(limit)
    if occ_id is not None:
        stmt = stmt.where(models.OccupationEvent.occupation_id == occ_id)
    return [{
        "id": e.id, "occupation_id": e.occupation_id, "occ_code": e.occ_code,
        "event_type": e.event_type,
        "created_at": e.created_at.isoformat(timespec="seconds"),
        "idempotency_key": e.idempotency_key, "detail": e.detail,
    } for e in db.scalars(stmt)]


def _capacity_message(shortages: list[dict]) -> str:
    parts = []
    for s in shortages:
        parts.append(
            f"原料 {s['material_code']}（{s['material_name']}）湿基可用 "
            f"{s['availability_t_wet']:g} t，已有效占用 {s['occupied_t_wet']:g} t，"
            f"剩余 {s['remaining_t_wet']:g} t，本次申请 {s['requested_t_wet']:g} t，"
            f"缺口 {s['shortage_t_wet']:g} t（湿基）"
        )
    return "湿基可用量不足，拒绝占用：" + "；".join(parts) + "。"


def log_external_rejection(db: Session, event_type: str, detail: dict,
                           occ: models.VirtualBatchOccupation | None = None,
                           occ_code: str | None = None):
    """缺测等在草稿建立前抛出的错误，也必须留下拒绝事件。"""
    _log(db, occ, event_type, detail, occ_code=occ_code)
    db.commit()
