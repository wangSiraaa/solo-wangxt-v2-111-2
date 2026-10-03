"""SQLAlchemy 模型：原料、化验版本、试算方案。

化验成分按版本保存（assay_version），方案结果通过 blend_item.assay_version_id
与 assay_composition 回指具体化验单，保证结果可追溯到原始化验版与换算过程。
"""
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


class Material(Base):
    __tablename__ = "material"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(64))
    category: Mapped[str] = mapped_column(String(32))  # 钙质/硅铝质/铁质/校正料/演示用
    moisture_pct: Mapped[float] = mapped_column(Float, default=0.0)  # 收到基含水率 %
    cost_per_t_wet: Mapped[float] = mapped_column(Float)  # 元/吨（收到基/湿基）
    availability_t_wet: Mapped[float | None] = mapped_column(Float, nullable=True)  # 可用量，湿基吨；NULL 不限
    min_share_pct: Mapped[float] = mapped_column(Float, default=0.0)  # 最低掺量（干基份额，%）
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    assay_versions: Mapped[list["AssayVersion"]] = relationship(
        back_populates="material", cascade="all, delete-orphan"
    )


class AssayVersion(Base):
    """原料化验单（一个原料可有多个化验版）。

    composition 形如 {"CaO": 78.2, "SiO2": 4.1, ..., "LOI": 35.0}，
    basis = dry 表示干基化验值（占干样 %），basis = wet 表示收到基化验值（占湿样 %）。
    缺测氧化物应不出现在 dict 中（由 measured_oxides 或 NULL 区分），
    禁止用 0 代替“未测”。
    """

    __tablename__ = "assay_version"
    __table_args__ = (UniqueConstraint("material_id", "version", name="uq_assay_material_version"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    material_id: Mapped[int] = mapped_column(ForeignKey("material.id"))
    version: Mapped[str] = mapped_column(String(32))
    lab_report_no: Mapped[str] = mapped_column(String(64))
    assayed_at: Mapped[datetime] = mapped_column(DateTime)
    basis: Mapped[str] = mapped_column(String(8), default="dry")  # dry / wet
    composition: Mapped[dict] = mapped_column(JSON)
    measured_oxides: Mapped[list] = mapped_column(JSON)  # 实际测定项目，如 ["CaO","SiO2"]
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    material: Mapped["Material"] = relationship(back_populates="assay_versions")


class BlendRun(Base):
    """一次试算（可含多个方案：成本最优/廉价料最多/平衡方案）。"""

    __tablename__ = "blend_run"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_code: Mapped[str] = mapped_column(String(64), unique=True)
    scenario_name: Mapped[str] = mapped_column(String(128))
    batch_t_dry: Mapped[float] = mapped_column(Float)
    target: Mapped[dict] = mapped_column(JSON)
    constraint_set: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(16))  # feasible / infeasible / error
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    remark: Mapped[str | None] = mapped_column(Text, nullable=True)

    items: Mapped[list["BlendItem"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )
    solutions: Mapped[list["BlendSolution"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )


class BlendSolution(Base):
    __tablename__ = "blend_solution"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("blend_run.id"))
    mode: Mapped[str] = mapped_column(String(32))  # min_cost / max_cheap / balanced
    success: Mapped[bool] = mapped_column(Boolean)
    total_cost: Mapped[float | None] = mapped_column(Float, nullable=True)
    indicators: Mapped[dict] = mapped_column(JSON)  # SM/IM/KH + 合成成分 + 有害组分
    diagnostic: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # 冲突项诊断
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    run: Mapped["BlendRun"] = relationship(back_populates="solutions")
    items: Mapped[list["BlendItem"]] = relationship(
        primaryjoin="BlendSolution.id == BlendItem.solution_id",
        viewonly=True,
    )


class BlendItem(Base):
    """某方案下某原料的配比与干湿基换算过程。"""

    __tablename__ = "blend_item"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("blend_run.id"))
    solution_id: Mapped[int] = mapped_column(ForeignKey("blend_solution.id"))
    material_id: Mapped[int] = mapped_column(ForeignKey("material.id"))
    assay_version_id: Mapped[int] = mapped_column(ForeignKey("assay_version.id"))
    share_pct_dry: Mapped[float] = mapped_column(Float)  # 干基份额 %
    mass_t_dry: Mapped[float] = mapped_column(Float)
    mass_t_wet: Mapped[float] = mapped_column(Float)
    water_t: Mapped[float] = mapped_column(Float)
    cost: Mapped[float] = mapped_column(Float)
    # 换算留痕：湿基->干基/干基->湿基每个氧化物的完整过程
    conversion_trace: Mapped[dict] = mapped_column(JSON)
    assay_composition_snapshot: Mapped[dict] = mapped_column(JSON)  # 原始化验单快照

    run: Mapped["BlendRun"] = relationship(back_populates="items")


# ---- 虚拟批次占用（跨批次湿基原料超订防护） ----

# 方案生命周期：草稿（预览，不占量）→ 已占用（占用湿基可用量）
# → 已释放（人工/替换释放，留痕）/ 已过期（TTL 到期自动释放，留痕）
OCC_DRAFT = "draft"
OCC_OCCUPIED = "occupied"
OCC_RELEASED = "released"
OCC_EXPIRED = "expired"
OCC_TERMINAL_STATES = (OCC_RELEASED, OCC_EXPIRED)
# 只有“已占用”计入湿基可用量
OCC_EFFECTIVE_STATES = (OCC_OCCUPIED,)


class VirtualBatchOccupation(Base):
    """一个可行方案对湿基原料的虚拟批次占用。

    状态机：draft --确认--> occupied --释放--> released；
    occupied --超过 ttl--> expired；
    已占用方案可被新方案“原子替换”（旧占用转 released，新占用同事务建立）。
    缺测/无解/失败的方案只允许产生失败事件，绝不产生本记录。
    """

    __tablename__ = "virtual_batch_occupation"

    id: Mapped[int] = mapped_column(primary_key=True)
    occ_code: Mapped[str] = mapped_column(String(64), unique=True)
    scenario_name: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16), default=OCC_DRAFT, index=True)
    # 乐观锁版本号：每次状态转移 +1，确认/释放/替换必须带期望值
    version: Mapped[int] = mapped_column(Integer, default=1)
    # 幂等键：同一 (request_kind, idempotency_key) 重试不重复占量
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    request_kind: Mapped[str] = mapped_column(String(16), default="confirm")  # confirm / replace

    batch_t_dry: Mapped[float] = mapped_column(Float)
    mode: Mapped[str] = mapped_column(String(32))
    total_cost: Mapped[float | None] = mapped_column(Float, nullable=True)
    request_snapshot: Mapped[dict] = mapped_column(JSON)  # 预览/确认时完整求解请求
    result_snapshot: Mapped[dict] = mapped_column(JSON)   # 可行方案结果快照（指标/诊断/明细）

    # 确认后回指已持久化的试算方案（追溯不变）；草稿为空
    run_id: Mapped[int | None] = mapped_column(
        ForeignKey("blend_run.id"), nullable=True
    )
    solution_id: Mapped[int | None] = mapped_column(
        ForeignKey("blend_solution.id"), nullable=True
    )

    occupied_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    released_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    replace_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    items: Mapped[list["OccupationItem"]] = relationship(
        back_populates="occupation", cascade="all, delete-orphan"
    )
    events: Mapped[list["OccupationEvent"]] = relationship(
        back_populates="occupation", cascade="all, delete-orphan",
        order_by="OccupationEvent.id",
    )


class OccupationItem(Base):
    """逐原料占用行：干湿基换算、来源方案、确认时刻剩余量快照。"""

    __tablename__ = "occupation_item"

    id: Mapped[int] = mapped_column(primary_key=True)
    occupation_id: Mapped[int] = mapped_column(
        ForeignKey("virtual_batch_occupation.id")
    )
    material_id: Mapped[int] = mapped_column(ForeignKey("material.id"))
    assay_version_id: Mapped[int] = mapped_column(ForeignKey("assay_version.id"))
    material_code: Mapped[str] = mapped_column(String(32))
    material_name: Mapped[str] = mapped_column(String(64))

    share_pct_dry: Mapped[float] = mapped_column(Float)
    mass_t_dry: Mapped[float] = mapped_column(Float)
    mass_t_wet: Mapped[float] = mapped_column(Float)  # 本占用申请的湿料量
    water_t: Mapped[float] = mapped_column(Float)
    cost: Mapped[float] = mapped_column(Float)
    moisture_pct: Mapped[float] = mapped_column(Float)
    dry_factor: Mapped[float] = mapped_column(Float)
    # 干湿基换算完整留痕（同 blend_item.conversion_trace）
    conversion_trace: Mapped[dict] = mapped_column(JSON)
    assay_composition_snapshot: Mapped[dict] = mapped_column(JSON)

    # 确认时刻容量快照（湿基吨）：可用/本行占用前已占用/本行占用后剩余
    available_t_wet_snapshot: Mapped[float | None] = mapped_column(Float, nullable=True)
    occupied_before_t_wet: Mapped[float] = mapped_column(Float, default=0.0)
    remaining_after_t_wet_snapshot: Mapped[float | None] = mapped_column(Float, nullable=True)

    occupation: Mapped["VirtualBatchOccupation"] = relationship(back_populates="items")


class OccupationEvent(Base):
    """占用事件流：创建、确认、拒绝（容量/版本冲突/缺测/无解）、释放、过期、替换。

    拒绝事件可能没有 occupation_id（缺测/无解方案在建草稿之前即被拒绝）。
    """

    __tablename__ = "occupation_event"

    id: Mapped[int] = mapped_column(primary_key=True)
    occupation_id: Mapped[int | None] = mapped_column(
        ForeignKey("virtual_batch_occupation.id"), nullable=True, index=True
    )
    occ_code: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    event_type: Mapped[str] = mapped_column(String(24), index=True)
    # created/confirmed/confirm_rejected/released/expired/replaced/replace_rejected/
    # missing_assay_rejected/infeasible_rejected
    detail: Mapped[dict] = mapped_column(JSON, default=dict)  # 缺口/版本/原因等结构化信息
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    occupation: Mapped["VirtualBatchOccupation | None"] = relationship(
        back_populates="events"
    )
