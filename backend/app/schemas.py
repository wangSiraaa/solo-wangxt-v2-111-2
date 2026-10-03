"""Pydantic 入参/出参模型。"""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field
class AssayVersionOut(BaseModel):
    id: int
    version: str
    lab_report_no: str
    assayed_at: datetime
    basis: str
    composition: dict
    measured_oxides: list


class MaterialOut(BaseModel):
    id: int
    code: str
    name: str
    category: str
    moisture_pct: float
    cost_per_t_wet: float
    availability_t_wet: float | None
    min_share_pct: float
    is_active: bool
    note: str | None = None
    assay_versions: list[AssayVersionOut] = []


class Interval(BaseModel):
    min: float | None = None
    max: float | None = None


class Targets(BaseModel):
    SM: Interval
    IM: Interval
    KH: Interval


class Candidate(BaseModel):
    material_id: int
    assay_version_id: int | None = None  # 默认取最新版


class BlendRequest(BaseModel):
    scenario_name: str = "未命名试算"
    batch_t_dry: float = Field(default=1000.0, gt=0)
    candidates: list[Candidate]
    targets: Targets
    hazard_limits_pct: dict[str, float] = {}  # 干基 %，如 {"Cl": 0.03, "alkali_eq": 1.5}
    modes: list[Literal["min_cost", "max_cheap", "balanced"]] = ["min_cost"]
    cheap_material_id: int | None = None  # max_cheap 模式的“廉价原料”
    save: bool = True


class EvaluateRequest(BaseModel):
    """手工配比试算：直接给干基份额，用于分母为零/缺测报错演示。"""

    scenario_name: str = "手工配比"
    picks: list[Candidate]
    shares_pct_dry: list[float]  # 与 picks 等长；和不强制 100，会归一化


class SolutionItem(BaseModel):
    material_code: str
    material_name: str
    assay_version: str
    lab_report_no: str
    share_pct_dry: float
    mass_t_dry: float
    mass_t_wet: float
    water_t: float
    cost: float
    conversion_trace: dict


class SolutionOut(BaseModel):
    mode: str
    mode_label: str
    success: bool
    total_cost: float | None = None
    cost_per_t_dry: float | None = None
    indicators: dict | None = None
    composition_dry_pct: dict | None = None
    composition_wet_pct: dict | None = None
    water_pct_in_wet_mix: float | None = None
    items: list[SolutionItem] = []
    diagnostic: dict | None = None


class BlendResponse(BaseModel):
    run_id: int | None
    run_code: str
    status: str
    solutions: list[SolutionOut]


# ---------------------------------------------------------------------------
# 虚拟批次占用
# ---------------------------------------------------------------------------

class OccupationSpec(BaseModel):
    """占用申请的方案口径：行内试算（与 /api/blend 相同）或引用已保存方案。"""

    scenario_name: str = "虚拟批次占用"
    batch_t_dry: float = Field(default=1000.0, gt=0)
    candidates: list[Candidate] | None = None
    targets: Targets | None = None
    hazard_limits_pct: dict[str, float] = {}
    mode: Literal["min_cost", "max_cheap", "balanced"] = "min_cost"
    cheap_material_id: int | None = None
    # 引用已保存试算方案（与行内字段二选一）
    source_run_id: int | None = None
    source_solution_id: int | None = None


class OccupationPreviewRequest(BaseModel):
    spec: OccupationSpec
    replace_occupation_id: int | None = None
    expected_versions: dict[str, int] = {}  # material_code -> 期望账本版本


class OccupationConfirmRequest(BaseModel):
    spec: OccupationSpec
    replace_occupation_id: int | None = None
    expected_versions: dict[str, int] = {}
    idempotency_key: str = Field(min_length=8, max_length=128)
    ttl_minutes: int = Field(default=24 * 60, ge=1, le=365 * 24 * 60)


class OccupationReleaseRequest(BaseModel):
    idempotency_key: str | None = Field(default=None, max_length=128)
    expected_versions: dict[str, int] = {}


class OccupationItemOut(BaseModel):
    material_id: int
    material_code: str
    material_name: str
    assay_version: str
    lab_report_no: str
    share_pct_dry: float
    mass_t_dry: float
    mass_t_wet: float
    water_t: float
    moisture_pct: float
    cost: float
    availability_t_wet: float | None
    already_occupied_t_wet: float
    requested_t_wet: float
    remaining_t_wet_after: float | None
    ledger_version: int
    conversion_trace: dict


class CapacityGap(BaseModel):
    material_id: int
    material_code: str
    material_name: str
    availability_t_wet: float | None
    already_occupied_t_wet: float
    remaining_t_wet: float | None
    requested_t_wet: float
    gap_t_wet: float


class MaterialCapacityOut(BaseModel):
    material_id: int
    material_code: str
    material_name: str
    moisture_pct: float
    availability_t_wet: float | None      # 湿基可用量（NULL=不限量）
    occupied_t_wet: float                 # 有效占用合计（不含已释放/已过期）
    remaining_t_wet: float | None         # 湿基剩余（NULL=不限量）
    ledger_version: int
    active_occupation_ids: list[int]
    active_items: list[dict]


class CapacityOut(BaseModel):
    as_of: datetime
    expired_released: list[int]
    materials: list[MaterialCapacityOut]


class OccupationEventOut(BaseModel):
    id: int
    occupation_id: int | None = None
    event_type: str
    event_reason: str | None
    idempotency_key: str | None = None
    detail: dict | None
    created_at: datetime


class OccupationOut(BaseModel):
    id: int
    occupation_code: str
    scenario_name: str
    batch_t_dry: float
    mode: str
    status: str
    source_kind: str
    source_run_id: int | None
    source_solution_id: int | None
    replaces_occupation_id: int | None
    total_cost: float | None
    created_at: datetime
    expires_at: datetime
    released_at: datetime | None
    items: list[OccupationItemOut]
    events: list[OccupationEventOut] = []
    solution: dict | None = None  # 方案结果快照（预览/确认返回）


class OccupationPreviewResponse(BaseModel):
    feasible: bool
    fits: bool                            # 容量是否足够（考虑替换）
    replace_occupation_id: int | None
    solution: SolutionOut | None
    items: list[OccupationItemOut]        # 以当前账本估算的逐原料占用快照
    gaps: list[CapacityGap]
    current_versions: dict[str, int]
    diagnostic: dict | None = None
    message: str | None = None


class OccupationConfirmResponse(BaseModel):
    occupation: OccupationOut
    replay: bool = False                  # True=幂等重试命中既有占用，未再扣量
    replaced_occupation_id: int | None = None
    versions: dict[str, int]
