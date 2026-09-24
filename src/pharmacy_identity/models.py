"""续办案卷与渠道缓冲的数据模型。

所有事实均以追加（append-only）方式留痕：监管事件、案卷版本、材料版本、
签署、渠道状态快照、许可证事件、通知任务都只插入不更新、不删除。
可变的"当前指针"（案卷当前版本、许可证状态）单独存放，并带版本号做
乐观并发控制，确保两次续办争抢同一许可证时只有一个唯一生效案卷。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    """SQLite 以 naive 文本存储时间，全服务统一使用 naive UTC。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def new_id() -> str:
    return uuid.uuid4().hex


def as_utc(value) -> datetime:
    """把外部传入的时间（datetime 或 ISO 字符串）统一为 naive UTC。"""
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class License(Base):
    """许可证主记录：同一许可证编号在同一地区只有一条。"""

    __tablename__ = "licenses"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    license_no: Mapped[str] = mapped_column(String(128), nullable=False)
    region: Mapped[str] = mapped_column(String(64), nullable=False)
    operator_id: Mapped[str] = mapped_column(String(128), nullable=False)
    store_id: Mapped[str] = mapped_column(String(128), nullable=False)
    current_expiry: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    # 证面初始有效期（不可变）；换证后的有效期通过 license_events(renewed) 重放。
    initial_expiry: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    # active | suspended | transferred
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    effective_case_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    __table_args__ = (
        UniqueConstraint("license_no", "region", name="uq_license_no_region"),
    )


class LicenseEvent(Base):
    """许可证生命周期事件（只追加）：暂停、恢复、主体转让、证面有效期变更。"""

    __tablename__ = "license_events"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    license_id: Mapped[str] = mapped_column(
        ForeignKey("licenses.id"), nullable=False, index=True
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    detail: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    __table_args__ = (
        UniqueConstraint("license_id", "seq", name="uq_license_event_seq"),
        Index("ix_license_event_occurred", "license_id", "occurred_at"),
    )


class RegionRule(Base):
    """地区监管规则版本。新版本不覆盖旧版本，历史状态按历史规则重放。"""

    __tablename__ = "region_rules"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    region: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    rule_version: Mapped[str] = mapped_column(String(32), nullable=False)
    effective_from: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    # 缓冲天数：受理后可缓冲营业的最长天数；补正期间是否继续缓冲等
    buffer_days: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    online_buffer_days: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    delivery_buffer_days: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    buffer_requires_acceptance: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    buffer_continues_in_correction: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    detail: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    __table_args__ = (
        UniqueConstraint("region", "rule_version", name="uq_region_rule_version"),
    )


class RenewalCase(Base):
    """续办案卷。一个批次一条案卷，版本自增；材料与事件挂在案卷下。"""

    __tablename__ = "renewal_cases"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    case_ref: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    license_id: Mapped[str] = mapped_column(
        ForeignKey("licenses.id"), nullable=False, index=True
    )
    license_no: Mapped[str] = mapped_column(String(128), nullable=False)
    region: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    operator_id: Mapped[str] = mapped_column(String(128), nullable=False)
    store_id: Mapped[str] = mapped_column(String(128), nullable=False)
    initiated_by: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    # open（续办进行中）| approved | rejected | withdrawn | superseded
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="open")
    current_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    versions: Mapped[list["CaseVersion"]] = relationship(
        back_populates="case", order_by="CaseVersion.seq"
    )


class CaseVersion(Base):
    """案卷的不可变版本。每次提交材料形成一个新版本，作为签署与解释的基准。"""

    __tablename__ = "case_versions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    case_id: Mapped[str] = mapped_column(
        ForeignKey("renewal_cases.id"), nullable=False, index=True
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    # 该版本收录的材料版本 id 集合（固化，便于按案卷版本解释）
    material_revision_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")

    case: Mapped[RenewalCase] = relationship(back_populates="versions")

    __table_args__ = (
        UniqueConstraint("case_id", "seq", name="uq_case_version_seq"),
    )


class MaterialRevision(Base):
    """材料版本。

    同一 document_key 的内容哈希不变 -> 沿用原记录（reuse_of 指向首版）；
    哈希变化 -> 新版本进入 verification_pending，核查通过/驳回分别落状态。
    """

    __tablename__ = "material_revisions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    case_id: Mapped[str] = mapped_column(
        ForeignKey("renewal_cases.id"), nullable=False, index=True
    )
    document_key: Mapped[str] = mapped_column(String(128), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    source_summary: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    valid_from: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    # unverified | verification_pending | verified | rejected
    verification: Mapped[str] = mapped_column(
        String(32), nullable=False, default="unverified"
    )
    reuse_of: Mapped[str | None] = mapped_column(
        ForeignKey("material_revisions.id"), nullable=True
    )
    verified_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "case_id", "document_key", "revision", name="uq_material_revision"
        ),
        Index("ix_material_lookup", "case_id", "document_key", "content_hash"),
    )


class MaterialCheck(Base):
    """材料核查决定（只追加）：as-of 查询按 decided_at 还原当日核查状态。"""

    __tablename__ = "material_checks"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    material_revision_id: Mapped[str] = mapped_column(
        ForeignKey("material_revisions.id"), nullable=False, index=True
    )
    result: Mapped[str] = mapped_column(String(32), nullable=False)  # verified | rejected
    decided_by: Mapped[str] = mapped_column(String(128), nullable=False)
    decided_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    comment: Mapped[str] = mapped_column(Text, nullable=False, default="")


class RegulatoryEvent(Base):
    """监管事件（只追加）：accepted / correction / rejected / approved / licensed。"""

    __tablename__ = "regulatory_events"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    case_id: Mapped[str] = mapped_column(
        ForeignKey("renewal_cases.id"), nullable=False, index=True
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    # 晚到事件：occurred_at 早于已记录事件。绝不倒改历史决定。
    is_late: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    detail: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    __table_args__ = (
        UniqueConstraint("case_id", "seq", name="uq_regulatory_event_seq"),
        Index("ix_reg_event_occurred", "case_id", "occurred_at"),
    )


class BufferSignoff(Base):
    """缓冲放行双签：合规与业务负责人基于同一案卷版本分别签署。"""

    __tablename__ = "buffer_signoffs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    case_id: Mapped[str] = mapped_column(
        ForeignKey("renewal_cases.id"), nullable=False, index=True
    )
    case_version_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)  # compliance | business
    signer: Mapped[str] = mapped_column(String(128), nullable=False)
    signed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    channels: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)

    __table_args__ = (
        UniqueConstraint(
            "case_id", "case_version_seq", "role", name="uq_signoff_version_role"
        ),
    )


class ChannelSnapshot(Base):
    """渠道状态的追加快照：每次重评追加一行，形成可审计的决定链。"""

    __tablename__ = "channel_snapshots"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    license_id: Mapped[str] = mapped_column(
        ForeignKey("licenses.id"), nullable=False, index=True
    )
    channel: Mapped[str] = mapped_column(String(16), nullable=False)  # physical|online|delivery
    state: Mapped[str] = mapped_column(String(16), nullable=False)  # open|restricted|closed
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    reason_detail: Mapped[str] = mapped_column(Text, nullable=False, default="")
    rule_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    rule_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    case_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    case_version_seq: Mapped[int | None] = mapped_column(Integer, nullable=True)
    material_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    event_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    valid_from: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    decided_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    trigger: Mapped[str] = mapped_column(String(48), nullable=False, default="reevaluation")

    __table_args__ = (
        Index("ix_snapshot_channel_time", "license_id", "channel", "valid_from"),
    )


class OrderRiskMarker(Base):
    """已成交订单只追加风险标记，不回改订单本身。"""

    __tablename__ = "order_risk_markers"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    order_no: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    license_id: Mapped[str] = mapped_column(
        ForeignKey("licenses.id"), nullable=False, index=True
    )
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    risk_code: Mapped[str] = mapped_column(String(64), nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="")
    marked_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    valid_from: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class MaintenanceTask(Base):
    """停服期间积压的时钟型任务：补正期限、缓冲到期、通知。恢复后按原定时点补发。"""

    __tablename__ = "maintenance_tasks"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    license_id: Mapped[str | None] = mapped_column(
        ForeignKey("licenses.id"), nullable=True, index=True
    )
    case_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    task_type: Mapped[str] = mapped_column(String(48), nullable=False)
    # correction_deadline | buffer_expiry | notification
    channel: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    due_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    # pending | done | cancelled
    # 幂等键：同类任务（如某渠道缓冲到期、某次补正期限）只登记一次；
    # 仅对 pending 行做部分唯一索引，已取消/完成的旧行不阻塞重新登记。
    dedupe_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        Index(
            "uq_maintenance_pending_dedupe",
            "dedupe_key",
            unique=True,
            sqlite_where=text("status = 'pending'"),
        ),
    )
