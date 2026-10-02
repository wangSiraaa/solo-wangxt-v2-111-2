"""FastAPI 入口：原料/化验查询、配比试算、手工评估、历史追溯。

注意：本服务为离线工艺研发试算工具，采用虚构工艺边界与演示数据，
不向任何真实生产设备下发指令。
"""
from pathlib import Path

import numpy as np
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session

from . import chemistry, crud, optimizer
from .database import Base, engine, get_db
from .schemas import (
    BlendRequest,
    BlendResponse,
    EvaluateRequest,
    MaterialOut,
    SolutionItem,
    SolutionOut,
)

Base.metadata.create_all(bind=engine)

app = FastAPI(
    title="离线原料配比试算（虚构工艺边界 · 研发用）",
    version="1.0.0",
    description="质量守恒合成 + 率值计算 + SciPy LP 优化；不连接任何生产控制系统。",
)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)


@app.exception_handler(chemistry.BlendError)
def blend_error_handler(request, exc: chemistry.BlendError):
    from fastapi.responses import JSONResponse

    return JSONResponse(
        status_code=422,
        content={
            "error_code": exc.code,
            "message": exc.message,
            "details": exc.details,
        },
    )


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "rawmix-offline", "mode": "fictional-boundary"}


@app.get("/api/materials", response_model=list[MaterialOut])
def materials(active_only: bool = False, db: Session = Depends(get_db)):
    return crud.list_materials(db, active_only=active_only)


def _serialize_solution(sol: dict) -> SolutionOut:
    return SolutionOut(
        mode=sol["mode"],
        mode_label=sol["mode_label"],
        success=sol["success"],
        total_cost=sol.get("total_cost"),
        cost_per_t_dry=sol.get("cost_per_t_dry"),
        indicators=sol.get("indicators"),
        composition_dry_pct=sol.get("composition_dry_pct"),
        composition_wet_pct=sol.get("composition_wet_pct"),
        water_pct_in_wet_mix=sol.get("water_pct_in_wet_mix"),
        diagnostic=sol.get("diagnostic"),
        items=[SolutionItem(**{k: v for k, v in it.items()
                               if not k.startswith("_")}) for it in sol.get("items", [])],
    )


@app.post("/api/blend", response_model=BlendResponse)
def blend(req: BlendRequest, db: Session = Depends(get_db)):
    pairs = crud.resolve_candidates(db, req.candidates)
    rows = optimizer.prepare_rows(pairs)
    if not rows:
        raise HTTPException(400, "候选原料为空。")
    solutions = optimizer.solve(rows, req)
    run_id, run_code = None, ""
    if req.save:
        run = crud.save_run(db, req, solutions)
        run_id, run_code = run.id, run.run_code
    return BlendResponse(
        run_id=run_id,
        run_code=run_code,
        status="feasible" if any(s["success"] for s in solutions) else "infeasible",
        solutions=[_serialize_solution(s) for s in solutions],
    )


@app.post("/api/evaluate")
def evaluate(req: EvaluateRequest, db: Session = Depends(get_db)):
    """手工给定干基份额做质量守恒合成与率值计算。

    用于显式演示：缺测报错、分母为零报错（不以零含量兜底）。
    """
    if len(req.picks) != len(req.shares_pct_dry):
        raise HTTPException(400, "picks 与 shares_pct_dry 长度必须一致。")
    pairs = crud.resolve_candidates(db, req.picks)
    rows = optimizer.prepare_rows(pairs)

    total = sum(req.shares_pct_dry)
    if total <= 0:
        raise HTTPException(400, "配比份额之和必须为正。")
    x = np.array([v / total for v in req.shares_pct_dry])

    material_rows = [{
        "code": r.code, "name": r.name, "version": r.version,
        "lab_report_no": r.lab_report_no,
        "composition": r.composition_dry, "measured_oxides": list(r.measured),
    } for r in rows]
    chemistry.require_measured(material_rows, ["CaO", "SiO2", "Al2O3", "Fe2O3"])

    components = [c for c in optimizer.COMPONENT_ORDER if any(
        c in r.composition_dry for r in rows
    )]
    pick_dicts = [{
        "code": r.code, "name": r.name, "moisture_pct": r.moisture_pct,
        "composition_dry": {k: r.composition_dry.get(k, 0.0) for k in components},
    } for r in rows]
    synth = chemistry.synthesize(pick_dicts, list(x), components)

    # 率值：分母为零必须由 ZeroDenominatorError 显式抛出
    indicators = chemistry.calc_indicators(synth["dry_pct"]).as_dict()

    items = []
    for r, xi in zip(rows, x):
        if xi < 1e-10:
            continue
        trace = chemistry.build_conversion_trace(
            r.code, r.name, r.composition_raw, r.basis, r.moisture_pct
        )
        items.append({
            "material_code": r.code,
            "material_name": r.name,
            "assay_version": r.version,
            "lab_report_no": r.lab_report_no,
            "share_pct_dry": round(xi * 100.0, 4),
            "conversion_trace": trace,
        })
    return {
        "scenario_name": req.scenario_name,
        "indicators": indicators,
        "composition_dry_pct": synth["dry_pct"],
        "composition_wet_pct": synth["wet_pct"],
        "water_pct_in_wet_mix": synth["water_pct_in_wet_mix"],
        "contributions": synth["contributions"],
        "items": items,
    }


@app.get("/api/runs")
def runs(limit: int = 50, db: Session = Depends(get_db)):
    return crud.list_runs(db, limit)


@app.get("/api/runs/{run_id}")
def run_detail(run_id: int, db: Session = Depends(get_db)):
    detail = crud.get_run_detail(db, run_id)
    if detail is None:
        raise HTTPException(404, "试算记录不存在。")
    return detail


# ---- 生产构建后的静态前端（ng build 产物） ----
_dist = Path(__file__).resolve().parent.parent / "static" / "browser"
if _dist.exists():
    _assets = _dist / "assets"
    if _assets.exists():
        app.mount("/assets", StaticFiles(directory=_assets), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str):
        index = _dist / "index.html"
        if full_path and (candidate := _dist / full_path).is_file():
            return FileResponse(candidate)
        return FileResponse(index)