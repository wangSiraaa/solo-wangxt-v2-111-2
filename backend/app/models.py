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
