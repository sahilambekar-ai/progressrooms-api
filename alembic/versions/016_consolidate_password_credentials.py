"""Consolidate password credentials in users; retire the legacy auth mirror.

OTP challenges already use user_otps (hashed, expiring and attempt limited).
Existing users.password_hash wins, matching the previous login behavior.
Unrecognized providers and ambiguous legacy-only passwords abort before changes.
"""
import uuid

from alembic import op
import sqlalchemy as sa

revision = "016_consolidate_passwords"
down_revision = "015_meetings_and_support"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # Prevent legacy application writers racing the backfill and table removal.
        bind.execute(sa.text("LOCK TABLE users, user_auth_methods IN ACCESS EXCLUSIVE MODE"))
    metadata = sa.MetaData()
    users = sa.Table("users", metadata, autoload_with=bind)
    legacy = sa.Table("user_auth_methods", metadata, autoload_with=bind)
    unknown = bind.scalar(sa.select(sa.func.count()).select_from(legacy).where(
        sa.or_(legacy.c.auth_type.not_in(["PASSWORD", "OTP"]), legacy.c.auth_type.is_(None))
    ))
    if unknown:
        raise RuntimeError("Unsupported legacy authentication providers; migrate them before consolidating credentials.")

    # Preflight every candidate before writing: never choose arbitrarily between hashes.
    candidates = bind.execute(sa.select(
        legacy.c.user_id, sa.func.min(legacy.c.credential_hash).label("password_hash"),
        sa.func.count(sa.distinct(legacy.c.credential_hash)).label("hash_count"),
    ).join(users, users.c.id == legacy.c.user_id).where(
        legacy.c.auth_type == "PASSWORD", legacy.c.credential_hash.is_not(None),
        sa.or_(users.c.password_hash.is_(None), users.c.password_hash == ""),
    ).group_by(legacy.c.user_id)).mappings().all()
    if any(row["hash_count"] > 1 for row in candidates):
        raise RuntimeError("Ambiguous legacy password credentials; resolve duplicate hashes before migration.")
    for row in candidates:
        bind.execute(users.update().where(users.c.id == row["user_id"]).values(password_hash=row["password_hash"]))
    op.drop_table("user_auth_methods")


def downgrade():
    legacy = op.create_table(
        "user_auth_methods",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("auth_type", sa.String(50), nullable=False),
        sa.Column("credential_hash", sa.String(255), nullable=True),
        sa.Column("otp_code", sa.String(10), nullable=True),
        sa.Column("otp_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_user_auth_methods_user_id", "user_auth_methods", ["user_id"])
    bind = op.get_bind()
    users = sa.Table("users", sa.MetaData(), autoload_with=bind)
    for row in bind.execute(sa.select(users.c.id, users.c.password_hash, users.c.created_at, users.c.updated_at).where(
        users.c.password_hash.is_not(None), users.c.password_hash != "",
    )).mappings():
        bind.execute(legacy.insert().values(
            id=uuid.uuid4(), user_id=uuid.UUID(str(row["id"])), auth_type="PASSWORD",
            credential_hash=row["password_hash"], created_at=row["created_at"], updated_at=row["updated_at"],
        ))
    # Consumed/legacy plaintext OTP codes are intentionally never resurrected.
