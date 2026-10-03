"""数据库读写辅助。"""
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models
from .optimizer import Row, prepare_rows


def list_materials(db: Session, active_only: bool = False):
    stmt = select(models.Material).order_by(models.Material.id)
    if active_only:
        stmt = stmt.where(models.Material.is_active.is_(True))
    mats = list(db.scalars(stmt))
    for m in mats:
        m.assay_versions.sort(key=lambda a: (a.assayed_at, a.id), reverse=True)
    return mats


def resolve_candidates(db: Session, candidates) -> list[tuple]:
    """把 [{material_id, assay_version_id?}] 解析成 (Material, AssayVersion)。"""
    pairs = []
    for c in candidates:
        mat = db.get(models.Material, c.material_id)
        if mat is None:
            from .chemistry import BlendError
            raise BlendError("MATERIAL_NOT_FOUND",
                             f"原料 id={c.material_id} 不存在。",
                             {"material_id": c.material_id})
        if c.assay_version_id is not None:
            ass = db.get(models.AssayVersion, c.assay_version_id)
            if ass is None or ass.material_id != mat.id:
                from .chemistry import BlendError
                raise BlendError("ASSAY_NOT_FOUND",
                                 f"化验版 id={c.assay_version_id} 不属于原料 {mat.code}。")
        else:
            ass = max(mat.assay_versions, key=lambda a: (a.assayed_at, a.id))
        pairs.append((mat, ass))
    return pairs


def rows_from_candidates(db: Session, candidates) -> list[Row]:
    return prepare_rows(resolve_candidates(db, candidates))


def save_run(db: Session, req, solutions: list[dict], scenario_name: str | None = None,
             commit: bool = True):
    run = models.BlendRun(
        run_code=f"RUN-{uuid.uuid4().hex[:10].upper()}",
        scenario_name=scenario_name or getattr(req, "scenario_name", "试算"),
        batch_t_dry=getattr(req, "batch_t_dry", 1000.0),
        target=req.targets.model_dump(),
        constraint_set={
            "hazard_limits_pct": getattr(req, "hazard_limits_pct", {}),
            "cheap_material_id": getattr(req, "cheap_material_id", None),
            "modes": getattr(req, "modes", []),
        },
        status="feasible" if any(s["success"] for s in solutions) else "infeasible",
    )
    db.add(run)
    db.flush()

    for sol in solutions:
        srec = models.BlendSolution(
            run_id=run.id,
            mode=sol["mode"],
            success=sol["success"],
            total_cost=sol.get("total_cost"),
            indicators={
                "indicators": sol.get("indicators"),
                "composition_dry_pct": sol.get("composition_dry_pct"),
                "composition_wet_pct": sol.get("composition_wet_pct"),
                "water_pct_in_wet_mix": sol.get("water_pct_in_wet_mix"),
                "cost_per_t_dry": sol.get("cost_per_t_dry"),
            },
            diagnostic=sol.get("diagnostic"),
        )
        db.add(srec)
        db.flush()
        for it in sol.get("items", []):
            db.add(models.BlendItem(
                run_id=run.id,
                solution_id=srec.id,
                material_id=it["_material_id"],
                assay_version_id=it["_assay_version_id"],
                share_pct_dry=it["share_pct_dry"],
                mass_t_dry=it["mass_t_dry"],
                mass_t_wet=it["mass_t_wet"],
                water_t=it["water_t"],
                cost=it["cost"],
                conversion_trace=it["conversion_trace"],
                assay_composition_snapshot=it["conversion_trace"]["steps"],
            ))
    if commit:
        db.commit()
        db.refresh(run)
    else:
        db.flush()
    return run


def get_run_detail(db: Session, run_id: int):
    run = db.get(models.BlendRun, run_id)
    if run is None:
        return None
    out = {
        "id": run.id,
        "run_code": run.run_code,
        "scenario_name": run.scenario_name,
        "batch_t_dry": run.batch_t_dry,
        "target": run.target,
        "constraint_set": run.constraint_set,
        "status": run.status,
        "created_at": run.created_at.isoformat(timespec="seconds"),
        "solutions": [],
    }
    for s in run.solutions:
        items = []
        for it in s.items:
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
                "raw_assay": {
                    "basis": ass.basis,
                    "composition": ass.composition,
                    "measured_oxides": ass.measured_oxides,
                },
            })
        out["solutions"].append({
            "mode": s.mode,
            "success": s.success,
            "total_cost": s.total_cost,
            "payload": s.indicators,
            "diagnostic": s.diagnostic,
            "items": items,
        })
    return out


def list_runs(db: Session, limit: int = 50):
    runs = list(db.scalars(
        select(models.BlendRun).order_by(models.BlendRun.id.desc()).limit(limit)
    ))
    return [{
        "id": r.id,
        "run_code": r.run_code,
        "scenario_name": r.scenario_name,
        "status": r.status,
        "created_at": r.created_at.isoformat(timespec="seconds"),
        "modes": [s.mode for s in r.solutions],
    } for r in runs]
