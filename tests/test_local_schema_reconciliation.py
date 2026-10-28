import sqlalchemy as sa
import pytest

from scripts.reconcile_local_communication_schema import PREVIOUS, REVISION, frozen_schema, reconcile


def database(*, partial=False):
    engine = sa.create_engine("sqlite:///:memory:")
    metadata = frozen_schema()
    for name in ("users", "organizations", "class_occurrences"):
        sa.Table(name, metadata, sa.Column("id", sa.Uuid(), primary_key=True))
    with engine.begin() as connection:
        metadata.create_all(connection)
        connection.execute(sa.text("CREATE TABLE alembic_version (version_num VARCHAR(64) NOT NULL)"))
        connection.execute(sa.text("INSERT INTO alembic_version VALUES (:revision)"), {"revision": PREVIOUS})
        connection.execute(sa.text("INSERT INTO platform_communication_settings VALUES ('test', 0, 1, NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"))
        if partial:
            connection.execute(sa.text("DROP TABLE pending_material_changes"))
    return engine


def test_adopts_verified_schema_preserving_data_and_is_repeatable():
    engine = database()
    with engine.begin() as connection:
        assert reconcile(connection)
        assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == REVISION
        assert connection.scalar(sa.text("SELECT id FROM platform_communication_settings")) == "test"
        assert not reconcile(connection)
    engine.dispose()


@pytest.mark.parametrize("partial", [True, False])
def test_rejects_partial_or_changed_schema_without_stamping(partial):
    engine = database(partial=partial)
    with engine.begin() as connection:
        if not partial:
            connection.execute(sa.text("ALTER TABLE platform_communication_settings ADD COLUMN unexpected TEXT"))
        with pytest.raises(RuntimeError, match="cannot be adopted"):
            reconcile(connection)
        assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == PREVIOUS
        assert connection.scalar(sa.text("SELECT count(*) FROM platform_communication_settings")) == 1
    engine.dispose()


def test_empty_schema_uses_normal_migrations():
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        assert not reconcile(connection)
    engine.dispose()


def test_preserves_legacy_whatsapp_values_and_defaults_new_records():
    engine = database()
    with engine.begin() as connection:
        connection.execute(sa.text("ALTER TABLE platform_communication_settings ADD COLUMN whatsapp_completely_disabled BOOLEAN NOT NULL DEFAULT 0"))
        # SQLite needs a table rebuild to remove the fixture default.
        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        with Operations(MigrationContext.configure(connection)).batch_alter_table("platform_communication_settings") as batch:
            batch.alter_column("whatsapp_completely_disabled", existing_type=sa.Boolean(), server_default=None)
        assert reconcile(connection)
        assert connection.scalar(sa.text("SELECT whatsapp_completely_disabled FROM platform_communication_settings WHERE id='test'")) == 0
        connection.execute(sa.text("INSERT INTO platform_communication_settings (id, whatsapp_enabled, email_enabled, created_at, updated_at) VALUES ('new', 0, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"))
        assert connection.scalar(sa.text("SELECT whatsapp_completely_disabled FROM platform_communication_settings WHERE id='new'")) == 1
    engine.dispose()
