"""add automation_handoff and automated_run_stopping_point to seer_project_preferences

Revision ID: kencove_001
Revises: c534539c3752
Create Date: 2026-02-18

Kencove patch: adds columns needed for Cursor coding agent handoff.
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "kencove_001"
down_revision = "c534539c3752"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("seer_project_preferences", schema=None) as batch_op:
        batch_op.add_column(sa.Column("automated_run_stopping_point", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("automation_handoff", sa.JSON(), nullable=True))


def downgrade():
    with op.batch_alter_table("seer_project_preferences", schema=None) as batch_op:
        batch_op.drop_column("automation_handoff")
        batch_op.drop_column("automated_run_stopping_point")
