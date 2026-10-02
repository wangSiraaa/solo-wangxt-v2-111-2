"""化学口径核心：干湿基换算、质量守恒合成、硅率/铝率/石灰饱和指标。

硬性规则（工艺研发约定）：
1. “未测/缺测”绝不以 0 含量参与计算——缺测直接抛 MissingAssayError；
2. 率值分母为 0 抛 ZeroDenominatorError，不静默返回 inf/0；
3. 所有换算保留逐步 trace，供前端追溯。
"""
from dataclasses import dataclass

from .config import ALKALI_EQ_FACTOR_K2O

EPS = 1e-9


class BlendError(Exception):
    """带错误码的业务异常，API 层映射为 422。"""

    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


class MissingAssayError(BlendError):
    def __init__(self, missing: list[dict]):
        super().__init__(
            "MISSING_ASSAY",
            "存在缺测组分，未测不允许按零含量处理，请补测或剔除该原料后重试。",
            {"missing": missing},
        )


class ZeroDenominatorError(BlendError):
    def __init__(self, indicator: str, denominator: str, value: float):
        super().__init__(
            "ZERO_DENOMINATOR",
            f"{indicator} 的分母（{denominator}）为零或接近零，指标无定义，"
            "不得用零含量兜底。",
            {"indicator": indicator, "denominator": denominator, "value": value},
        )


def dry_factor(moisture_pct: float) -> float:
    """湿基质量 -> 干基质量的系数 1/(1-w)。"""
    w = moisture_pct / 100.0
    if not 0.0 <= w < 1.0:
        raise BlendError(
            "BAD_MOISTURE",
            f"含水率 {moisture_pct}% 越界，必须位于 [0,100)。",
            {"moisture_pct": moisture_pct},
        )
    return 1.0 / (1.0 - w)


def convert_composition(composition: dict, basis: str, moisture_pct: float) -> dict:
    """把化验单统一换算到干基（mass %，干基合计含 LOI）。

    dry: 原样返回；wet: 各组分除以 (1-w)，LOI 同样处理
    （自由水在烘干时脱除，不并入 LOI）。
    """
    if basis == "dry":
        return {k: float(v) for k, v in composition.items()}
    if basis == "wet":
        f = dry_factor(moisture_pct)
        return {k: float(v) * f for k, v in composition.items()}
    raise BlendError("BAD_BASIS", f"未知化验基准: {basis}", {"basis": basis})


def build_conversion_trace(
    material_code: str,
    material_name: str,
    composition: dict,
    basis: str,
    moisture_pct: float,
) -> dict:
    """逐组分留痕：输入值、基准、换算系数、输出干基值。"""
    f = dry_factor(moisture_pct)
    steps = []
    for ox, val in composition.items():
        if basis == "wet":
            steps.append(
                {
                    "component": ox,
                    "basis_in": "wet",
                    "value_in": round(float(val), 4),
                    "formula": f"{val} / (1 - {moisture_pct}/100)",
                    "factor": round(f, 6),
                    "basis_out": "dry",
                    "value_out": round(float(val) * f, 4),
                }
            )
        else:
            steps.append(
                {
                    "component": ox,
                    "basis_in": "dry",
                    "value_in": round(float(val), 4),
                    "formula": "干基化验单，无需水分换算",
                    "factor": 1.0,
                    "basis_out": "dry",
                    "value_out": round(float(val), 4),
                }
            )
    return {
        "material_code": material_code,
        "material_name": material_name,
        "moisture_pct": moisture_pct,
        "assay_basis": basis,
        "dry_factor": round(f, 6),
        "steps": steps,
    }


def require_measured(material_rows: list[dict], required: list[str]) -> None:
    """参与求解的原料必须实测所需组分；缺测即报错，绝不当零。"""
    missing = []
    for row in material_rows:
        measured = set(row.get("measured_oxides") or row["composition"].keys())
        for comp in required:
            if comp == "alkali_eq":
                ok = ("K2O" in measured and "Na2O" in measured)
            else:
                ok = comp in measured
            if not ok:
                missing.append(
                    {
                        "material_code": row["code"],
                        "material_name": row["name"],
                        "assay_version": row["version"],
                        "lab_report_no": row["lab_report_no"],
                        "component": comp,
                    }
                )
    if missing:
        raise MissingAssayError(missing)


@dataclass
class Indicators:
    SM: float          # 硅率 silica modulus
    IM: float          # 铝率 alumina modulus
    KH: float          # 石灰饱和系数 lime saturation factor
    C: float
    S: float
    A: float
    F: float

    def as_dict(self) -> dict:
        return {
            "SM": round(self.SM, 4),
            "IM": round(self.IM, 4),
            "KH": round(self.KH, 4),
            "CaO": round(self.C, 4),
            "SiO2": round(self.S, 4),
            "Al2O3": round(self.A, 4),
            "Fe2O3": round(self.F, 4),
            "warnings": self.warnings(),
        }

    def warnings(self) -> list[str]:
        w = []
        if self.KH < 0:
            w.append("KH 分子为负，CaO 不足以饱和酸性氧化物，实际生料不会出现该结果。")
        return w


def calc_indicators(comp_dry_pct: dict) -> Indicators:
    """硅率 SM=SiO2/(Al2O3+Fe2O3)，铝率 IM=Al2O3/Fe2O3，
    KH=(CaO-1.65Al2O3-0.35Fe2O3)/(2.8SiO2)。
    分母为零必须明确报错。缺测应在进入本函数前拦截。
    """
    try:
        C = float(comp_dry_pct["CaO"])
        S = float(comp_dry_pct["SiO2"])
        A = float(comp_dry_pct["Al2O3"])
        F = float(comp_dry_pct["Fe2O3"])
    except KeyError as e:
        raise MissingAssayError(
            [{"component": e.args[0], "where": "calc_indicators"}]
        )

    if abs(A + F) < EPS:
        raise ZeroDenominatorError("SM", "Al2O3+Fe2O3", A + F)
    if abs(F) < EPS:
        raise ZeroDenominatorError("IM", "Fe2O3", F)
    if abs(S) < EPS:
        raise ZeroDenominatorError("KH", "2.8*SiO2", 2.8 * S)

    return Indicators(
        SM=S / (A + F),
        IM=A / F,
        KH=(C - 1.65 * A - 0.35 * F) / (2.8 * S),
        C=C,
        S=S,
        A=A,
        F=F,
    )


def synthesize(
    picks: list[dict], shares_dry: list[float], components: list[str]
) -> dict:
    """质量守恒合成。

    picks: [{code, name, moisture_pct, composition_dry:% , composition_wet:% (可选)}]
    shares_dry: 干基份额（小数，和为 1）
    返回干基/湿基合成成分（%）、水量平衡，以及逐原料贡献明细。
    """
    dry_pct = {c: 0.0 for c in components}
    wet_pct = {c: 0.0 for c in components}
    contributions = []
    wet_share_sum = 0.0

    for pick, x in zip(picks, shares_dry):
        m = pick["moisture_pct"] / 100.0
        cdry = pick["composition_dry"]
        # 该原料干基 -> 湿基组分：wet% = dry% * (1-m)；湿料中水占 m
        cwet = {c: cdry.get(c, 0.0) * (1.0 - m) for c in components}
        row = {"material_code": pick["code"], "share_dry": x, "by_component": {}}
        for c in components:
            dry_pct[c] += x * cdry.get(c, 0.0)
            row["by_component"][c] = round(x * cdry.get(c, 0.0), 4)
        contributions.append(row)

    # 湿基：以湿料总质量为 100% 重新归一。干基份额 x 对应湿基份额
    # x_wet = x/(1-m) / sum_j x_j/(1-m_j)
    wet_mass_rel = [x / (1.0 - p["moisture_pct"] / 100.0) for x, p in zip(shares_dry, picks)]
    tot_wet_rel = sum(wet_mass_rel)
    water_pct_total = 0.0
    for pick, rel in zip(picks, wet_mass_rel):
        x_wet = rel / tot_wet_rel
        m = pick["moisture_pct"] / 100.0
        wet_share_sum += x_wet
        for c in components:
            wet_pct[c] += x_wet * pick["composition_dry"].get(c, 0.0) * (1.0 - m)
        water_pct_total += x_wet * m
        for row in contributions:
            if row["material_code"] == pick["code"]:
                row["share_wet"] = round(x_wet, 6)

    return {
        "dry_pct": {c: round(v, 4) for c, v in dry_pct.items()},
        "wet_pct": {c: round(v, 4) for c, v in wet_pct.items()},
        "water_pct_in_wet_mix": round(water_pct_total * 100.0, 4),
        "contributions": contributions,
        "dry_share_sum": round(sum(shares_dry), 8),
        "wet_share_sum": round(wet_share_sum, 8),
    }


def alkali_equivalent(comp: dict) -> float | None:
    """Na2O 当量 = Na2O + 0.658*K2O；任一缺测返回 None（调用方负责报错策略）。"""
    if "Na2O" not in comp or "K2O" not in comp:
        return None
    return float(comp["Na2O"]) + ALKALI_EQ_FACTOR_K2O * float(comp["K2O"])
