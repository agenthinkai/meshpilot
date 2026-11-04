"""add cpuset to model instances

Revision ID: f4c7a2d19e60
Revises: e2b6f3c81a94
Create Date: 2026-11-04 16:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f4c7a2d19e60'
down_revision: Union[str, None] = 'e2b6f3c81a94'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('model_instances', sa.Column('cpuset', sa.String(length=32), nullable=True))


def downgrade() -> None:
    op.drop_column('model_instances', 'cpuset')
