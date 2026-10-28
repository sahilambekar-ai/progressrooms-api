"""Adopt an exact, already-created revision 031 schema in a local database only."""
import importlib.util
from pathlib import Path
import sys

import sqlalchemy as sa

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
PREVIOUS = "030_zoom_host_identity"
REVISION = "031_communication_schema"
LEGACY_COLUMN = "whatsapp_completely_disabled"


def legacy_setting_column(table_name, column):
    return (table_name == "platform_communication_settings"
            and column["name"] == LEGACY_COLUMN
            and isinstance(column["type"], sa.Boolean)
            and column["nullable"] is False
            and column.get("default") in (None, "true", "1"))


def frozen_schema():
    spec = importlib.util.spec_from_file_location("frozen_communication", ROOT / "alembic/versions/031_communication_schema.py")
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    metadata = sa.MetaData()

    class Recorder:
        def create_table(self, name, *columns):
            return sa.Table(name, metadata, *columns)

        def create_index(self, name, table, columns, unique=False):
            sa.Index(name, *(metadata.tables[table].c[column] for column in columns), unique=unique)

    migration.op = Recorder()
    migration.upgrade()
    return metadata


def differences(inspector, metadata, dialect):
    problems = []
    existing = set(inspector.get_table_names())
    for name, table in metadata.tables.items():
        if name not in existing:
            problems.append(f"{name}: missing table")
            continue
        actual = {c["name"]: c for c in inspector.get_columns(name)}
        extra = set(actual) - set(table.c.keys())
        allowed_extra = {key for key in extra if legacy_setting_column(name, actual[key])}
        missing = set(table.c.keys()) - set(actual)
        if missing or extra - allowed_extra:
            problems.append(f"{name}: columns differ (missing={sorted(missing)}, unexpected={sorted(extra - allowed_extra)})")
        for column in table.c:
            if column.name not in actual:
                continue
            found = actual[column.name]
            # PostgreSQL reflects FLOAT as DOUBLE PRECISION; both are 64-bit here.
            float_match = isinstance(column.type, sa.Float) and isinstance(found["type"], sa.Float) and getattr(found["type"], "precision", None) in (None, 53)
            if not float_match and str(column.type.compile(dialect=dialect)) != str(found["type"].compile(dialect=dialect)):
                problems.append(f"{name}.{column.name}: type differs")
            if dialect.name == "postgresql" and isinstance(column.type, sa.DateTime) and column.type.timezone != getattr(found["type"], "timezone", None):
                problems.append(f"{name}.{column.name}: timestamp timezone differs")
            if column.nullable != found["nullable"] or found.get("default") is not None:
                problems.append(f"{name}.{column.name}: nullability/default differs")
        if set(inspector.get_pk_constraint(name)["constrained_columns"]) != {c.name for c in table.primary_key}:
            problems.append(f"{name}: primary key differs")
        expected_fk = {(tuple(f.parent.name for f in constraint.elements), tuple(f.target_fullname for f in constraint.elements), constraint.ondelete) for constraint in table.foreign_key_constraints}
        actual_fk = {(tuple(f["constrained_columns"]), tuple(f'{f["referred_table"]}.{c}' for c in f["referred_columns"]), f.get("options", {}).get("ondelete")) for f in inspector.get_foreign_keys(name)}
        if expected_fk != actual_fk:
            problems.append(f"{name}: foreign keys differ")
        expected_indexes = {(i.name, tuple(c.name for c in i.columns), bool(i.unique)) for i in table.indexes}
        actual_indexes = {(i["name"], tuple(i["column_names"]), bool(i["unique"])) for i in inspector.get_indexes(name)}
        if expected_indexes != actual_indexes:
            problems.append(f"{name}: indexes differ")
        if inspector.get_unique_constraints(name) or inspector.get_check_constraints(name):
            problems.append(f"{name}: unexpected constraints")
    return problems


def reconcile(connection):
    inspector = sa.inspect(connection)
    if "alembic_version" not in inspector.get_table_names():
        return False
    revisions = connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalars().all()
    if revisions != [PREVIOUS]:
        return False
    metadata = frozen_schema()
    if not set(metadata.tables).intersection(inspector.get_table_names()):
        return False  # Normal Alembic upgrade will create all tables.
    problems = differences(inspector, metadata, connection.dialect)
    if problems:
        raise RuntimeError("Existing communication schema cannot be adopted:\n" + "\n".join(problems))
    legacy = next((c for c in inspector.get_columns("platform_communication_settings") if c["name"] == LEGACY_COLUMN), None)
    if legacy and legacy.get("default") is None:
        # Keep the retired column and all existing values. New ORM inserts omit it.
        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        operations = Operations(MigrationContext.configure(connection))
        with operations.batch_alter_table("platform_communication_settings") as batch:
            batch.alter_column(LEGACY_COLUMN, existing_type=sa.Boolean(), existing_nullable=False, server_default=sa.true())
    result = connection.execute(sa.text("UPDATE alembic_version SET version_num=:new WHERE version_num=:old"), {"new": REVISION, "old": PREVIOUS})
    if result.rowcount != 1:
        raise RuntimeError("Migration revision changed during verification")
    return True


def main():
    from app.core.config import settings
    for url in (settings.DATABASE_URL, settings.SYNC_DATABASE_URL):
        if sa.engine.make_url(url).host not in {"localhost", "127.0.0.1", "::1"}:
            raise RuntimeError("Schema adoption is restricted to local databases")
    engine = sa.create_engine(settings.SYNC_DATABASE_URL)
    try:
        with engine.begin() as connection:
            if connection.dialect.name == "postgresql":
                connection.execute(sa.text("SELECT pg_advisory_xact_lock(31032026)"))
                if sa.inspect(connection).has_table("alembic_version"):
                    connection.execute(sa.text("LOCK TABLE alembic_version IN EXCLUSIVE MODE"))
            adopted = reconcile(connection)
        print("Verified existing revision 031 tables; adopted without changing their data." if adopted else "No schema adoption needed.")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
