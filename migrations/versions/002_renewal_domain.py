"""建立续办案卷与渠道缓冲域全部表。"""

from alembic import op

from pharmacy_identity.schema import metadata

revision = "002_renewal_domain"
down_revision = "001_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    metadata.create_all(bind=bind)


def downgrade() -> None:
    bind = op.get_bind()
    for table in reversed(metadata.sorted_tables):
        table.drop(bind=bind, checkfirst=True)
