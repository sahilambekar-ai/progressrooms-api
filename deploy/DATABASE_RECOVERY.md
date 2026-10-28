# Database migrations, backup and recovery

## Schema and rollout

Migration `034_support_indexes` renames the existing organization support index to
`ix_support_tickets_organization_id` and adds `ix_support_tickets_student_id` for
immutable student-ownership lookups. It changes no rows. PostgreSQL operations run
in a transaction with a 5-second lock timeout and 60-second statement timeout.
These are ordinary index operations: schedule a quiet maintenance window and
measure table size first. A timeout or conflicting manual index rolls the whole
migration back. Do not blindly mark an unexpectedly drifted installation current;
inspect its existing indexes before reconciling it. The deployment never does this
reconciliation automatically.

Fresh installations run the complete migration chain. CI now runs `alembic check`
after upgrading PostgreSQL 16, plus populated-schema/constraint/restore tests.
Alembic's ordinary comparison covers tables, columns/types, indexes, unique and
foreign-key definitions; zero drift does not imply matching every server default,
all business rules, permissions or production contents. Existing ORM/client
validation supplies many values that have no server default.

A migration success is not permission to run an arbitrary older API image against
the new database. Revision 033 explicitly refuses downgrade because old code
cannot verify Argon2 credentials or respect session revocations. The current 034
index reversal is safe and tested back to 033; it does not make 033 reversible.
Use a compatible forward fix when possible. Never remove a migration's refusal or
manually stamp a lower revision to bypass it.

Historical migrations contain destructive downgrades: table/column removal drops
data, 004 collapses old role distinctions, 005 intentionally has a no-op downgrade,
016 reconstructs password mirrors without resurrecting OTPs, 020 can clear studio
teacher codes, 021 disables legacy Zoom authorization, 023 substitutes capacities,
024 truncates long taglines, and 032 does not undo superseded reminder states or
WhatsApp disabling. A historical downgrade is not a lossless rollback strategy.
Inventory: `audit-evidence/.../database-review/rollback-inventory.json` in the parent
workspace. Downgrade tests do not execute these historical data-loss operations.

## Before migration

For post-migration failure behavior and private-field key requirements, also see
[RUNTIME_PRIVACY_OPERATIONS.md](RUNTIME_PRIVACY_OPERATIONS.md).

The UAT deployment now requires `backup-db.sh` in its release bundle and runs it
before `alembic upgrade head`. It invokes the PostgreSQL container's own `pg_dump`
with its existing environment, custom format, no owner/ACL restoration, restricted
file permissions and a SHA256 checksum. It refuses an existing destination, checks
the archive header/table of contents, and aborts the deployment if any step fails.
It does not expose credentials in arguments or log database contents. A TOC check
is not a restore drill. Schema changes still require a reviewed maintenance plan;
pg_dump makes a consistent database snapshot while writers continue, so writes
after that snapshot would be lost if restoring it.

For an existing Compose environment, using its exact project/env/files:

```sh
bash deploy/backup-db.sh /private/backup/database.dump \
  docker compose -p uat --env-file .env -f compose.yaml -f compose.api.yaml
(cd /private/backup && sha256sum -c database.dump.sha256)
```

Keep the backup outside a publicly served directory. The script does not enable a
scheduler, off-host copy, encryption or retention policy. The database includes
personal data, password hashes and encrypted provider tokens. Protect the backup
with encryption/access controls and restrict logs/artifacts. Keep compatible code,
configuration and the separate encryption/signing keys securely recoverable.
Back up `api_uploads`, `api_websites`, `api_data`, configuration, roles/grants and
required external records separately: a single-database dump is not a coherent
whole-service or cluster/global-role backup.

## Restore drill and disaster recovery

1. Verify the checksum and archive source, choose a fresh isolated PostgreSQL 16
   database, and use matching PostgreSQL 16 client tools. Never restore an untrusted
   archive or overwrite the running database merely to test a backup.
2. Provision its owner/role independently. Supply connection settings using normal
   `PGHOST`, `PGPORT`, `PGUSER`, `PGDATABASE` and a protected `PGPASSFILE` (do not put
   passwords in shell history). Restore into the new empty database:

   ```sh
   pg_restore --exit-on-error --single-transaction --no-owner --no-acl \
     --dbname "$PGDATABASE" /private/backup/database.dump
   ```
3. Run `alembic current` and `alembic check` using the restored database's explicit
   application URLs. Verify row counts/content hashes, constraints/index validity,
   money/receipt ledgers, password verification and session versions. Apply grants
   from the controlled role definition. Analyze restored tables before comparing
   query performance. A pre-release snapshot should be checked against its matching
   release metadata, not current head.
4. Start compatible API/workers with email/payments/Zoom disabled in the drill.
   Test authenticated journeys and uploads against restored file volumes. Reconcile
   post-snapshot payments with provider records before resuming capture/fulfillment;
   reconcile notification queues before sending anything. Avoid duplicate charges,
   replayed messages, or granting access from incomplete ledgers.
5. For actual disaster recovery, stop all writers, preserve the failed system for
   investigation, restore coherent database/files/config to replacement resources,
   rotate authentication signing keys to invalidate tokens from the restored past,
   reconcile provider state and verify access. Change routing only after validation.
   Do not automatically restore over production on an application health-check failure.

Recovery objectives and ownership must be explicit in deployment operations.
Daily off-host encrypted backups plus pre-migration archives, retention matched to
privacy requirements, periodic restoration drills and WAL/PITR for tighter RPO are
operational work still to be enabled/verified on the host. Local results establish
neither production retention nor a production RPO/RTO.

## Reproduce local verification

Use a dedicated disposable database account allowed to create/drop schemas and
create a disposable database. Never point this at production. Install matching
PostgreSQL 16 clients and run from the API repository:

```sh
APP_ENV=test DATABASE_REVIEW_POSTGRES_URL=postgresql://audit@127.0.0.1:65436/review \
DATABASE_REVIEW_PG_BIN=/path/to/postgresql16/bin \
.venv/bin/python -m pytest tests/test_database_review_postgres.py -q

APP_ENV=test PYTHONPATH=. \
DATABASE_REVIEW_POSTGRES_URL=postgresql://audit@127.0.0.1:65436/review \
.venv/bin/python scripts/review_database_queries.py --output /private/tmp/query-plans.json
```

Tests create random `review_*` schemas and a `restore_*` database, clean them up,
exercise the actual deployment backup script via a local Compose adapter and
compare every table in the owned schema. Synthetic restored records include
support conversations, payment amounts/idempotency, nested webhook JSON, Argon2
passwords, session versions and auth counters. They also check checksum, private
permissions, overwrite refusal and failed-backup cleanup. No live credentials,
provider requests or application databases are used.
