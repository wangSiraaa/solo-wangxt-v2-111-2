"""虚拟批次占用服务：跨研发批次的湿基原料容量账本。

关键不变量：
1. 只有“可行方案”才能产生 occupied 占用；失败/缺测/无解绝不写部分占用；
2. 同一原料：湿基可用量 ≥ 有效占用合计 + 新申请（替换时先剔除旧占用）；
3. 替换是原子操作——在同一个事务内验证“新方案 + 全部既存占用”成立后，
   才以新占用替代旧占用，绝不先释放旧量制造并发超订窗口；
4. 乐观并发：逐原料容量账本 version 递增；携带 expected_versions 的确认
   若发现账本已被他人改动，则返回 VERSION_CONFLICT（可恢复，客户端可重取
   容量后重试）；
5. 幂等：确认按 idempotency_key 去重，重试命中既有占用直接回放，不双扣；
6. 释放/过期只追加事件并改状态，占用留痕永久保留，来源方案快照不被改写。
"""
import threading
import uuid
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import crud, models, optimizer
from .chemistry import BlendError

# 容量比较的数值容差（吨）：求解器浮点误差在此范围内视为刚好占满
CAPACITY_EPS = 1e-6

# 覆盖字典中的哨兵：显式取消该原料的一切可用量约束（含档案容量），
# 用于“解除容量后重算以定位缺口”的诊断。
NO_LIMIT = float("inf")


class OccupationError(BlendError):
    """占用业务异常，API 层映射为对应 HTTP 状态码。"""

    def __init__(self, code: str, message: str, status_code: int = 409,
                 details: dict | None = None):
        super().__init__(code, message, details)
        self.status_code = status_code


# ---------------------------------------------------------------------------
# 时间（集中在一处，便于测试与重启后的过期判定保持一致口径）
# ---------------------------------------------------------------------------

def utcnow() -> datetime:
    return datetime.utcnow()


# ---------------------------------------------------------------------------
# 容量账本读取
# ---------------------------------------------------------------------------

def _ensure_ledger_rows(db: Session, material_ids) -> None:
    """幂等确保账本行存在（并发安全：ON CONFLICT DO NOTHING）。"""
    ids = sorted(set(material_ids))
    if not ids:
        return
    db.execute(
        pg_insert(models.MaterialCapacityLedger)
        .values([{"material_id": mid, "version": 1} for mid in ids])
        .on_conflict_do_nothing(index_elements=["material_id"])
    )


def _active_items(db: Session, exclude_occupation_ids: set | None = None,
                  ) -> list[models.OccupationItem]:
    """全部有效（occupied 状态）占用明细。

    exclude_occupation_ids：容量核算时剔除这些占用（例如替换场景中的旧占用、
    以及当前事务刚 flush、尚未提交的本申请占用——避免把自己当既存占用重复扣减）。
    """
    items = list(db.scalars(
        select(models.OccupationItem)
        .join(models.Occupation,
              models.OccupationItem.occupation_id == models.Occupation.id)
        .where(models.Occupation.status == models.OCC_OCCUPIED)
    ))
    exclude = exclude_occupation_ids or set()
    return [it for it in items if it.occupation_id not in exclude]


def capacity_map(db: Session, now: datetime | None = None,
                 exclude_occupation_ids: set | None = None) -> dict:
    """计算逐原料：湿基可用量 / 有效占用 / 剩余 / 账本版本 / 关联占用。

    exclude_occupation_ids 的占用不计入有效占用（替换/并发重核时使用）。
    返回 {material_id: {...}}。调用前通常先 sweep_expired() 释放过期占用。
    """
    now = now or utcnow()
    items = _active_items(db, exclude_occupation_ids=exclude_occupation_ids)
    by_mat: dict[int, dict] = {}
    for it in items:
        row = by_mat.setdefault(it.material_id, {
            "occupied": 0.0, "occupation_ids": set(), "item_rows": []
        })
        row["occupied"] += it.mass_t_wet
        row["occupation_ids"].add(it.occupation_id)
        row["item_rows"].append(it)

    out: dict[int, dict] = {}
    materials = {m.id: m for m in db.scalars(select(models.Material))}
    for mid, mat in materials.items():
        row = by_mat.get(mid)
        occupied = row["occupied"] if row else 0.0
        avail = mat.availability_t_wet
        remaining = None if avail is None else avail - occupied
        led = db.get(models.MaterialCapacityLedger, mid)
        out[mid] = {
            "material_id": mid,
            "material_code": mat.code,
            "material_name": mat.name,
            "moisture_pct": mat.moisture_pct,
            "availability_t_wet": avail,
            "occupied_t_wet": round(occupied, 6),
            "remaining_t_wet": None if remaining is None else round(remaining, 6),
            "ledger_version": led.version if led else 1,
            "active_occupation_ids": sorted(row["occupation_ids"]) if row else [],
            "active_items": [
                {
                    "occupation_id": it.occupation_id,
                    "mass_t_wet": it.mass_t_wet,
                    "share_pct_dry": it.share_pct_dry,
                    "occupation_code": _occupation_code(db, it.occupation_id),
                }
                for it in (row["item_rows"] if row else [])
            ],
        }
    return out


def _occupation_code(db: Session, occupation_id: int) -> str:
    occ = db.get(models.Occupation, occupation_id)
    return occ.occupation_code if occ else f"#{occupation_id}"


# ---------------------------------------------------------------------------
# 过期扫描：服务启动与后台周期执行；重启后过期占用被正确释放
# ---------------------------------------------------------------------------

def _locked_increment(db: Session, material_ids, now: datetime) -> None:
    """对给定原料账本做行锁条件递增（供释放/过期使用）。

    即使与确认事务并发，也以 SELECT … FOR UPDATE 串行化，不会用旧版本覆盖
    对手已提交的新版本。
    """
    _ensure_ledger_rows(db, sorted(set(material_ids)))
    for mid in sorted(set(material_ids)):
        led = db.execute(
            select(models.MaterialCapacityLedger)
            .where(models.MaterialCapacityLedger.material_id == mid)
            .with_for_update()
        ).scalar_one()
        led.version += 1
        led.updated_at = now


def sweep_expired(db: Session, now: datetime | None = None) -> list[int]:
    """把 expires_at 已到的 occupied 占用原子地标记为 expired 并追加事件。

    逐条提交（事件/状态/账本版本同一事务），返回被释放的占用 id 列表。
    原占用明细与来源方案快照保持不变，仅状态由 occupied 变为 expired。
    """
    now = now or utcnow()
    released: list[int] = []
    while True:
        # 每次循环锁定一条到期占用（FOR UPDATE 与确认/替换/释放串行化），
        # 避免“扫描到过期 → 手工释放/替换先提交 → 再被本扫描重复处理”。
        occ = db.scalars(
            select(models.Occupation).where(
                models.Occupation.status == models.OCC_OCCUPIED,
                models.Occupation.expires_at <= now,
            ).order_by(models.Occupation.id).limit(1).with_for_update(
                skip_locked=True)
        ).first()
        if occ is None:
            break
        try:
            occ.status = models.OCC_EXPIRED
            occ.released_at = now
            db.add(models.OccupationEvent(
                occupation_id=occ.id,
                event_type=models.EVENT_EXPIRED,
                event_reason="TTL_EXPIRED",
                detail={"expires_at": occ.expires_at.isoformat(timespec="seconds"),
                        "released_at": now.isoformat(timespec="seconds")},
            ))
            _locked_increment(db, [it.material_id for it in occ.items], now)
            db.commit()
            released.append(occ.id)
        except Exception:
            db.rollback()
    return released


_reaper_started = False
_reaper_lock = threading.Lock()


def start_reaper(session_factory, interval_seconds: float = 60.0):
    """启动后台守护线程周期性释放过期占用（进程内单例）。"""
    global _reaper_started
    with _reaper_lock:
        if _reaper_started:
            return
        _reaper_started = True

    def loop():
        while True:
            try:
                db = session_factory()
                try:
                    sweep_expired(db)
                finally:
                    db.close()
            except Exception:
                pass
            threading.Event().wait(interval_seconds)

    t = threading.Thread(target=loop, name="occupation-reaper", daemon=True)
    t.start()


# ---------------------------------------------------------------------------
# 方案求解（行内）或方案装载（已保存），并把跨批次剩余容量注入 LP
# ---------------------------------------------------------------------------

def _req_for_spec(spec, residual: dict[int, float]):
    """把 OccupationSpec 转成 optimizer 可用的 req 对象（含剩余容量覆盖）。"""
    obj = type("OccupationReq", (), {})()
    obj.scenario_name = spec.scenario_name
    obj.batch_t_dry = spec.batch_t_dry
    obj.targets = spec.targets
    obj.hazard_limits_pct = spec.hazard_limits_pct or {}
    obj.modes = [spec.mode]
    obj.cheap_material_id = spec.cheap_material_id
    obj.availability_override_t_wet = residual
    return obj


def _residual_for(cap: dict, material_ids, replace_id: int | None) -> dict[int, float]:
    """计算各候选原料在“剔除将被替换占用”后的湿基剩余量。

    无限量原料不放入覆盖（继续按无约束处理）。
    """
    residual: dict[int, float] = {}
    for mid in material_ids:
        info = cap.get(mid)
        if info is None or info["availability_t_wet"] is None:
            continue
        occupied = info["occupied_t_wet"]
        if replace_id is not None and replace_id in info["active_occupation_ids"]:
            old = next((it["mass_t_wet"] for it in info["active_items"]
                        if it["occupation_id"] == replace_id), 0.0)
            occupied -= old
        residual[mid] = info["availability_t_wet"] - occupied
    return residual


def solve_spec(db: Session, spec, replace_id: int | None = None) -> dict:
    """按 spec 求一个方案（不持久化）。缺测/无解向上抛出对应业务异常。

    返回 optimizer 的单个 solution dict。
    """
    if spec.source_run_id is not None:
        return _load_saved_solution(db, spec)

    if not spec.candidates or spec.targets is None:
        raise OccupationError(
            "BAD_SPEC",
            "行内占用申请必须提供 candidates 与 targets。",
            status_code=400,
        )
    pairs = crud.resolve_candidates(db, spec.candidates)
    rows = optimizer.prepare_rows(pairs)
    cap = capacity_map(db)
    candidate_ids = [r.material_id for r in rows]
    residual = _residual_for(cap, candidate_ids, replace_id)
    req = _req_for_spec(spec, residual)
    sols = optimizer.solve(rows, req)
    sol = sols[0]
    if sol["success"]:
        return sol

    # 受限求解失败：区分“仅容量挡死”与“本身无解”。
    # 用无可用量约束重算一次（档案容量/跨批次占用都不限制），
    # 若如此即可行，则矛盾来自湿基容量，由调用方按缺口报 CAPACITY_EXCEEDED。
    free_req = _req_for_spec(spec, {})
    # 让候选原料完全不受可用量限制（连档案容量也解除），用于定位容量缺口
    free_req.availability_override_t_wet = {
        mid: NO_LIMIT for mid in candidate_ids
    }
    free_sols = optimizer.solve(rows, free_req)
    free_sol = free_sols[0]
    if free_sol["success"]:
        free_sol["_capacity_blocked"] = True
        return free_sol

    raise OccupationError(
        models.REASON_NOT_FEASIBLE,
        "方案无可行解，不能占用容量。请放宽约束后重试。",
        status_code=422,
        details={"diagnostic": sol.get("diagnostic")},
    )


def _load_saved_solution(db: Session, spec) -> dict:
    """从已保存的 run/solution 重建 solution dict（保持占用时口径一致）。"""
    run = db.get(models.BlendRun, spec.source_run_id)
    if run is None:
        raise OccupationError(
            "RUN_NOT_FOUND",
            f"试算批次 id={spec.source_run_id} 不存在。",
            status_code=404,
            details={"run_id": spec.source_run_id},
        )
    solrec = None
    if spec.source_solution_id is not None:
        solrec = db.get(models.BlendSolution, spec.source_solution_id)
        if solrec is None or solrec.run_id != run.id:
            raise OccupationError(
                "SOLUTION_NOT_FOUND",
                f"方案 id={spec.source_solution_id} 不属于批次 {run.run_code}。",
                status_code=404,
            )
    else:
        solrec = next((s for s in run.solutions if s.success), None)
    if solrec is None or not solrec.success:
        raise OccupationError(
            models.REASON_NOT_FEASIBLE,
            "引用的来源方案不存在或不是可行方案，不能占用容量。",
            status_code=422,
        )
    items = []
    for it in solrec.items:
        mat = db.get(models.Material, it.material_id)
        ass = db.get(models.AssayVersion, it.assay_version_id)
        items.append({
            "material_code": mat.code,
            "material_name": mat.name,
            "assay_version": ass.version,
            "lab_report_no": ass.lab_report_no,
            "share_pct_dry": it.share_pct_dry,
            "mass_t_dry": it.mass_t_dry,
            "mass_t_wet": it.mass_t_wet,
            "water_t": it.water_t,
            "cost": it.cost,
            "conversion_trace": it.conversion_trace,
            "_material_id": it.material_id,
            "_assay_version_id": it.assay_version_id,
        })
    payload = solrec.indicators or {}
    return {
        "mode": solrec.mode,
        "mode_label": optimizer.MODE_LABELS.get(solrec.mode, solrec.mode),
        "success": True,
        "total_cost": solrec.total_cost,
        "cost_per_t_dry": payload.get("cost_per_t_dry"),
        "indicators": payload.get("indicators"),
        "composition_dry_pct": payload.get("composition_dry_pct"),
        "composition_wet_pct": payload.get("composition_wet_pct"),
        "water_pct_in_wet_mix": payload.get("water_pct_in_wet_mix"),
        "items": items,
        "diagnostic": None,
    }


# ---------------------------------------------------------------------------
# 占用快照构建
# ---------------------------------------------------------------------------

def _build_item_snapshots(db: Session, sol: dict, cap: dict,
                          replace_id: int | None,
                          new_versions: dict[int, int]) -> list[dict]:
    """逐原料构造占用明细 dict（干湿基换算 + 来源 + 剩余量快照）。"""
    out: list[dict] = []
    for it in sol["items"]:
        mid = it["_material_id"]
        ass_id = it["_assay_version_id"]
        ass = db.get(models.AssayVersion, ass_id)
        info = cap.get(mid, {})
        avail = info.get("availability_t_wet")
        already = info.get("occupied_t_wet", 0.0)
        if replace_id is not None and replace_id in info.get("active_occupation_ids", []):
            old = next((x["mass_t_wet"] for x in info.get("active_items", [])
                        if x["occupation_id"] == replace_id), 0.0)
            already -= old
        requested = it["mass_t_wet"]
        remaining_after = None
        if avail is not None:
            remaining_after = avail - already - requested
        out.append({
            "material_id": mid,
            "assay_version_id": ass_id,
            "material_code": it["material_code"],
            "material_name": it["material_name"],
            "assay_version": ass.version if ass else "?",
            "lab_report_no": ass.lab_report_no if ass else "?",
            "share_pct_dry": it["share_pct_dry"],
            "mass_t_dry": it["mass_t_dry"],
            "mass_t_wet": it["mass_t_wet"],
            "water_t": it["water_t"],
            "moisture_pct": it["conversion_trace"].get("moisture_pct", 0.0),
            "cost": it["cost"],
            "availability_t_wet_snapshot": avail,
            "already_occupied_t_wet": round(already, 6),
            "requested_t_wet": round(requested, 6),
            "remaining_t_wet_after": None if remaining_after is None
            else round(remaining_after, 6),
            "ledger_version": new_versions.get(mid, 1),
            "conversion_trace": it["conversion_trace"],
            "assay_composition_snapshot": it["conversion_trace"].get("steps", []),
        })
    return out


def _gaps_for_request(cap: dict, sol: dict, replace_id: int | None) -> list[dict]:
    """容量缺口（逐原料，湿基）。"""
    gaps = []
    by_mat = {it["_material_id"]: it for it in sol["items"]}
    for mid, it in by_mat.items():
        info = cap.get(mid)
        if info is None or info["availability_t_wet"] is None:
            continue
        already = info["occupied_t_wet"]
        if replace_id is not None and replace_id in info["active_occupation_ids"]:
            old = next((x["mass_t_wet"] for x in info["active_items"]
                        if x["occupation_id"] == replace_id), 0.0)
            already -= old
        requested = it["mass_t_wet"]
        remaining = info["availability_t_wet"] - already
        gap = requested - remaining
        if gap > CAPACITY_EPS:
            gaps.append({
                "material_id": mid,
                "material_code": info["material_code"],
                "material_name": info["material_name"],
                "availability_t_wet": info["availability_t_wet"],
                "already_occupied_t_wet": round(already, 6),
                "remaining_t_wet": round(remaining, 6),
                "requested_t_wet": round(requested, 6),
                "gap_t_wet": round(gap, 6),
            })
    gaps.sort(key=lambda g: -g["gap_t_wet"])
    return gaps


def _strip_private(it: dict) -> dict:
    return {k: v for k, v in it.items() if not k.startswith("_")}


# ---------------------------------------------------------------------------
# 预览：不落库，返回方案 + 容量评估
# ---------------------------------------------------------------------------

def preview(db: Session, spec, replace_id: int | None,
            expected_versions: dict[str, int] | None = None) -> dict:
    now = utcnow()
    sweep_expired(db, now)
    if replace_id is not None:
        old = db.get(models.Occupation, replace_id)
        if old is None:
            raise OccupationError(models.REASON_NOT_FOUND,
                                  f"被替换占用 id={replace_id} 不存在。",
                                  status_code=404)
        if old.status != models.OCC_OCCUPIED:
            raise OccupationError(
                models.REASON_ALREADY_RELEASED if old.status == models.OCC_RELEASED
                else models.REASON_ALREADY_EXPIRED,
                f"被替换占用 {old.occupation_code} 已{old.status}，不能再替换。",
                status_code=409,
            )

    cap = capacity_map(db, now)
    version_conflicts = _check_expected_versions(cap, expected_versions or {})
    if version_conflicts:
        raise OccupationError(
            models.REASON_VERSION_CONFLICT,
            "容量账本版本已变化：可能有其它批次抢先确认。请重新查询容量后重试。",
            status_code=409,
            details={"conflicts": version_conflicts,
                     "current_versions": _version_codes(cap)},
        )

    sol = solve_spec(db, spec, replace_id)
    gaps = _gaps_for_request(cap, sol, replace_id)
    candidate_ids = [it["_material_id"] for it in sol["items"]]
    new_versions = {mid: cap[mid]["ledger_version"] + 1 for mid in candidate_ids}
    snap_items = _build_item_snapshots(db, sol, cap, replace_id, new_versions)
    return {
        "feasible": True,
        "fits": not gaps,
        "replace_occupation_id": replace_id,
        "solution": sol,
        "items": snap_items,
        "gaps": gaps,
        "current_versions": _version_codes(cap),
        "diagnostic": None,
        "message": None if not gaps else
        "容量不足：" + "；".join(
            f"{g['material_code']} 缺口 {g['gap_t_wet']:g} t（湿基）"
            for g in gaps),
    }


# ---------------------------------------------------------------------------
# 确认（核心事务）
# ---------------------------------------------------------------------------

def _check_expected_versions(cap: dict, expected: dict[str, int]) -> list[dict]:
    """expected: material_code -> 版本。不一致即版本冲突。"""
    conflicts = []
    by_code = {info["material_code"]: info for info in cap.values()}
    for code, ver in expected.items():
        info = by_code.get(code)
        cur = info["ledger_version"] if info else 1
        if cur != ver:
            conflicts.append({
                "material_code": code,
                "expected_version": ver,
                "current_version": cur,
            })
    return conflicts


def _version_codes(cap: dict) -> dict[str, int]:
    return {info["material_code"]: info["ledger_version"] for info in cap.values()}


def _reject_event(db: Session, key: str | None, reason: str, detail: dict):
    """持久化一次被拒申请（含冲突原因）。相同 (key, reason) 去重。"""
    if not key:
        return
    exists = db.scalars(
        select(models.OccupationEvent).where(
            models.OccupationEvent.idempotency_key == key,
            models.OccupationEvent.event_type == models.EVENT_REJECTED,
            models.OccupationEvent.event_reason == reason,
        )
    ).first()
    if exists:
        return
    db.add(models.OccupationEvent(
        occupation_id=None,
        event_type=models.EVENT_REJECTED,
        event_reason=reason,
        idempotency_key=key,
        detail=detail,
    ))
    db.commit()


def confirm(db: Session, spec, *, idempotency_key: str,
            replace_id: int | None = None,
            expected_versions: dict[str, int] | None = None,
            ttl_minutes: int = 24 * 60) -> tuple[dict, bool]:
    """确认占用。返回 (occupation_out, replay)。

    所有检查与写入在同一事务内完成；任何一步失败整体回滚，不留部分占用。
    """
    key = idempotency_key
    # 幂等：同一 key 重试直接回放既有占用，绝不双扣量
    existing = db.scalars(
        select(models.Occupation).where(
            models.Occupation.idempotency_key == key
        )
    ).first()
    if existing is not None:
        return serialize_occupation(db, existing), True

    now = utcnow()
    # 先释放过期（独立事务），再进入确认事务
    sweep_expired(db, now)

    try:
        occ = _confirm_tx(db, spec, key, replace_id,
                          expected_versions or {}, ttl_minutes, now)
    except (OccupationError, BlendError) as exc:
        # 任何业务失败：回滚全部占用写入（绝无部分占用），并把冲突原因落事件流水
        db.rollback()
        detail = {"replace_occupation_id": replace_id,
                  "expected_versions": expected_versions or {}}
        if isinstance(getattr(exc, "details", None), dict):
            detail.update(exc.details)
        _reject_event(db, key, exc.code, detail)
        raise
    except IntegrityError:
        # 唯一约束竞争：并发下另一请求已用相同 key 成功 → 回放
        db.rollback()
        winner = db.scalars(
            select(models.Occupation).where(
                models.Occupation.idempotency_key == key
            )
        ).first()
        if winner is not None:
            return serialize_occupation(db, winner), True
        raise
    return serialize_occupation(db, occ), False


def _confirm_tx(db: Session, spec, key, replace_id, expected_versions,
                ttl_minutes, now) -> models.Occupation:
    # 1) 被替换占用必须仍是 occupied（先 FOR UPDATE 锁定该行，
    #    使两个并发替换/释放/过期扫描在此串行化，杜绝“同一旧占用被替代两次”）
    old_occ = None
    if replace_id is not None:
        old_occ = db.scalars(
            select(models.Occupation)
            .where(models.Occupation.id == replace_id)
            .with_for_update()
        ).first()
        if old_occ is None:
            raise OccupationError(models.REASON_NOT_FOUND,
                                  f"被替换占用 id={replace_id} 不存在。",
                                  status_code=404)
        if old_occ.status != models.OCC_OCCUPIED:
            raise OccupationError(
                models.REASON_ALREADY_RELEASED if old_occ.status == models.OCC_RELEASED
                else models.REASON_ALREADY_EXPIRED,
                f"被替换占用 {old_occ.occupation_code} 已{old_occ.status}，不能再替换。",
                status_code=409,
            )

    # 2) 当前容量账本（含全部有效占用）
    cap = capacity_map(db, now)

    # 3) 乐观版本检查：客户端读到的账本版本必须仍是当前版本
    version_conflicts = _check_expected_versions(cap, expected_versions)
    if version_conflicts:
        raise OccupationError(
            models.REASON_VERSION_CONFLICT,
            "容量账本版本已变化：可能有其它批次抢先确认。请重新查询容量后重试。",
            status_code=409,
            details={"conflicts": version_conflicts,
                     "current_versions": _version_codes(cap)},
        )

    # 4) 求解（剩余容量已剔除“将被替换”的旧占用，注入 LP 上限）
    sol = solve_spec(db, spec, replace_id)

    # 5) 二次硬校验：新申请 + 全部既存占用 ≤ 湿基可用量
    #    （替换场景下旧占用仍在账本中，先在此处逻辑剔除；旧记录稍后原子改状态）
    #    若方案是在“解除容量约束后”才可行，此处必然报出逐原料缺口。
    gaps = _gaps_for_request(cap, sol, replace_id)
    if gaps or sol.get("_capacity_blocked"):
        # 兜底：求解阶段被容量挡死但缺口表为空（理论上不应发生），补一条缺口
        if not gaps:
            gaps = _gaps_for_request(cap, sol, replace_id) or \
                [{"material_id": it["_material_id"],
                  "material_code": it["material_code"],
                  "material_name": it["material_name"],
                  "availability_t_wet": cap.get(it["_material_id"], {})
                      .get("availability_t_wet"),
                  "already_occupied_t_wet": cap.get(it["_material_id"], {})
                      .get("occupied_t_wet", 0.0),
                  "remaining_t_wet": None,
                  "requested_t_wet": it["mass_t_wet"],
                  "gap_t_wet": it["mass_t_wet"]}
                 for it in sol["items"]]
        raise OccupationError(
            models.REASON_CAPACITY_EXCEEDED,
            "湿基可用量不足：" + "；".join(
                f"原料 {g['material_code']}（{g['material_name']}）"
                f"剩余 {g['remaining_t_wet']:g}t，新申请 {g['requested_t_wet']:g}t，"
                f"缺口 {g['gap_t_wet']:g}t（湿基）"
                for g in gaps),
            status_code=409,
            details={"gaps": gaps},
        )

    # 6) 写占用（此时还未提交，任何后续异常都会整体回滚）
    candidate_ids = [it["_material_id"] for it in sol["items"]]
    if spec.source_run_id is not None:
        source_run = db.get(models.BlendRun, spec.source_run_id)
        batch_t_dry = source_run.batch_t_dry if source_run else spec.batch_t_dry
    else:
        batch_t_dry = spec.batch_t_dry
    occ = models.Occupation(
        occupation_code=f"OCC-{uuid.uuid4().hex[:10].upper()}",
        scenario_name=spec.scenario_name,
        batch_t_dry=batch_t_dry,
        mode=spec.mode,
        source_kind="saved" if spec.source_run_id is not None else "inline",
        source_run_id=spec.source_run_id,
        source_solution_id=spec.source_solution_id,
        source_request_snapshot=spec.model_dump(mode="json"),
        solution_snapshot=_solution_snapshot(sol),
        status=models.OCC_OCCUPIED,
        idempotency_key=key,
        replaces_occupation_id=replace_id,
        expires_at=now + timedelta(minutes=ttl_minutes),
        total_cost=sol.get("total_cost"),
    )
    db.add(occ)
    db.flush()

    new_versions = {mid: cap[mid]["ledger_version"] + 1 for mid in candidate_ids}
    for snap in _build_item_snapshots(db, sol, cap, replace_id, new_versions):
        db.add(models.OccupationItem(
            occupation_id=occ.id,
            material_id=snap["material_id"],
            assay_version_id=snap["assay_version_id"],
            material_code=snap["material_code"],
            material_name=snap["material_name"],
            share_pct_dry=snap["share_pct_dry"],
            mass_t_dry=snap["mass_t_dry"],
            mass_t_wet=snap["mass_t_wet"],
            water_t=snap["water_t"],
            moisture_pct=snap["moisture_pct"],
            cost=snap["cost"],
            availability_t_wet_snapshot=snap["availability_t_wet_snapshot"],
            already_occupied_t_wet=snap["already_occupied_t_wet"],
            requested_t_wet=snap["requested_t_wet"],
            remaining_t_wet_after=snap["remaining_t_wet_after"],
            ledger_version=snap["ledger_version"],
            conversion_trace=snap["conversion_trace"],
            assay_composition_snapshot=snap["assay_composition_snapshot"],
        ))

    db.add(models.OccupationEvent(
        occupation_id=occ.id,
        event_type=models.EVENT_CONFIRMED,
        event_reason=None,
        idempotency_key=key,
        detail={
            "replace_occupation_id": replace_id,
            "ttl_minutes": ttl_minutes,
            "expected_versions": expected_versions,
            "items": [
                {"material_code": it["material_code"],
                 "mass_t_wet": it["mass_t_wet"]}
                for it in sol["items"]
            ],
        },
    ))

    # 7) 原子替换：旧占用同事务内标记 released（不先提交释放），追加 replaced 事件
    if old_occ is not None:
        old_occ.status = models.OCC_RELEASED
        old_occ.released_at = now
        db.add(models.OccupationEvent(
            occupation_id=old_occ.id,
            event_type=models.EVENT_REPLACED,
            event_reason="REPLACED_BY_NEW_OCCUPATION",
            detail={"replaced_by_occupation_id": occ.id,
                    "replaced_by_code": occ.occupation_code},
        ))

    # 8) 账本版本原子递增（行锁 + 条件更新）。
    #    并发的另一个确认若已先提交，这里的条件更新影响 0 行，
    #    直接得到可恢复的 VERSION_CONFLICT——即使客户端未显式带 expected_versions。
    affected_materials = set(candidate_ids)
    if old_occ is not None:
        affected_materials.update(it.material_id for it in old_occ.items)
    version_conflict_detail = _bump_ledger_versions(
        db, affected_materials, cap, now,
        sol=sol, replace_id=replace_id, self_occ_id=occ.id
    )
    if version_conflict_detail is not None:
        gaps = version_conflict_detail.get("gaps") or []
        # 并发对手先提交导致版本变化：统一报可恢复的 VERSION_CONFLICT
        # （details 内附最新版本与容量缺口，客户端刷新容量后可安全重试）
        msg = "并发确认冲突：其它研发批次已抢先占用容量，账本版本已变化。请重新查询容量后重试。"
        if gaps:
            msg += " 容量缺口：" + "；".join(
                f"{g['material_code']} 缺 {g['gap_t_wet']:g}t（湿基）"
                for g in gaps)
        raise OccupationError(
            models.REASON_VERSION_CONFLICT,
            msg,
            status_code=409,
            details=version_conflict_detail,
        )

    db.commit()
    db.refresh(occ)
    return occ


def _bump_ledger_versions(db: Session, material_ids: set, cap: dict,
                          now: datetime, sol: dict | None = None,
                          replace_id: int | None = None,
                          self_occ_id: int | None = None) -> dict | None:
    """对每个触及原料：行锁 + 条件更新版本号。

    先到的事务持行锁递增并提交；后到的事务阻塞在 FOR UPDATE 上，
    锁释放后读到最新版本，其“按事务开始时旧版本”的条件更新失配。
    此时不立即判冲突——先用最新账本重新核对一次容量：
    新申请在对手提交后仍放得下就采用新版本继续；放不下才返回
    可恢复的 CAPACITY_EXCEEDED（带版本冲突信息）。
    """
    ids = sorted(material_ids)
    _ensure_ledger_rows(db, ids)

    # 事务进入时各原料的版本（来自先前的 capacity_map 快照）
    snap_versions = {mid: cap.get(mid, {}).get("ledger_version", 1) for mid in ids}

    stale: list[int] = []
    for mid in ids:
        led = db.execute(
            select(models.MaterialCapacityLedger)
            .where(models.MaterialCapacityLedger.material_id == mid)
            .with_for_update()
        ).scalar_one()
        snap_v = snap_versions[mid]
        if led.version != snap_v:
            stale.append(mid)
            continue
        led.version = snap_v + 1
        led.updated_at = now

    if not stale:
        return None

    # 有并发提交：用最新账本重新核对容量。
    # db.expire_all() 丢弃本事务的旧快照（含并发对手刚提交的占用行），
    # 随后 capacity_map 重新读到的 occupied 才是锁释放后的真实合计；
    # 同时排除“本申请自己刚 flush 的占用”与（替换时）已改状态的旧占用。
    db.expire_all()
    db.flush()
    exclude = {self_occ_id} if self_occ_id is not None else set()
    if replace_id is not None:
        exclude.add(replace_id)
    fresh_cap = capacity_map(db, now, exclude_occupation_ids=exclude)
    fresh_gaps = (_gaps_for_request(fresh_cap, sol, replace_id)
                  if sol is not None else [])
    if not fresh_gaps:
        for mid in stale:
            led = db.execute(
                select(models.MaterialCapacityLedger)
                .where(models.MaterialCapacityLedger.material_id == mid)
                .with_for_update()
            ).scalar_one()
            led.version += 1
            led.updated_at = now
        return None

    conflicts = [{
        "material_id": mid,
        "material_code": fresh_cap.get(mid, {}).get("material_code"),
        "expected_version": snap_versions[mid],
        "current_version": fresh_cap.get(mid, {}).get("ledger_version"),
    } for mid in stale]
    return {
        "conflicts": conflicts,
        "current_versions": _version_codes(fresh_cap),
        "gaps": fresh_gaps,
    }


def _solution_snapshot(sol: dict) -> dict:
    """可追溯方案快照（率值/合成/成本/逐原料干湿基换算）。"""
    return {
        "mode": sol["mode"],
        "mode_label": sol.get("mode_label"),
        "total_cost": sol.get("total_cost"),
        "cost_per_t_dry": sol.get("cost_per_t_dry"),
        "indicators": sol.get("indicators"),
        "composition_dry_pct": sol.get("composition_dry_pct"),
        "composition_wet_pct": sol.get("composition_wet_pct"),
        "water_pct_in_wet_mix": sol.get("water_pct_in_wet_mix"),
        "items": [_strip_private(it) for it in sol["items"]],
    }


# ---------------------------------------------------------------------------
# 释放
# ---------------------------------------------------------------------------

def release(db: Session, occupation_id: int,
            expected_versions: dict[str, int] | None = None) -> dict:
    now = utcnow()
    sweep_expired(db, now)
    # 先锁定占用行再判状态，避免与确认/替换/过期扫描并发时重复处理
    occ = db.scalars(
        select(models.Occupation)
        .where(models.Occupation.id == occupation_id)
        .with_for_update()
    ).first()
    if occ is None:
        raise OccupationError(models.REASON_NOT_FOUND,
                              f"占用 id={occupation_id} 不存在。",
                              status_code=404)
    if occ.status == models.OCC_RELEASED:
        db.rollback()
        return {"occupation": serialize_occupation(db, occ), "replay": True}
    if occ.status == models.OCC_EXPIRED:
        db.rollback()
        raise OccupationError(
            models.REASON_ALREADY_EXPIRED,
            f"占用 {occ.occupation_code} 已过期释放，无需重复释放。",
            status_code=409,
        )

    cap = capacity_map(db, now)
    conflicts = _check_expected_versions(cap, expected_versions or {})
    if conflicts:
        db.rollback()
        raise OccupationError(
            models.REASON_VERSION_CONFLICT,
            "容量账本版本已变化，请重新查询容量后重试。",
            status_code=409,
            details={"conflicts": conflicts,
                     "current_versions": _version_codes(cap)},
        )

    occ.status = models.OCC_RELEASED
    occ.released_at = now
    db.add(models.OccupationEvent(
        occupation_id=occ.id,
        event_type=models.EVENT_RELEASED,
        event_reason="MANUAL_RELEASE",
        detail={"released_at": now.isoformat(timespec="seconds")},
    ))
    _locked_increment(db, [it.material_id for it in occ.items], now)
    db.commit()
    db.refresh(occ)
    return {"occupation": serialize_occupation(db, occ), "replay": False}


# ---------------------------------------------------------------------------
# 序列化 / 查询
# ---------------------------------------------------------------------------

def _serialize_item(db: Session, it: models.OccupationItem) -> dict:
    ass = db.get(models.AssayVersion, it.assay_version_id)
    return {
        "material_id": it.material_id,
        "material_code": it.material_code,
        "material_name": it.material_name,
        "assay_version": ass.version if ass else "?",
        "lab_report_no": ass.lab_report_no if ass else "?",
        "share_pct_dry": it.share_pct_dry,
        "mass_t_dry": it.mass_t_dry,
        "mass_t_wet": it.mass_t_wet,
        "water_t": it.water_t,
        "moisture_pct": it.moisture_pct,
        "cost": it.cost,
        "availability_t_wet": it.availability_t_wet_snapshot,
        "already_occupied_t_wet": it.already_occupied_t_wet,
        "requested_t_wet": it.requested_t_wet,
        "remaining_t_wet_after": it.remaining_t_wet_after,
        "ledger_version": it.ledger_version,
        "conversion_trace": it.conversion_trace,
    }


def _serialize_event(ev: models.OccupationEvent) -> dict:
    return {
        "id": ev.id,
        "occupation_id": ev.occupation_id,
        "event_type": ev.event_type,
        "event_reason": ev.event_reason,
        "idempotency_key": ev.idempotency_key,
        "detail": ev.detail,
        "created_at": ev.created_at,
    }


def serialize_occupation(db: Session, occ: models.Occupation,
                         with_solution: bool = True) -> dict:
    items = [_serialize_item(db, it)
             for it in sorted(occ.items, key=lambda x: x.id)]
    events = [_serialize_event(ev)
              for ev in sorted(occ.events, key=lambda x: x.id)]
    out = {
        "id": occ.id,
        "occupation_code": occ.occupation_code,
        "scenario_name": occ.scenario_name,
        "batch_t_dry": occ.batch_t_dry,
        "mode": occ.mode,
        "status": occ.status,
        "source_kind": occ.source_kind,
        "source_run_id": occ.source_run_id,
        "source_solution_id": occ.source_solution_id,
        "replaces_occupation_id": occ.replaces_occupation_id,
        "total_cost": occ.total_cost,
        "created_at": occ.created_at,
        "expires_at": occ.expires_at,
        "released_at": occ.released_at,
        "items": items,
        "events": events,
    }
    if with_solution:
        out["solution"] = occ.solution_snapshot
    return out


def list_occupations(db: Session, status: str | None = None,
                     limit: int = 100) -> list[dict]:
    stmt = select(models.Occupation).order_by(
        models.Occupation.id.desc()).limit(limit)
    occs = list(db.scalars(stmt))
    if status:
        occs = [o for o in occs if o.status == status]
    return [serialize_occupation(db, o, with_solution=False) for o in occs]


def get_occupation(db: Session, occupation_id: int) -> dict | None:
    occ = db.get(models.Occupation, occupation_id)
    return serialize_occupation(db, occ) if occ else None


def list_events(db: Session, limit: int = 200) -> list[dict]:
    evs = db.scalars(
        select(models.OccupationEvent).order_by(
            models.OccupationEvent.id.desc()).limit(limit)
    )
    return [_serialize_event(ev) for ev in evs]
