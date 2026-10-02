"""后端核心行为测试：换算、合成、率值、报错与求解冲突诊断。"""
import numpy as np
import pytest

from app import chemistry, optimizer
from app.chemistry import (
    BlendError, MissingAssayError, ZeroDenominatorError,
    calc_indicators, convert_composition, dry_factor, synthesize,
)


# ---------- 干湿基换算 ----------
def test_dry_factor_and_wet_to_dry():
    assert dry_factor(18.0) == pytest.approx(1 / 0.82)
    wet = {"SiO2": 41.0}
    dry = convert_composition(wet, basis="wet", moisture_pct=18.0)
    assert dry["SiO2"] == pytest.approx(50.0)


def test_dry_basis_passthrough():
    assert convert_composition({"CaO": 49.5}, "dry", 2.0) == {"CaO": 49.5}


def test_bad_moisture():
    with pytest.raises(BlendError) as e:
        dry_factor(100.0)
    assert e.value.code == "BAD_MOISTURE"


# ---------- 合成 + 率值 ----------
def test_indicators_known_ratio():
    # SiO2 20, A 4, F 2, CaO 66 → SM=3.333, IM=2,
    # KH=(66-6.6-0.7)/56=1.0482
    ind = calc_indicators({"CaO": 66.0, "SiO2": 20.0, "Al2O3": 4.0, "Fe2O3": 2.0})
    assert ind.SM == pytest.approx(20 / 6)
    assert ind.IM == pytest.approx(2.0)
    assert ind.KH == pytest.approx((66 - 1.65 * 4 - 0.35 * 2) / (2.8 * 20))


def test_zero_denominator_im():
    with pytest.raises(ZeroDenominatorError) as e:
        calc_indicators({"CaO": 1.0, "SiO2": 99.0, "Al2O3": 0.5, "Fe2O3": 0.0})
    assert e.value.code == "ZERO_DENOMINATOR"
    assert "IM" in e.value.message


def test_zero_denominator_kh():
    with pytest.raises(ZeroDenominatorError):
        calc_indicators({"CaO": 50.0, "SiO2": 0.0, "Al2O3": 1.0, "Fe2O3": 1.0})


def test_missing_assay_never_treated_as_zero():
    rows = [{
        "code": "SP01", "name": "缺测样", "version": "v1",
        "lab_report_no": "L1", "composition": {}, "measured_oxides": ["CaO"],
    }]
    with pytest.raises(MissingAssayError) as e:
        chemistry.require_measured(rows, ["CaO", "SiO2", "Al2O3", "Fe2O3"])
    missing = {m["component"] for m in e.value.details["missing"]}
    assert missing == {"SiO2", "Al2O3", "Fe2O3"}


# ---------- 质量守恒 ----------
def _pick(code, m, comp):
    return {"code": code, "name": code, "moisture_pct": m, "composition_dry": comp}


def test_synthesize_dry_sum_one_and_water():
    picks = [
        _pick("A", 0.0, {"CaO": 50.0, "LOI": 40.0}),
        _pick("B", 50.0, {"CaO": 10.0, "LOI": 10.0}),
    ]
    out = synthesize(picks, [0.8, 0.2], ["CaO", "LOI"])
    assert out["dry_share_sum"] == pytest.approx(1.0)
    assert out["dry_pct"]["CaO"] == pytest.approx(42.0)
    # 湿料中水：B 湿基相对量 0.2/0.5=0.4，湿料份额 0.4/1.2=1/3，
    # 水占湿混料 1/3×50% = 16.67%
    assert out["water_pct_in_wet_mix"] == pytest.approx(100 / 6, abs=1e-3)


def test_wet_pct_is_renormalized():
    picks = [_pick("A", 10.0, {"CaO": 100.0})]
    out = synthesize(picks, [1.0], ["CaO"])
    assert out["wet_pct"]["CaO"] == pytest.approx(90.0)
    assert out["water_pct_in_wet_mix"] == pytest.approx(10.0)


# ---------- 求解器（用内存构造的 Row） ----------
def _row(mid, code, m, cost, avail, mins, comp, measured=None):
    measured = measured or list(comp)
    return optimizer.Row(
        material_id=mid, code=code, name=code, moisture_pct=m,
        cost_per_t_wet=cost, availability_t_wet=avail, min_share_pct=mins,
        assay_version_id=mid, version="v1", lab_report_no=f"L{mid}",
        basis="dry", composition_raw=dict(comp), composition_dry=dict(comp),
        measured=set(measured),
    )


class _Req:
    def __init__(self, targets, hazards=None, batch=1000.0, modes=("min_cost",),
                 cheap=None):
        self.targets = targets
        self.hazard_limits_pct = hazards or {}
        self.batch_t_dry = batch
        self.modes = list(modes)
        self.cheap_material_id = cheap
        self.scenario_name = "test"


def _iv(lo=None, hi=None):
    from app.schemas import Interval
    return Interval(min=lo, max=hi)


def _targets(sm, im, kh):
    from types import SimpleNamespace
    return SimpleNamespace(
        SM=_iv(*sm), IM=_iv(*im), KH=_iv(*kh),
    )


def test_min_cost_feasible_and_linear_constraints():
    limestone = _row(1, "LS", 2, 65, None, 60,
                     {"CaO": 49.5, "SiO2": 5.2, "Al2O3": 1.4, "Fe2O3": 0.7})
    clay = _row(2, "CL", 6, 42, None, 0,
                {"CaO": 8.0, "SiO2": 55.0, "Al2O3": 17.0, "Fe2O3": 7.5})
    sand = _row(3, "SS", 6, 42, None, 0,
                {"CaO": 1.5, "SiO2": 78.0, "Al2O3": 9.5, "Fe2O3": 3.8})
    iron = _row(4, "IR", 8, 320, None, 0,
                {"CaO": 4.0, "SiO2": 18.0, "Al2O3": 5.0, "Fe2O3": 62.0})
    req = _Req(_targets((2.4, 2.8), (1.4, 1.8), (0.88, 0.94)))
    sols = optimizer.solve([limestone, clay, sand, iron], req)
    s = sols[0]
    assert s["success"]
    shares = sum(it["share_pct_dry"] for it in s["items"])
    assert shares == pytest.approx(100.0, abs=1e-3)
    ind = s["indicators"]
    assert 2.4 - 1e-6 <= ind["SM"] <= 2.8 + 1e-6
    assert 1.4 - 1e-6 <= ind["IM"] <= 1.8 + 1e-6
    assert 0.88 - 1e-6 <= ind["KH"] <= 0.94 + 1e-6


def test_infeasible_reports_conflicts():
    limestone = _row(1, "LS", 2, 65, None, 60,
                     {"CaO": 49.5, "SiO2": 5.2, "Al2O3": 1.4, "Fe2O3": 0.7})
    clay = _row(2, "CL", 14, 22, None, 0,
                {"CaO": 8.0, "SiO2": 55.0, "Al2O3": 17.0, "Fe2O3": 7.5})
    req = _Req(_targets((2.4, 2.8), (1.4, 1.8), (0.88, 0.94)))
    s = optimizer.solve([limestone, clay], req)[0]
    assert not s["success"]
    diag = s["diagnostic"]
    assert diag["reason"] == "INFEASIBLE_CONSTRAINT_SET"
    assert diag["conflicts"]
    # 冲突项必须带物理量
    assert all("achieved" in c for c in diag["conflicts"])


def test_min_share_overflow_arith_conflict():
    a = _row(1, "A", 0, 10, None, 80,
             {"CaO": 50.0, "SiO2": 10.0, "Al2O3": 2.0, "Fe2O3": 1.0})
    b = _row(2, "B", 0, 10, None, 30,
             {"CaO": 10.0, "SiO2": 60.0, "Al2O3": 10.0, "Fe2O3": 5.0})
    s = optimizer.solve([a, b], _Req(_targets((2.0, 4.0), (1.0, 3.0), (0.5, 1.2))))[0]
    assert not s["success"]
    assert s["diagnostic"]["reason"] == "MIN_SHARE_OVERFLOW"


def test_missing_in_solver_raises():
    a = _row(1, "A", 0, 10, None, 0,
             {"CaO": 50.0, "SiO2": 10.0, "Al2O3": 2.0},
             measured=["CaO", "SiO2", "Al2O3"])
    with pytest.raises(MissingAssayError):
        optimizer.solve([a], _Req(_targets((2.0, 4.0), (1.0, 3.0), (0.5, 1.2))))


def test_hazard_limit_active():
    a = _row(1, "A", 0, 10, None, 0,
             {"CaO": 49.5, "SiO2": 5.2, "Al2O3": 1.4, "Fe2O3": 0.7,
              "K2O": 2.0, "Na2O": 0.5, "Cl": 0.01})
    b = _row(2, "B", 0, 10, None, 0,
             {"CaO": 10.0, "SiO2": 60.0, "Al2O3": 10.0, "Fe2O3": 5.0,
              "K2O": 0.1, "Na2O": 0.05, "Cl": 0.0})
    req = _Req(_targets((0.0, 10.0), (0.0, 10.0), (0.0, 2.0)),
               hazards={"Cl": 0.005})
    s = optimizer.solve([a, b], req)[0]
    # B 单独 KH 为负不满足 KH≥0，必须掺入 A；Cl 上限把 A 的干基份额压到 ≤50%
    assert s["success"]
    share_a = next(it["share_pct_dry"] for it in s["items"] if it["material_code"] == "A")
    assert share_a <= 50.0 + 1e-4
    blended_cl = 0.01 * share_a / 100.0
    assert blended_cl <= 0.005 + 1e-9
