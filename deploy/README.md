# UAT deployment

Work on `dev`. The user promotes `dev` to `uat`, then `uat` to `main` manually.
Ask for explicit approval before every push. No production deployment is defined.

## Checks and deployment

`.github/workflows/ci-uat.yaml` checks pull requests and pushes to `dev`, `uat`,
and `main`: Python 3.11 tests, PostgreSQL 16 migrations, shell syntax, Docker build.
Only a successful **push to `uat`** starts deployment. PR checks never use SSH
secrets. Both SSH actions verify `VPS_HOST_FINGERPRINT`.

Required API repository secrets:
`VPS_HOST`, `VPS_PORT`, `VPS_USER`, `VPS_HOST_FINGERPRINT`, `VPS_SSH_KEY`,
`VPS_SSH_PASSPHRASE`.

The workflow builds an amd64 image on GitHub, transfers it to
`/opt/progressrooms/uat/incoming/api-<commit>/`, then runs `uat.sh`.
The VPS must have an amd64 CPU, Docker Compose, Bash, flock, curl, and tar.
Deploy user needs Docker and write access to the UAT directory.

Existing server files are `compose.yaml` (Web), `.env` (Web's `IMAGE_TAG`),
and `postgres.env` (`POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`).
PostgreSQL runs as service `postgres` in project `uat` with volume
`uat_postgres_data`; the new Compose file preserves that identity and data.

Before first deployment, create a mode-0600 `api.env` on the VPS containing:

```dotenv
DATABASE_URL=postgresql+asyncpg://USER:PASSWORD@postgres:5432/DATABASE
SYNC_DATABASE_URL=postgresql://USER:PASSWORD@postgres:5432/DATABASE
SECRET_KEY=GENERATE_A_UNIQUE_RANDOM_SECRET
BACKEND_CORS_ORIGINS=["https://uat.progressrooms.com"]
WEBSITE_PUBLIC_ORIGIN=https://uat.progressrooms.com
AUTH_SHOW_OTP_PREVIEW=false
DEV_TEST_LOGIN_ENABLED=false
BREVO_ENABLED=false
```

Use the actual PostgreSQL values, URL-encoding credentials. Never commit these
files or print secrets in CI. Email OTP delivery requires adding valid Brevo
credentials and sender settings, then enabling Brevo. Email-dependent flows are
not functional while mail is disabled. Payment credentials remain placeholders
until explicitly configured for UAT.

Deployment serializes API releases, loads the image, starts/checks PostgreSQL,
runs `alembic upgrade head` once, then replaces only the API service. Migration
failure stops deployment before the API update. API uploads, exports, and data
use persistent Docker volumes. Health checks probe `/health` (process health,
not database readiness). CI separately checks migrations against PostgreSQL.
Database backup/restore and schema compatibility must be considered before later
migrations against populated UAT data; migrations are not automatically undone.

The API binds only to `127.0.0.1:8100`. Web remains on `127.0.0.1:3100`.
Web's `.env` is never rewritten. The successful API tag is stored separately in
`api-image.env`; previous Compose/tag files are retained in `backups/`.

## Host Caddy (separate manual step)

After the first API deployment succeeds, replace only the existing UAT site
block, keeping all other sites as they are:

```caddyfile
uat.progressrooms.com {
    @api path /api/v1 /api/v1/*
    handle @api {
        reverse_proxy 127.0.0.1:8100
    }
    handle {
        reverse_proxy 127.0.0.1:3100
    }
}
```

Preserve `/api/v1`; do not use `handle_path`, which strips the prefix.
Back up `/etc/caddy/Caddyfile`, edit, then validate before reloading:

```bash
sudo caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
sudo systemctl reload caddy
curl --fail http://127.0.0.1:8100/health
curl --fail https://uat.progressrooms.com/api/v1/openapi.json -o /dev/null
curl --head https://uat.progressrooms.com
```

## Operations

After deployment, include both env and Compose files when operating API services:

```bash
cd /opt/progressrooms/uat
docker compose -p uat --env-file .env --env-file api-image.env \
  -f compose.yaml -f compose.api.yaml ps
docker compose -p uat --env-file .env --env-file api-image.env \
  -f compose.yaml -f compose.api.yaml logs --tail 100 api
```

Do not run `down -v` or prune volumes. Images remain available for a manual
application rollback. If a release fails, inspect the workflow and API logs;
the new container may be running but unhealthy. Do not assume the old API was
automatically restored. For a schema-compatible rollback, use a known previous
image tag and its matching Compose file from backups; restart only `api` with
`up -d --no-deps --wait api`. Do not downgrade the database automatically.
