"""SciPy 线性规划求解（HiGHS）。

决策变量：x_i = 各原料干基份额（小数），Σx_i=1。
率值约束全部线性化，例如 SM=SiO2/(Al2O3+Fe2O3) ∈ [lo,hi] 等价于：
    Σ(SiO2_i - hi*(Al2O3_i+Fe2O3_i)) x_i ≤ 0
    Σ(lo*(Al2O3_i+Fe2O3_i) - SiO2_i) x_i ≤ 0
其余同理。无解时用“最小违约松弛模型”定位冲突项。
"""
from dataclasses import dataclass

import numpy as np
from scipy.optimize import linprog

from . import chemistry

COMPONENT_ORDER = [
    "CaO", "SiO2", "Al2O3", "Fe2O3",
    "MgO", "SO3", "K2O", "Na2O", "Cl", "LOI",
]
HAZARD_CALC = ["MgO", "SO3", "K2O", "Na2O", "Cl"]


@dataclass
class Row:
    material_id: int
    code: str
    name: str
    moisture_pct: float
    cost_per_t_wet: float
    availability_t_wet: float | None
    min_share_pct: float
    assay_version_id: int
    version: str
    lab_report_no: str
    basis: str
    composition_raw: dict          # 原始化验单（可能湿基）
    composition_dry: dict          # 换算后干基 %
    measured: set[str]


def prepare_rows(candidates: list) -> list[Row]:
    """candidates: 已联表查询好的 (material, assay_version) 二元组列表。"""
    rows = []
    for mat, ass in candidates:
        comp_dry = chemistry.convert_composition(
            ass.composition, ass.basis, mat.moisture_pct
        )
        rows.append(
            Row(
                material_id=mat.id,
                code=mat.code,
                name=mat.name,
                moisture_pct=mat.moisture_pct,
                cost_per_t_wet=mat.cost_per_t_wet,
                availability_t_wet=mat.availability_t_wet,
                min_share_pct=mat.min_share_pct,
                assay_version_id=ass.id,
                version=ass.version,
                lab_report_no=ass.lab_report_no,
                basis=ass.basis,
                composition_raw=dict(ass.composition),
                composition_dry=comp_dry,
                measured=set(ass.measured_oxides),
            )
        )
    return rows


def _hazard_value(row: Row, key: str) -> float:
    comp = row.composition_dry
    if key == "alkali_eq":
        if "K2O" not in row.measured or "Na2O" not in row.measured:
            raise chemistry.MissingAssayError(
                [{
                    "material_code": row.code, "material_name": row.name,
                    "assay_version": row.version, "lab_report_no": row.lab_report_no,
                    "component": "alkali_eq(需 Na2O 与 K2O 均实测)",
                }]
            )
        return comp["Na2O"] + chemistry.ALKALI_EQ_FACTOR_K2O * comp["K2O"]
    if key not in row.measured:
        raise chemistry.MissingAssayError(
            [{
                "material_code": row.code, "material_name": row.name,
                "assay_version": row.version, "lab_report_no": row.lab_report_no,
                "component": key,
            }]
        )
    return comp[key]


def _build_constraints(rows: list[Row], req) -> tuple[list, list, list[dict]]:
    """构造 A_ub x ≤ b_ub（含上下界/率值/有害组分/可用量）。

    返回 A, b, meta；meta 每项带 kind/label/limit，供冲突诊断换算物理量。
    """
    n = len(rows)
    A, b, meta = [], [], []

    def add(coefs, rhs, kind, label, limit=None):
        A.append([float(v) for v in coefs])
        b.append(float(rhs))
        meta.append({"kind": kind, "label": label, "limit": limit,
                     "rhs": float(rhs)})

    cdry = [r.composition_dry for r in rows]

    # --- 率值线性约束 ---
    S = np.array([c.get("SiO2", 0.0) for c in cdry])
    Aa = np.array([c.get("Al2O3", 0.0) for c in cdry])
    F = np.array([c.get("Fe2O3", 0.0) for c in cdry])
    Cc = np.array([c.get("CaO", 0.0) for c in cdry])

    t = req.targets
    if t.SM.max is not None:
        add(S - t.SM.max * (Aa + F), 0.0, "SM_max",
            f"硅率上限 SM≤{t.SM.max}", t.SM.max)
    if t.SM.min is not None:
        add(t.SM.min * (Aa + F) - S, 0.0, "SM_min",
            f"硅率下限 SM≥{t.SM.min}", t.SM.min)
    if t.IM.max is not None:
        add(Aa - t.IM.max * F, 0.0, "IM_max",
            f"铝率上限 IM≤{t.IM.max}", t.IM.max)
    if t.IM.min is not None:
        add(t.IM.min * F - Aa, 0.0, "IM_min",
            f"铝率下限 IM≥{t.IM.min}", t.IM.min)
    if t.KH.max is not None:
        add(Cc - 1.65 * Aa - 0.35 * F - t.KH.max * 2.8 * S, 0.0, "KH_max",
            f"石灰饱和上限 KH≤{t.KH.max}", t.KH.max)
    if t.KH.min is not None:
        add(t.KH.min * 2.8 * S - (Cc - 1.65 * Aa - 0.35 * F), 0.0, "KH_min",
            f"石灰饱和下限 KH≥{t.KH.min}", t.KH.min)

    # --- 有害组分干基上限（线性：Σ comp_i x_i ≤ limit） ---
    for key, limit in (req.hazard_limits_pct or {}).items():
        add([_hazard_value(r, key) for r in rows], float(limit),
            "hazard_" + key, f"有害组分上限 {key}≤{limit}%(干基)", float(limit))

    # --- 可用量（湿基）：B*x/(1-m) ≤ avail → x ≤ avail*(1-m)/B ---
    B = req.batch_t_dry
    for i, r in enumerate(rows):
        if r.availability_t_wet is not None:
            ub = r.availability_t_wet * (1.0 - r.moisture_pct / 100.0) / B
            e = np.zeros(n)
            e[i] = 1.0
            add(e, ub, "avail",
                f"{r.name} 可用量≤{r.availability_t_wet:g}t(湿基)", ub)

    return A, b, meta


def _bounds(rows: list[Row]):
    return [(max(0.0, r.min_share_pct / 100.0), 1.0) for r in rows]


def _cost_coef(rows: list[Row]) -> np.ndarray:
    """每吨干生料的湿料成本系数：c_wet/(1-m)。"""
    return np.array([
        r.cost_per_t_wet / (1.0 - r.moisture_pct / 100.0) for r in rows
    ])


def _solve_lp(rows, req, c_obj, extra_ub=None):
    n = len(rows)
    A, b, meta = _build_constraints(rows, req)
    if extra_ub:
        for (coefs, rhs, info) in extra_ub:
            A.append(list(coefs)); b.append(float(rhs)); meta.append(info)
    res = linprog(
        c_obj,
        A_ub=np.array(A) if A else None,
        b_ub=np.array(b) if b else None,
        A_eq=np.ones((1, n)),
        b_eq=np.array([1.0]),
        bounds=_bounds(rows),
        method="highs",
    )
    return res, A, b, meta


def _achieved(rows: list[Row], kind: str, x: np.ndarray):
    """按约束类型把违约解 x 换算回物理量（率值/百分比/掺量）。"""
    cdry = [r.composition_dry for r in rows]
    S = sum(c.get("SiO2", 0.0) * xi for c, xi in zip(cdry, x))
    Aa = sum(c.get("Al2O3", 0.0) * xi for c, xi in zip(cdry, x))
    F = sum(c.get("Fe2O3", 0.0) * xi for c, xi in zip(cdry, x))
    Cc = sum(c.get("CaO", 0.0) * xi for c, xi in zip(cdry, x))
    table = {
        "SM_max": (S / (Aa + F) if Aa + F > 1e-12 else float("inf")),
        "SM_min": (S / (Aa + F) if Aa + F > 1e-12 else float("inf")),
        "IM_max": (Aa / F if F > 1e-12 else float("inf")),
        "IM_min": (Aa / F if F > 1e-12 else float("inf")),
        "KH_max": ((Cc - 1.65 * Aa - 0.35 * F) / (2.8 * S) if S > 1e-12 else float("inf")),
        "KH_min": ((Cc - 1.65 * Aa - 0.35 * F) / (2.8 * S) if S > 1e-12 else -float("inf")),
    }
    if kind in table:
        return round(table[kind], 4)
    if kind.startswith("hazard_"):
        key = kind[len("hazard_"):]
        return round(sum(_hazard_value(r, key) * xi for r, xi in zip(rows, x)), 4)
    if kind == "avail":
        return None
    return None


def _diagnose(rows: list[Row], req, base_A, base_b, meta) -> dict:
    """最小违约模型：每条不等式加非负松弛 s_k，min Σ s_k/scale。

    松弛显著非零的约束即为冲突项；并用实际合成物的物理量描述超限方向。
    """
    n = len(rows)
    m = len(base_A)
    scales, An = [], []
    for row in base_A:
        sc = max(float(np.max(np.abs(row))), 1.0)
        scales.append(sc)
        An.append([v / sc for v in row])
    bn = [bv / sc for bv, sc in zip(base_b, scales)]

    Au = []
    for i in range(m):
        Au.append(An[i] + [-1.0 if j == i else 0.0 for j in range(m)])
    c = [0.0] * n + [1.0] * m
    bounds = _bounds(rows) + [(0.0, None)] * m
    res = linprog(
        c,
        A_ub=np.array(Au), b_ub=np.array(bn),
        A_eq=np.concatenate([np.ones((1, n)), np.zeros((1, m))], axis=1),
        b_eq=np.array([1.0]),
        bounds=bounds,
        method="highs",
    )
    conflicts = []
    if res.success:
        sl = res.x[n:]
        xpart = res.x[:n]
        for k, v in enumerate(sl):
            raw = float(v * scales[k])
            if raw <= 1e-4:
                continue
            info = meta[k]
            entry = {
                "constraint": info["label"],
                "limit": round(info["limit"], 4) if info["limit"] is not None else None,
                "normalized_gap": round(raw, 4),
            }
            achieved = _achieved(rows, info["kind"], xpart)
            if achieved is not None:
                entry["achieved"] = achieved
            conflicts.append(entry)
    mins = [r.min_share_pct / 100.0 for r in rows]
    if sum(mins) > 1.0 + 1e-9:
        conflicts.append({
            "constraint": "最低掺量之和",
            "limit": 100.0,
            "achieved": round(sum(mins) * 100.0, 4),
            "normalized_gap": round((sum(mins) - 1.0) * 100.0, 4),
        })
    return {
        "reason": "INFEASIBLE_CONSTRAINT_SET",
        "message": "当前约束组合无可行解（求解器状态 infeasible）。下列约束在最小违约解中仍被突破，即为冲突项，请放宽其中之一。",
        "conflicts": conflicts,
        "min_violation_objective": round(float(res.fun), 6) if res.success else None,
    }


def _build_solution(mode, mode_label, rows, req, x, diagnostic=None) -> dict:
    B = req.batch_t_dry
    components = [c for c in COMPONENT_ORDER if any(
        c in r.composition_dry for r in rows
    )]
    # 追加各化验单上出现但不在固定顺序里的组分
    for r in rows:
        for c in r.composition_dry:
            if c not in components:
                components.append(c)

    # 缺测在 prepare 后已由约束/合成前检查保证；这里合成只使用已测键
    pick_dicts = [{
        "code": r.code, "name": r.name,
        "moisture_pct": r.moisture_pct,
        "composition_dry": {k: r.composition_dry.get(k, 0.0) for k in components},
    } for r in rows]
    synth = chemistry.synthesize(pick_dicts, list(x), components)

    # 有害组分合成值（干基）
    hazard_vals = {}
    for key in set(HAZARD_CALC) | set((req.hazard_limits_pct or {}).keys()):
        if key == "alkali_eq":
            hazard_vals["alkali_eq"] = round(sum(
                (r.composition_dry.get("Na2O", 0.0)
                 + chemistry.ALKALI_EQ_FACTOR_K2O * r.composition_dry.get("K2O", 0.0)) * xi
                for r, xi in zip(rows, x)
            ), 4)
        elif any(key in r.composition_dry for r in rows):
            hazard_vals[key] = round(sum(
                r.composition_dry.get(key, 0.0) * xi for r, xi in zip(rows, x)
            ), 4)

    indicators = chemistry.calc_indicators(synth["dry_pct"]).as_dict()

    items, total_cost = [], 0.0
    for r, xi in zip(rows, x):
        if xi < 1e-10:
            continue
        mass_dry = B * xi
        mass_wet = mass_dry / (1.0 - r.moisture_pct / 100.0)
        water = mass_wet - mass_dry
        cost = mass_wet * r.cost_per_t_wet
        total_cost += cost
        trace = chemistry.build_conversion_trace(
            r.code, r.name, r.composition_raw, r.basis, r.moisture_pct
        )
        trace["mass_balance"] = {
            "share_pct_dry": round(xi * 100.0, 4),
            "mass_t_dry": round(mass_dry, 4),
            "formula_mass_wet": f"{mass_dry:.4f} / (1 - {r.moisture_pct}/100)",
            "mass_t_wet": round(mass_wet, 4),
            "water_t": round(water, 4),
            "cost": f"{mass_wet:.4f} t × {r.cost_per_t_wet} 元/t = {cost:.2f} 元",
        }
        items.append({
            "material_code": r.code,
            "material_name": r.name,
            "assay_version": r.version,
            "lab_report_no": r.lab_report_no,
            "share_pct_dry": round(float(xi) * 100.0, 4),
            "mass_t_dry": round(float(mass_dry), 4),
            "mass_t_wet": round(float(mass_wet), 4),
            "water_t": round(float(water), 4),
            "cost": round(float(cost), 2),
            "conversion_trace": trace,
            "_material_id": r.material_id,
            "_assay_version_id": r.assay_version_id,
        })

    return {
        "mode": mode,
        "mode_label": mode_label,
        "success": True,
        "total_cost": round(float(total_cost), 2),
        "cost_per_t_dry": round(float(total_cost) / B, 2),
        "indicators": indicators,
        "composition_dry_pct": synth["dry_pct"],
        "composition_wet_pct": synth["wet_pct"],
        "water_pct_in_wet_mix": synth["water_pct_in_wet_mix"],
        "items": items,
        "diagnostic": diagnostic,
    }


MODE_LABELS = {
    "min_cost": "成本最优",
    "max_cheap": "廉价原料用量最大",
    "balanced": "率值居中平衡方案",
}


def solve(rows: list[Row], req) -> list[dict]:
    n = len(rows)
    # 求解前缺测检查（四大率值组分 + 被约束有害组分必须实测）
    material_rows = [{
        "code": r.code, "name": r.name, "version": r.version,
        "lab_report_no": r.lab_report_no,
        "composition": r.composition_dry, "measured_oxides": list(r.measured),
    } for r in rows]
    chemistry.require_measured(material_rows, ["CaO", "SiO2", "Al2O3", "Fe2O3"])
    for key in (req.hazard_limits_pct or {}):
        chemistry.require_measured(
            material_rows,
            ["Na2O", "K2O"] if key == "alkali_eq" else [key],
        )

    # 最低掺量算术预检
    mins_sum = sum(r.min_share_pct for r in rows)
    if mins_sum > 100.0 + 1e-9:
        diag = {
            "reason": "MIN_SHARE_OVERFLOW",
            "message": f"最低掺量之和 {mins_sum:.2f}% 超过 100%，配比无解。",
            "conflicts": [{
                "constraint": "最低掺量之和",
                "violation_pct_or_t": round(mins_sum - 100.0, 4),
                "lhs": round(mins_sum, 4), "rhs": 100.0,
            }],
        }
        return [_failed_solution(m, diag) for m in req.modes]

    cost_c = _cost_coef(rows)
    results = []

    for mode in req.modes:
        extra_ub, extra_labels = None, None
        if mode == "min_cost":
            c = cost_c
        elif mode == "max_cheap":
            if req.cheap_material_id is None:
                raise chemistry.BlendError(
                    "NO_CHEAP_TARGET",
                    "max_cheap 模式必须指定 cheap_material_id（要最大化的廉价原料）。",
                )
            idx = next((i for i, r in enumerate(rows)
                        if r.material_id == req.cheap_material_id), None)
            if idx is None:
                raise chemistry.BlendError(
                    "CHEAP_NOT_CANDIDATE",
                    "廉价原料不在候选列表中。",
                    {"cheap_material_id": req.cheap_material_id},
                )
            # 第一阶段：最大化廉价料份额
            c1 = np.zeros(n); c1[idx] = -1.0
            res1, A1, b1, meta1 = _solve_lp(rows, req, c1)
            if not res1.success:
                results.append(_failed_solution(mode,
                                                _diagnose(rows, req, A1, b1, meta1)))
                continue
            xbest = -res1.fun
            # 第二阶段：锁定廉价料份额（容差 1e-6）后再最小化成本，打破平局
            e = np.zeros(n); e[idx] = -1.0
            info = {"kind": "lock_cheap",
                    "label": f"锁定廉价料最大份额 {r_name(rows, idx)}≥{xbest*100:.2f}%",
                    "limit": None, "rhs": -(xbest - 1e-6)}
            extra_ub = [(e, -(xbest - 1e-6), info)]
            c = cost_c
        elif mode == "balanced":
            # 目标区间中点偏差最小（线性化绝对值），加成本权重做次序裁决
            mid, cols, names = _midpoint_rows(rows, req)
            results.append(_solve_balanced(rows, req, mid, cols, names, cost_c))
            continue
        else:
            raise chemistry.BlendError("BAD_MODE", f"未知求解模式: {mode}")

        res, A, b, meta = _solve_lp(rows, req, c, extra_ub)
        if not res.success:
            results.append(_failed_solution(mode, _diagnose(rows, req, A, b, meta)))
        else:
            results.append(_build_solution(mode, MODE_LABELS[mode], rows, req,
                                           np.clip(res.x[:n], 0, 1)))
    return results


def r_name(rows, idx):
    return rows[idx].name


def _midpoint_rows(rows, req):
    cdry = [r.composition_dry for r in rows]
    S = np.array([c.get("SiO2", 0.0) for c in cdry])
    Aa = np.array([c.get("Al2O3", 0.0) for c in cdry])
    F = np.array([c.get("Fe2O3", 0.0) for c in cdry])
    Cc = np.array([c.get("CaO", 0.0) for c in cdry])
    mids, cols, names = {}, {}, {}
    t = req.targets
    if t.SM.min is not None and t.SM.max is not None:
        m = (t.SM.min + t.SM.max) / 2
        cols["SM"] = S - m * (Aa + F); names["SM"] = f"SM 偏离中点 {m:.2f}"
    if t.IM.min is not None and t.IM.max is not None:
        m = (t.IM.min + t.IM.max) / 2
        cols["IM"] = Aa - m * F; names["IM"] = f"IM 偏离中点 {m:.2f}"
    if t.KH.min is not None and t.KH.max is not None:
        m = (t.KH.min + t.KH.max) / 2
        cols["KH"] = Cc - 1.65 * Aa - 0.35 * F - m * 2.8 * S
        names["KH"] = f"KH 偏离中点 {m:.3f}"
    return mids, cols, names


def _solve_balanced(rows, req, mid, cols, names, cost_c):
    n = len(rows)
    k = len(cols)
    keys = list(cols.keys())
    G = np.array([cols[kk] for kk in keys])  # k×n，偏差行向量（单位约为成分百分点）

    A_base, b_base, meta_base = _build_constraints(rows, req)
    # 扩展变量 [x, u_1..u_k]，-u ≤ Gx ≤ u，Σx=1，u≥0
    Au, bu = [], []
    for rowA, rhs in zip(A_base, b_base):
        Au.append(list(rowA) + [0.0] * k); bu.append(rhs)
    for j, kk in enumerate(keys):
        Au.append(list(G[j]) + [-1.0 if t == j else 0.0 for t in range(k)])
        bu.append(0.0)
        Au.append(list(-G[j]) + [-1.0 if t == j else 0.0 for t in range(k)])
        bu.append(0.0)
    c = [0.0] * n + [1.0] * k
    # 轻微成本偏好（单位：元/吨干生料，缩放后加入）
    c[:n] = list(1e-3 * cost_c / max(float(np.max(np.abs(cost_c))), 1.0))
    bounds = _bounds(rows) + [(0.0, None)] * k
    res = linprog(
        c,
        A_ub=np.array(Au), b_ub=np.array(bu),
        A_eq=np.concatenate([np.ones((1, n)), np.zeros((1, k))], axis=1),
        b_eq=np.array([1.0]),
        bounds=bounds,
        method="highs",
    )
    if not res.success:
        return _failed_solution("balanced",
                                _diagnose(rows, req, A_base, b_base, meta_base))
    return _build_solution("balanced", MODE_LABELS["balanced"], rows, req,
                           np.clip(res.x[:n], 0, 1))


def _failed_solution(mode, diagnostic) -> dict:
    return {
        "mode": mode,
        "mode_label": MODE_LABELS.get(mode, mode),
        "success": False,
        "diagnostic": diagnostic,
        "items": [],
    }
