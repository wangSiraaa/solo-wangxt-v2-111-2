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


# ---- 虚拟批次占用 ----

class OccupationItemRequest(BaseModel):
    material_id: int
    assay_version_id: int | None = None


class OccupationPreviewRequest(BaseModel):
    """基于一个可行方案做占用预览/建草稿（或直接确认）。

    结构与 BlendRequest 同源，保证占用量与湿基换算口径一致；
    mode 指定占用该次试算的哪个方案（默认 min_cost）。
    """

    scenario_name: str = "未命名虚拟批次"
    batch_t_dry: float = Field(default=1000.0, gt=0)
    candidates: list[Candidate]
    targets: Targets
    hazard_limits_pct: dict[str, float] = {}
    mode: str = "min_cost"
    cheap_material_id: int | None = None
    ttl_seconds: int = Field(default=3600, ge=60, le=7 * 24 * 3600)
    idempotency_key: str | None = Field(default=None, max_length=128)


class OccupationItemOut(BaseModel):
    material_id: int
    material_code: str
    material_name: str
    assay_version_id: int
    assay_version: str
    lab_report_no: str
    moisture_pct: float
    dry_factor: float
    share_pct_dry: float
    mass_t_dry: float
    mass_t_wet: float
    water_t: float
    cost: float
    conversion_trace: dict
    available_t_wet_snapshot: float | None = None
    occupied_before_t_wet: float | None = None
    remaining_after_t_wet_snapshot: float | None = None


class CapacityRow(BaseModel):
    material_id: int
    material_code: str
    material_name: str
    availability_t_wet: float | None          # 湿基可用量；null=不限
    occupied_t_wet: float                      # 有效占用合计
    remaining_t_wet: float | None              # 剩余；null=不限
    unlimited: bool
    effective_occupations: list[dict] = []     # 构成占用的明细


class ProspectiveCapacityRow(CapacityRow):
    requested_t_wet: float = 0.0               # 待确认草稿对该原料的申请量
    would_fit: bool = True                     # 当前占用 + 本草稿是否不超订


class OccupationOut(BaseModel):
    id: int
    occ_code: str
    scenario_name: str
    status: str
    version: int
    batch_t_dry: float
    mode: str
    total_cost: float | None = None
    run_id: int | None = None
    solution_id: int | None = None
    occupied_at: datetime | None = None
    expires_at: datetime | None = None
    released_at: datetime | None = None
    replace_reason: str | None = None
    created_at: datetime | None = None
    items: list[OccupationItemOut] = []
    events: list[dict] = []


class OccupationPreviewResponse(BaseModel):
    occupation_id: int | None                  # 草稿 id（无解/缺测被拒时为 null）
    occ_code: str | None = None
    status: str                                # draft / rejected
    feasible: bool
    mode: str
    total_cost: float | None = None
    items: list[OccupationItemOut] = []
    capacity: list[ProspectiveCapacityRow] = []  # 若本草稿确认后的逐原料容量
    indicators: dict | None = None
    diagnostic: dict | None = None
    ttl_seconds: int | None = None
    version: int | None = None


class OccupationIdRequest(BaseModel):
    expected_version: int
    idempotency_key: str | None = Field(default=None, max_length=128)
    note: str | None = None


class OccupationReplaceRequest(OccupationPreviewRequest):
    """原子替换：expected_version 针对旧占用。"""

    expected_version: int
    replace_note: str | None = None


class CapacityResponse(BaseModel):
    swept_expired: int
    materials: list[CapacityRow]
