"""续办案卷、材料版本、监管事件、双签、渠道快照、订单标记与任务。"""

from alembic import op
import sqlalchemy as sa

revision = "002_renewal_cases"
down_revision = "001_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "licenses",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("license_no", sa.String(length=128), nullable=False),
        sa.Column("region", sa.String(length=64), nullable=False),
        sa.Column("operator_id", sa.String(length=128), nullable=False),
        sa.Column("store_id", sa.String(length=128), nullable=False),
        sa.Column("current_expiry", sa.DateTime(), nullable=False),
        sa.Column("initial_expiry", sa.DateTime(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
        sa.Column("effective_case_id", sa.String(length=32), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.UniqueConstraint("license_no", "region", name="uq_license_no_region"),
    )

    op.create_table(
        "license_events",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("license_id", sa.String(length=32), sa.ForeignKey("licenses.id"), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.Column("detail", sa.JSON(), nullable=False),
        sa.UniqueConstraint("license_id", "seq", name="uq_license_event_seq"),
    )
    op.create_index("ix_license_events_license_id", "license_events", ["license_id"])
    op.create_index(
        "ix_license_event_occurred", "license_events", ["license_id", "occurred_at"]
    )

    op.create_table(
        "region_rules",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("region", sa.String(length=64), nullable=False),
        sa.Column("rule_version", sa.String(length=32), nullable=False),
        sa.Column("effective_from", sa.DateTime(), nullable=False),
        sa.Column("buffer_days", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("online_buffer_days", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("delivery_buffer_days", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "buffer_requires_acceptance", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
        sa.Column(
            "buffer_continues_in_correction",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column("detail", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("region", "rule_version", name="uq_region_rule_version"),
    )
    op.create_index("ix_region_rules_region", "region_rules", ["region"])

    op.create_table(
        "renewal_cases",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("case_ref", sa.String(length=64), nullable=False),
        sa.Column("license_id", sa.String(length=32), sa.ForeignKey("licenses.id"), nullable=False),
        sa.Column("license_no", sa.String(length=128), nullable=False),
        sa.Column("region", sa.String(length=64), nullable=False),
        sa.Column("operator_id", sa.String(length=128), nullable=False),
        sa.Column("store_id", sa.String(length=128), nullable=False),
        sa.Column("initiated_by", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="open"),
        sa.Column("current_version", sa.Integer(), nullable=False, server_default="0"),
        sa.UniqueConstraint("case_ref", name="uq_renewal_cases_case_ref"),
    )
    op.create_index("ix_renewal_cases_license_id", "renewal_cases", ["license_id"])
    op.create_index("ix_renewal_cases_region", "renewal_cases", ["region"])

    op.create_table(
        "case_versions",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("case_id", sa.String(length=32), sa.ForeignKey("renewal_cases.id"), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("material_revision_ids", sa.JSON(), nullable=False),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.UniqueConstraint("case_id", "seq", name="uq_case_version_seq"),
    )
    op.create_index("ix_case_versions_case_id", "case_versions", ["case_id"])

    op.create_table(
        "material_revisions",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("case_id", sa.String(length=32), sa.ForeignKey("renewal_cases.id"), nullable=False),
        sa.Column("document_key", sa.String(length=128), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("source_summary", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=128), nullable=False),
        sa.Column("valid_from", sa.DateTime(), nullable=False),
        sa.Column("valid_until", sa.DateTime(), nullable=True),
        sa.Column("received_at", sa.DateTime(), nullable=False),
        sa.Column("verification", sa.String(length=32), nullable=False, server_default="unverified"),
        sa.Column("reuse_of", sa.String(length=32), sa.ForeignKey("material_revisions.id"), nullable=True),
        sa.Column("verified_by", sa.String(length=128), nullable=True),
        sa.Column("verified_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("case_id", "document_key", "revision", name="uq_material_revision"),
    )
    op.create_index("ix_material_revisions_case_id", "material_revisions", ["case_id"])
    op.create_index(
        "ix_material_lookup",
        "material_revisions",
        ["case_id", "document_key", "content_hash"],
    )

    op.create_table(
        "material_checks",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column(
            "material_revision_id",
            sa.String(length=32),
            sa.ForeignKey("material_revisions.id"),
            nullable=False,
        ),
        sa.Column("result", sa.String(length=32), nullable=False),
        sa.Column("decided_by", sa.String(length=128), nullable=False),
        sa.Column("decided_at", sa.DateTime(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=False, server_default=""),
    )
    op.create_index(
        "ix_material_checks_material_revision_id",
        "material_checks",
        ["material_revision_id"],
    )

    op.create_table(
        "regulatory_events",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("case_id", sa.String(length=32), sa.ForeignKey("renewal_cases.id"), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.Column("is_late", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("detail", sa.JSON(), nullable=False),
        sa.UniqueConstraint("case_id", "seq", name="uq_regulatory_event_seq"),
    )
    op.create_index("ix_regulatory_events_case_id", "regulatory_events", ["case_id"])
    op.create_index(
        "ix_reg_event_occurred", "regulatory_events", ["case_id", "occurred_at"]
    )

    op.create_table(
        "buffer_signoffs",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("case_id", sa.String(length=32), sa.ForeignKey("renewal_cases.id"), nullable=False),
        sa.Column("case_version_seq", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("signer", sa.String(length=128), nullable=False),
        sa.Column("signed_at", sa.DateTime(), nullable=False),
        sa.Column("channels", sa.JSON(), nullable=False),
        sa.UniqueConstraint(
            "case_id", "case_version_seq", "role", name="uq_signoff_version_role"
        ),
    )
    op.create_index("ix_buffer_signoffs_case_id", "buffer_signoffs", ["case_id"])

    op.create_table(
        "channel_snapshots",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("license_id", sa.String(length=32), sa.ForeignKey("licenses.id"), nullable=False),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("reason_code", sa.String(length=64), nullable=False),
        sa.Column("reason_detail", sa.Text(), nullable=False, server_default=""),
        sa.Column("rule_id", sa.String(length=32), nullable=True),
        sa.Column("rule_version", sa.String(length=32), nullable=True),
        sa.Column("case_id", sa.String(length=32), nullable=True),
        sa.Column("case_version_seq", sa.Integer(), nullable=True),
        sa.Column("material_ids", sa.JSON(), nullable=False),
        sa.Column("event_ids", sa.JSON(), nullable=False),
        sa.Column("valid_from", sa.DateTime(), nullable=False),
        sa.Column("decided_at", sa.DateTime(), nullable=False),
        sa.Column("trigger", sa.String(length=48), nullable=False, server_default="reevaluation"),
    )
    op.create_index("ix_channel_snapshots_license_id", "channel_snapshots", ["license_id"])
    op.create_index(
        "ix_snapshot_channel_time",
        "channel_snapshots",
        ["license_id", "channel", "valid_from"],
    )

    op.create_table(
        "order_risk_markers",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("order_no", sa.String(length=128), nullable=False),
        sa.Column("license_id", sa.String(length=32), sa.ForeignKey("licenses.id"), nullable=False),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("risk_code", sa.String(length=64), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False, server_default=""),
        sa.Column("marked_at", sa.DateTime(), nullable=False),
        sa.Column("valid_from", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_order_risk_markers_order_no", "order_risk_markers", ["order_no"])
    op.create_index("ix_order_risk_markers_license_id", "order_risk_markers", ["license_id"])

    op.create_table(
        "maintenance_tasks",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("license_id", sa.String(length=32), sa.ForeignKey("licenses.id"), nullable=True),
        sa.Column("case_id", sa.String(length=32), nullable=True),
        sa.Column("task_type", sa.String(length=48), nullable=False),
        sa.Column("channel", sa.String(length=16), nullable=False, server_default=""),
        sa.Column("due_at", sa.DateTime(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("dedupe_key", sa.String(length=128), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("processed_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_maintenance_tasks_license_id", "maintenance_tasks", ["license_id"])
    op.create_index("ix_maintenance_tasks_case_id", "maintenance_tasks", ["case_id"])
    op.create_index("ix_maintenance_tasks_due_at", "maintenance_tasks", ["due_at"])
    op.create_index(
        "uq_maintenance_pending_dedupe",
        "maintenance_tasks",
        ["dedupe_key"],
        unique=True,
        sqlite_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_table("maintenance_tasks")
    op.drop_table("order_risk_markers")
    op.drop_table("channel_snapshots")
    op.drop_table("buffer_signoffs")
    op.drop_table("regulatory_events")
    op.drop_table("material_checks")
    op.drop_table("material_revisions")
    op.drop_table("case_versions")
    op.drop_table("renewal_cases")
    op.drop_table("region_rules")
    op.drop_table("license_events")
    op.drop_table("licenses")
