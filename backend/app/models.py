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
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
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


# ---------------------------------------------------------------------------
# 虚拟批次占用：多个虚构研发批次争用同一批湿基原料，跨批次共用容量账本。
# 仅成功确认的方案会留下 occupied 占用；释放/过期只追加事件并改状态，留痕不删。
# ---------------------------------------------------------------------------

# 占用生命周期状态
OCC_DRAFT = "draft"        # 草稿：仅预览，未占量（不入库为占用）
OCC_OCCUPIED = "occupied"  # 已占用：湿基容量被该方案有效占用
OCC_RELEASED = "released"  # 已释放：人工释放，留痕保留
OCC_EXPIRED = "expired"    # 已过期：TTL 到期被扫描释放，留痕保留

# 占用事件类型（occupation_event 仅做追加）
EVENT_CONFIRMED = "confirmed"
EVENT_RELEASED = "released"
EVENT_EXPIRED = "expired"
EVENT_REPLACED = "replaced"       # 旧占用被新方案原子替换
EVENT_REJECTED = "rejected"       # 申请被拒（容量不足/版本冲突/无解/缺测等），未占量

# 占用被拒原因码（写入 occupation_event.reason / conflict_detail）
REASON_CAPACITY_EXCEEDED = "CAPACITY_EXCEEDED"
REASON_VERSION_CONFLICT = "VERSION_CONFLICT"
REASON_NOT_FEASIBLE = "NOT_FEASIBLE"
REASON_MISSING_ASSAY = "MISSING_ASSAY"
REASON_ZERO_DENOMINATOR = "ZERO_DENOMINATOR"
REASON_ALREADY_RELEASED = "OCCUPATION_ALREADY_RELEASED"
REASON_ALREADY_EXPIRED = "OCCUPATION_EXPIRED"
REASON_NOT_FOUND = "OCCUPATION_NOT_FOUND"


class MaterialCapacityLedger(Base):
    """逐原料容量账本（每原料一行）：版本号用于乐观并发确认。

    version 在每次对该原料的有效占用集合发生变化时递增；确认请求携带
    expected_versions 可检测“读后被他人抢先确认”的并发窗口。
    """

    __tablename__ = "material_capacity_ledger"

    material_id: Mapped[int] = mapped_column(
        ForeignKey("material.id"), primary_key=True
    )
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class Occupation(Base):
    """一次虚拟批次占用：一个可行方案对若干原料湿基容量的原子占用。"""

    __tablename__ = "occupation"
    __table_args__ = (
        # 幂等键只在已确认的占用上唯一；同一确认请求重试命中同一行，不双扣量
        Index(
            "uq_occupation_idem",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    occupation_code: Mapped[str] = mapped_column(String(64), unique=True)
    scenario_name: Mapped[str] = mapped_column(String(128))
    batch_t_dry: Mapped[float] = mapped_column(Float)
    mode: Mapped[str] = mapped_column(String(32))
    # 来源方案：已保存试算方案（run/solution）或行内试算（inline）
    source_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("blend_run.id"), nullable=True
    )
    source_solution_id: Mapped[int | None] = mapped_column(
        ForeignKey("blend_solution.id"), nullable=True
    )
    source_kind: Mapped[str] = mapped_column(String(16), default="inline")  # saved / inline
    source_request_snapshot: Mapped[dict] = mapped_column(JSON)  # 确认时的完整入参快照
    # 方案结果快照：率值/合成成分/成本等，保证来源方案事后可追溯且不随重算改变
    solution_snapshot: Mapped[dict] = mapped_column(JSON)

    status: Mapped[str] = mapped_column(String(16), default=OCC_OCCUPIED)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    replaces_occupation_id: Mapped[int | None] = mapped_column(
        ForeignKey("occupation.id"), nullable=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    released_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    total_cost: Mapped[float | None] = mapped_column(Float, nullable=True)

    items: Mapped[list["OccupationItem"]] = relationship(
        back_populates="occupation", cascade="all, delete-orphan"
    )
    events: Mapped[list["OccupationEvent"]] = relationship(
        back_populates="occupation", cascade="all, delete-orphan"
    )


class OccupationItem(Base):
    """占用逐原料明细：干湿基换算、来源方案与剩余量快照各存一份。"""

    __tablename__ = "occupation_item"

    id: Mapped[int] = mapped_column(primary_key=True)
    occupation_id: Mapped[int] = mapped_column(ForeignKey("occupation.id"))
    material_id: Mapped[int] = mapped_column(ForeignKey("material.id"))
    assay_version_id: Mapped[int] = mapped_column(ForeignKey("assay_version.id"))
    material_code: Mapped[str] = mapped_column(String(32))
    material_name: Mapped[str] = mapped_column(String(64))
    share_pct_dry: Mapped[float] = mapped_column(Float)
    mass_t_dry: Mapped[float] = mapped_column(Float)
    mass_t_wet: Mapped[float] = mapped_column(Float)  # 本次占用的湿料量
    water_t: Mapped[float] = mapped_column(Float)
    moisture_pct: Mapped[float] = mapped_column(Float)
    cost: Mapped[float] = mapped_column(Float)
    # 容量快照（确认瞬间）：湿基可用量 / 本料已有效占用 / 本笔申请 / 确认后剩余
    availability_t_wet_snapshot: Mapped[float | None] = mapped_column(
        Float, nullable=True
    )
    already_occupied_t_wet: Mapped[float] = mapped_column(Float)
    requested_t_wet: Mapped[float] = mapped_column(Float)
    remaining_t_wet_after: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 该原料账本版本（确认后版本），与 ledger.version 对应
    ledger_version: Mapped[int] = mapped_column(Integer)
    conversion_trace: Mapped[dict] = mapped_column(JSON)
    assay_composition_snapshot: Mapped[dict] = mapped_column(JSON)

    occupation: Mapped["Occupation"] = relationship(back_populates="items")


class OccupationEvent(Base):
    """占用事件流水（只追加）：确认/释放/过期/替换/拒绝及原因。"""

    __tablename__ = "occupation_event"

    id: Mapped[int] = mapped_column(primary_key=True)
    occupation_id: Mapped[int | None] = mapped_column(
        ForeignKey("occupation.id"), nullable=True, index=True
    )
    event_type: Mapped[str] = mapped_column(String(16))  # confirmed/released/expired/replaced/rejected
    event_reason: Mapped[str | None] = mapped_column(String(48), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    occupation: Mapped["Occupation"] = relationship(back_populates="events")
