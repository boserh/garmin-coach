"""rename demo account to bihun

The app was renamed Garmin Coach → Bihun. `app.demo.ensure_demo_user` finds the
singleton demo account BY EMAIL, so on an existing install the new constant would miss
the old row and seed a second demo account, leaving the first one orphaned in
/admin/users. Rename the row in place instead. Guarded on both sides so it is a no-op
where the demo account was never created (a fresh DB) or already carries the new name.

Revision ID: 343cc3f80d00
Revises: 7a2b1e7cbd53
Create Date: 2026-08-11 04:51:26.283323
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '343cc3f80d00'
down_revision: Union[str, None] = '7a2b1e7cbd53'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD = "demo@garmin-coach.local"
_NEW = "demo@bihun.local"


def _rename(frm: str, to: str) -> None:
    # `email` is UNIQUE — bail out if the target address is already taken (a demo account
    # created under the new name before this migration ran). The stale row is then dead
    # weight, not something to silently merge, so it is left for an admin to delete.
    conn = op.get_bind()
    taken = conn.execute(
        sa.text("SELECT 1 FROM users WHERE email = :e"), {"e": to}
    ).first()
    if taken is not None:
        return
    conn.execute(
        sa.text("UPDATE users SET email = :to WHERE email = :frm"),
        {"to": to, "frm": frm},
    )


def upgrade() -> None:
    _rename(_OLD, _NEW)


def downgrade() -> None:
    _rename(_NEW, _OLD)
