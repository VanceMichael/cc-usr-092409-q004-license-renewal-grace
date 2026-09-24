"""续办与渠道缓冲域的表结构（SQLAlchemy Core 元数据，单一事实来源）。

迁移脚本与运行时代码共用本模块的表定义。所有业务时点统一为
``YYYY-MM-DDTHH:MM:SS`` 字符串（UTC），字典序即时间序，方便 SQLite 比较。

两条贯穿全设计的时间线：

* ``occurred_at``——事实声称发生的时点（可能迟到）；
* ``recorded_at``——系统实际知悉/记录的时点（追加后不可变）。

历史裁定只使用 ``recorded_at <= 裁定时点`` 的记录，因此迟到回执永远不会
倒改已经作出的决定。
"""

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
)

metadata = MetaData()

# 经营主体（法人企业）。主体转让即被许可人变化。
operators = Table(
    "operators",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("code", String, nullable=False, unique=True),
    Column("name", String, nullable=False),
    Column("created_at", String, nullable=False),
)

# 地区：规则按地区版本化。
regions = Table(
    "regions",
    metadata,
    Column("code", String, primary_key=True),
    Column("name", String, nullable=False),
)

# 地区规则版本：缓冲天数、补正天数、各渠道在各案卷阶段下的放行状态。
# effective_from 起生效；案卷提交时锁定版本，历史裁定永远可复算。
rule_versions = Table(
    "rule_versions",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("region_code", String, ForeignKey("regions.code"), nullable=False),
    Column("version", String, nullable=False),
    Column("effective_from", String, nullable=False),
    Column("rules", JSON, nullable=False),
    UniqueConstraint("region_code", "version", name="uq_rule_version"),
)
Index("ix_rule_region_effective", rule_versions.c.region_code, rule_versions.c.effective_from)

# 门店（渠道挂在门店上）。
stores = Table(
    "stores",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("code", String, nullable=False, unique=True),
    Column("name", String, nullable=False),
    Column("region_code", String, ForeignKey("regions.code"), nullable=False),
    Column("operator_id", Integer, ForeignKey("operators.id"), nullable=False),
    Column("created_at", String, nullable=False),
)

# 许可证。暂停/转让以监管动作只追加记录，并立即触发渠道重评。
licenses = Table(
    "licenses",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("number", String, nullable=False, unique=True),
    Column("store_id", Integer, ForeignKey("stores.id"), nullable=False),
    Column("operator_id", Integer, ForeignKey("operators.id"), nullable=False),
    Column("issued_at", String, nullable=False),
    Column("expires_at", String, nullable=False, doc="旧证截止时点"),
    Column("created_at", String, nullable=False),
)

# 许可证有效期历史（只追加）：初始登记一行，每次新证到达追加一行。
# as-of 重放据此还原当时的旧证截止日，新证延展不倒改历史。
license_periods = Table(
    "license_periods",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("license_id", Integer, ForeignKey("licenses.id"), nullable=False),
    Column("valid_from", String, nullable=False),
    Column("expires_at", String, nullable=False),
    Column("recorded_at", String, nullable=False),
    Column("source_event_id", Integer, ForeignKey("events.id"), nullable=True),
)
Index("ix_license_period", license_periods.c.license_id,
      license_periods.c.recorded_at)
dossiers = Table(
    "dossiers",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("docket_no", String, nullable=False, unique=True, doc="案卷业务编号（幂等键）"),
    Column("license_id", Integer, ForeignKey("licenses.id"), nullable=False),
    Column("store_id", Integer, ForeignKey("stores.id"), nullable=False),
    Column("operator_id", Integer, ForeignKey("operators.id"), nullable=False),
    Column("region_code", String, ForeignKey("regions.code"), nullable=False),
    Column("status", String, nullable=False, server_default="open",
           doc="open / granted / rejected / withdrawn"),
    Column("revision", Integer, nullable=False, server_default="0",
           doc="案卷版本：材料/事件变化即递增，签署绑定具体版本"),
    Column("rule_version_id", Integer, ForeignKey("rule_versions.id"), nullable=False,
           doc="案卷提交时锁定的地区规则版本"),
    Column("submitted_at", String, nullable=False),
    Column("created_by", String, nullable=False),
    Column("decided_at", String, nullable=True, doc="批准/驳回最终决定时点"),
    CheckConstraint("status in ('open','granted','rejected','withdrawn')", name="ck_dossier_status"),
)
Index("ix_dossier_license", dossiers.c.license_id)

# 案卷版本历史（只追加）：revision 每递增一次记一行，as-of 重放据此还原
# “当时的案卷版本”，签署也是绑定该版本号校验的。
dossier_revisions = Table(
    "dossier_revisions",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("dossier_id", Integer, ForeignKey("dossiers.id"), nullable=False),
    Column("revision", Integer, nullable=False),
    Column("reason", String, nullable=False, doc="submitted/event/material"),
    Column("changed_at", String, nullable=False),
    UniqueConstraint("dossier_id", "revision", name="uq_dossier_revision"),
)

# 案卷生效历史（只追加）：竞争裁决的取得与释放全程留痕，as-of 重放据此
# 确定当时唯一生效案卷，且“谁在何时生效”不会被后来者倒改。
dossier_effectiveness = Table(
    "dossier_effectiveness",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("license_id", Integer, ForeignKey("licenses.id"), nullable=False),
    Column("dossier_id", Integer, ForeignKey("dossiers.id"), nullable=False),
    Column("acquired_at", String, nullable=False),
    Column("released_at", String, nullable=True, doc="让位/终结时点；开放行唯一"),
    Column("release_reason", String, nullable=True),
)
Index(
    "ux_effectiveness_open",
    dossier_effectiveness.c.license_id,
    sqlite_where=dossier_effectiveness.c.released_at.is_(None),
    unique=True,
)
Index("ix_effectiveness_dossier", dossier_effectiveness.c.dossier_id)

# 材料：来源摘要 + 有效时点。每个 (案卷, 文件标识) 一行，内容变化进核查。
materials = Table(
    "materials",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("dossier_id", Integer, ForeignKey("dossiers.id"), nullable=False),
    Column("doc_key", String, nullable=False, doc="业务侧文件标识"),
    Column("source_summary", Text, nullable=False, doc="来源摘要"),
    Column("content_hash", String, nullable=False, doc="内容指纹；变化即进入核查"),
    Column("valid_from", String, nullable=False),
    Column("valid_until", String, nullable=True),
    Column("state", String, nullable=False, server_default="accepted",
           doc="accepted / under_review"),
    Column("review_resolved_at", String, nullable=True,
           doc="最近一次内容核查通过的时点（as-of 重放据此还原材料状态）"),
    Column("first_seen_at", String, nullable=False),
    Column("last_resubmitted_at", String, nullable=False,
           doc="最近一次重送时点（相同内容沿用原记录时仅刷新这里）"),
    Column("resubmit_count", Integer, nullable=False, server_default="1"),
    UniqueConstraint("dossier_id", "doc_key", name="uq_material_doc"),
    CheckConstraint("state in ('accepted','under_review')", name="ck_material_state"),
)

# 材料版本历史（只追加）：内容变化留痕，支撑核查与“采用了哪些材料”。
material_revisions = Table(
    "material_revisions",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("material_id", Integer, ForeignKey("materials.id"), nullable=False),
    Column("content_hash", String, nullable=False),
    Column("source_summary", Text, nullable=False),
    Column("valid_from", String, nullable=False),
    Column("valid_until", String, nullable=True),
    Column("changed_at", String, nullable=False),
)
Index("ix_material_revision", material_revisions.c.material_id, material_revisions.c.changed_at)

# 只追加的监管事件与案卷生命周期事件。
events = Table(
    "events",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("dossier_id", Integer, ForeignKey("dossiers.id"), nullable=False),
    Column("seq", Integer, nullable=False, doc="案卷内单调递增序号"),
    Column("kind", String, nullable=False,
           doc="submitted/accepted/correction_request/answered/correction_overdue_noted/"
               "rejected/approved/license_issued/withdrawn"),
    Column("occurred_at", String, nullable=False, doc="事件声称发生时点（可迟到，绝不倒改）"),
    Column("recorded_at", String, nullable=False, doc="系统记录时点（追加即固定）"),
    Column("payload", JSON, nullable=False),
    Column("client_event_id", String, nullable=True,
           doc="客户端幂等键：同一案卷内同一回执重复送达只入账一次"),
    Column("rule_version_id", Integer, ForeignKey("rule_versions.id"), nullable=True,
           doc="该事件触发裁定时采用的规则版本"),
    UniqueConstraint("dossier_id", "seq", name="uq_event_seq"),
    UniqueConstraint("dossier_id", "client_event_id", name="uq_event_client_id"),
)
Index("ix_event_recorded", events.c.recorded_at)

# 缓冲放行签署：合规人员与业务负责人基于同一案卷版本分别签署。
signoffs = Table(
    "signoffs",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("dossier_id", Integer, ForeignKey("dossiers.id"), nullable=False),
    Column("revision", Integer, nullable=False, doc="签署绑定的案卷版本"),
    Column("role", String, nullable=False, doc="compliance / business"),
    Column("signer", String, nullable=False),
    Column("signed_at", String, nullable=False),
    Column("recorded_at", String, nullable=False),
    UniqueConstraint("dossier_id", "revision", "role", name="uq_signoff_role_rev"),
    CheckConstraint("role in ('compliance','business')", name="ck_signoff_role"),
)

# 许可证级监管动作（暂停/恢复/转让），挂在许可证上，立即重评未完成渠道。
license_actions = Table(
    "license_actions",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("license_id", Integer, ForeignKey("licenses.id"), nullable=False),
    Column("kind", String, nullable=False, doc="suspended / reinstated / transferred"),
    Column("occurred_at", String, nullable=False),
    Column("recorded_at", String, nullable=False),
    Column("detail", JSON, nullable=False),
    CheckConstraint("kind in ('suspended','reinstated','transferred')", name="ck_action_kind"),
)
Index("ix_license_action", license_actions.c.license_id, license_actions.c.recorded_at)

# 渠道状态裁定结果（实体店/线上店/配送）。每次重评状态变化即追加新版本行。
channel_states = Table(
    "channel_states",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("store_id", Integer, ForeignKey("stores.id"), nullable=False),
    Column("channel", String, nullable=False, doc="physical / online / delivery"),
    Column("state", String, nullable=False, doc="open / restricted / closed"),
    Column("reason", String, nullable=False),
    Column("rule_version_id", Integer, ForeignKey("rule_versions.id"), nullable=False),
    Column("dossier_id", Integer, ForeignKey("dossiers.id"), nullable=True),
    Column("based_on_revision", Integer, nullable=True),
    Column("material_ids", JSON, nullable=False, doc="裁定采用的材料记录 id 列表"),
    Column("material_snapshot", JSON, nullable=False,
           doc="裁定时刻采用材料的冻结快照（doc_key/hash/摘要/状态），材料日后变化不倒改本裁定"),
    Column("event_ids", JSON, nullable=False, doc="裁定采用的事件 id 列表"),
    Column("effective_at", String, nullable=False,
           doc="本行生效的记录时点；裁定不回溯，迟到事实只产生新行"),
    Column("superseded_at", String, nullable=True),
    CheckConstraint("channel in ('physical','online','delivery')", name="ck_channel"),
    CheckConstraint("state in ('open','restricted','closed')", name="ck_state"),
)
Index("ix_channel_store", channel_states.c.store_id, channel_states.c.channel,
      channel_states.c.effective_at)

# 渠道期限任务（补正期限/缓冲到期/通知/重评）。截止时间绝对，宕机不丢失。
channel_tasks = Table(
    "channel_tasks",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("store_id", Integer, ForeignKey("stores.id"), nullable=False),
    Column("channel", String, nullable=False),
    Column("kind", String, nullable=False,
           doc="grace_expiry / correction_due / notify / reevaluate"),
    Column("due_at", String, nullable=False),
    Column("status", String, nullable=False, server_default="pending",
           doc="pending / done / cancelled"),
    Column("dossier_id", Integer, ForeignKey("dossiers.id"), nullable=True),
    Column("created_at", String, nullable=False),
    Column("processed_at", String, nullable=True),
    Column("result", JSON, nullable=True),
    UniqueConstraint("store_id", "channel", "kind", "dossier_id", name="uq_channel_task"),
    CheckConstraint("status in ('pending','done','cancelled')", name="ck_task_status"),
)
Index("ix_task_due", channel_tasks.c.due_at, channel_tasks.c.status)

# 已成交订单：成交时冻结当时渠道状态；重评只能追加风险标记。
orders = Table(
    "orders",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("order_no", String, nullable=False, unique=True),
    Column("store_id", Integer, ForeignKey("stores.id"), nullable=False),
    Column("channel", String, nullable=False),
    Column("completed_at", String, nullable=False),
    Column("snapshot_state", String, nullable=False, doc="成交时点冻结的渠道状态"),
    Column("snapshot_reason", String, nullable=False),
    Column("created_at", String, nullable=False),
)
Index("ix_order_store", orders.c.store_id, orders.c.channel, orders.c.completed_at)

order_risk_flags = Table(
    "order_risk_flags",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("order_id", Integer, ForeignKey("orders.id"), nullable=False),
    Column("kind", String, nullable=False, doc="license_suspended / license_transferred / grace_expired / ..."),
    Column("reason", Text, nullable=False),
    Column("flagged_at", String, nullable=False),
    UniqueConstraint("order_id", "kind", name="uq_order_flag_kind"),
)
