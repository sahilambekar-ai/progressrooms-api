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
APP_ENV=uat
DATABASE_URL=postgresql+asyncpg://USER:PASSWORD@postgres:5432/DATABASE
SYNC_DATABASE_URL=postgresql://USER:PASSWORD@postgres:5432/DATABASE
SECRET_KEY=<unique-random-key-at-least-32-bytes>
BACKEND_CORS_ORIGINS=["https://uat.progressrooms.com"]
WEBSITE_PUBLIC_ORIGIN=https://uat.progressrooms.com
AUTH_SHOW_OTP_PREVIEW=false
DEV_TEST_LOGIN_ENABLED=false
BREVO_ENABLED=false
```

Shared environments (`uat`, `staging`, `production`) reject default/short/obvious
placeholder signing keys, developer login, OTP previews and mock Zoom at startup.
They require an HTTPS public origin and explicit HTTPS CORS origins. Generate a
unique key with `python -c 'import secrets; print(secrets.token_urlsafe(48))'` in a
private terminal, then place it in the protected environment file. Do not use the
example value. HS384/HS512 require at least 48/64 key bytes respectively. Unknown
environment names are rejected. Local development must explicitly set
`APP_ENV=development`; CI uses `APP_ENV=test` and a synthetic signing key.

Use the actual PostgreSQL values, URL-encoding credentials. Never commit these
files or print secrets in CI. Email OTP delivery requires adding valid Brevo
credentials and sender settings, then enabling Brevo. Email-dependent flows are
not functional while mail is disabled. Payment credentials remain placeholders
until explicitly configured for UAT.

Deployment serializes API releases, loads the image, starts/checks PostgreSQL,
runs `alembic upgrade head` once, then replaces the API service and starts the Zoom maintenance worker. The worker retries provider revocations and removes expired authorization candidates; it shares backend configuration and has no public port. Migration
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


## Automatic validation tenants (UAT and production images)

The image runs `app.validation_seed` before starting the API. It is a no-op by default.
To enable it, install the private environment-specific settings from `private-validation/uat.env`
or `private-validation/prod.env` into the deployment's `api.env` (do not commit these files).
The tracked `.env.validation.example` documents the variable names. Keep each environment's
seed secret unchanged; never use the UAT secret in production. This configuration is a one-time
server setup. Afterward, every API container start automatically adds missing validation data.
Migrations must already have run. The UAT deployment script runs them before starting the image.
No production deployment workflow is added: the production image has the same startup behavior
when the user promotes and deploys it.

Dataset: two standalone teachers with 10 students each; Lotus Studio with an owner,
5 teachers and 20 students; Rhythm Academy with an owner, 4 teachers and 15 students.
Each teaching member has a private course, Monday/Wednesday/Friday timetable, 14 days of
attendance history and 28 days of upcoming classes. Each studio's last two students have
paused/expired enrollment scenarios. Seed users are verified for password login. Optional explicit admin bootstrap is described below.
Synthetic studios have complimentary, unmanaged Core/Studio access, not fabricated Razorpay payments.
All recipients opt out of operational notifications; no phone numbers, real Zoom authorizations,
payment accounts or public courses are created.

The script uses stable IDs, a transaction and a PostgreSQL advisory lock. Re-running it does not
reset passwords, names, enrollment states or existing attendance. It adds missing dates; old data
is retained. To turn off future seed runs, set `VALIDATION_SEED_ENABLED=false`; this preserves
existing validation data and accounts.

Private credentials are regenerated at `/app/data/validation-credentials.md` (mode 0600).
The current UAT compose mounts `/app/data` on the persistent `api_data` volume.
Use an equivalent private persistent volume on production. For manual execution after migration:

```sh
python -m app.validation_seed
```

To generate the credential document from the environment without changing the database:

```sh
python -m app.validation_seed --credentials-only
```

The document lists initial credentials. Passwords changed by testers are not reset by the seed.
Never publish that document as a web asset, CI artifact, Git commit or log output.

## One-time UAT account setup using the matching credential document

Generating a credential document does **not** populate the database. Deploy the
updated API through the normal user-controlled dev → uat promotion first. The
new image must include `ensure_admin` in `app.validation_seed`. The deployment
copies `setup-uat-validation.sh` to `/opt/progressrooms/uat`.

1. Copy the existing private `private-validation/uat.env` from this checkout to
   the VPS as `/opt/progressrooms/uat/validation-uat.env`, keeping it mode 0600.
   This file includes the same seed secret used by yesterday's UAT credentials;
   do not generate a replacement secret. Never use `prod.env` for this step.
2. On the VPS run:

   ```bash
   bash /opt/progressrooms/uat/setup-uat-validation.sh /opt/progressrooms/uat/validation-uat.env
   ```

The helper accepts only `uat`, shares the deployment lock, validates the deployed
image, saves a private backup of `api.env`, and installs only the seed settings
and `APP_ENV=uat`. It seeds the current UAT database, preserving existing records
and passwords, then recreates only the API service with those settings. It does
not delete database volumes or alter production. If the database already has a
super admin under another email, it stops: set `VALIDATION_ADMIN_EMAIL` in the
private configuration to that existing address and rerun. A non-admin account
can never be promoted by this bootstrap.

The dataset contains 4 studios/standalone-teacher workspaces, 4 owners, 9 teachers
and 55 students, with courses and attendance scenarios. All seeded account emails
and passwords in the existing private document remain the same. They use normal
password login and do not need the developer floating controls or OTP previews.
The configured admin email defaults to `admin@test.progressrooms.local`; its
initial password is included near the top of the private credential document.
Bootstrap creates a missing admin or initializes a missing admin password; it
never resets a password or reactivates a disabled account. When an existing admin
has a different password, the server-generated document states that it was
preserved instead of reporting a password that does not work.

Retrieve the authoritative document **after setup** privately:

```bash
cd /opt/progressrooms/uat
umask 077
container=$(docker compose -p uat --env-file .env --env-file api-image.env \
  -f compose.yaml -f compose.api.yaml ps -q api)
docker cp "$container:/app/data/validation-credentials.md" ./uat-credentials.md
chmod 600 ./uat-credentials.md
```

Download that file to your own machine rather than displaying it in logs or CI.
`private-validation/uat-credentials.md` in this checkout lists the same initial
credentials, including admin, but they apply only once bootstrap succeeds. The
production private configuration/document remain separate; a production operator
can explicitly configure the same seed module in their existing production API
deployment. This UAT helper does not define or perform a production deployment.

### Meeting notifications through Zoom Mail

Migration `032_zoom_meeting_mail` adds a per-recipient outbox and supersedes the
old 2-hour/15-minute schedules. WhatsApp controls/sending are retired; historical
columns and tables remain. One reminder is scheduled at class start minus 30
minutes. A class created inside that window receives one immediate reminder;
started classes do not receive a backlog. Cancellations enqueue immediately;
material edits consolidate for 2 minutes, with earlier starts expedited.

The `meeting-email-worker` Compose service runs the scheduler every 15 seconds,
then drains up to 100 queue records with 4 concurrent workers, each using its own
database session. No cron is required. Deploy scripts start this worker after
migration. For other environments, run `python -m app.modules.communication.worker`
as a supervised process using the same backend configuration. `--once` is available
for a manually managed scheduler. Settings:
`MEETING_EMAIL_WORKER_INTERVAL_SECONDS`, `MEETING_EMAIL_CONCURRENCY` (max 10),
`MEETING_EMAIL_BATCH_SIZE` (max 500). Multiple instances use atomic delivery claims;
PostgreSQL scheduler scans use row locks and SKIP LOCKED. Sends sharing a Zoom host
serialize with its token refresh, while different hosts can send concurrently.

Add `email:write:send_msg` to the user-level Zoom OAuth app (or the admin variant
for an account-level grant), then reconnect the sending hosts. Each host needs an
onboarded Zoom Mail Service mailbox and the Zoom license required by the Mail API.
The mailbox associated with the occurrence's connected Zoom host is the sender.
Reconnecting a host may require reconciling existing meetings under the existing
Zoom connection-version policy. No meeting mail falls back to Brevo. The permission is optional for meeting hosting,
but invitations fail clearly without it. Development Zoom sends are SIMULATED.

The queue rechecks class status, time, enrollment validity/credits, teacher
assignment, opt-outs and platform email settings just before sending. Each email
has one recipient; no student address lists are exposed. Invitation links return
to an authenticated class-joining page and never contain a private host start URL.
The attendance Send invite action queues eligible class recipients for staff, or
only the requesting student's own invitation. Repeated requests within one minute
reuse the request. Provider-confirmed sends are SENT; HTTP 429 is retried up to five
attempts. Network/5xx uncertainty and abandoned SENDING claims become UNCERTAIN and
are not automatically resent; inspect Zoom sent mail before manually retrying.
Email activity includes provider failures and simulation status.

Brevo remains responsible for OTP and purchase/payment notifications, with actual
provider responses recorded. Meeting mail uses Zoom's RFC 2822/base64url Mail API:
https://developers.zoom.us/docs/api/mail/ . Do not start a live worker until the
host permissions/mailboxes are configured and the deployment migration is applied.


## Webhook request caps and email data

Commerce, SaaS billing and Zoom webhooks cap raw bodies at 256 KiB before
signature verification or JSON parsing. Oversize bodies return 413, malformed
lengths 400, and compressed/encoded requests 415. Chunked bodies receive the same
streaming cap; a false Content-Length cannot bypass it. Signature bytes and
existing idempotency/replay handling remain unchanged for valid requests.

Apply a matching request-body cap at the reverse proxy for these webhook paths
and verify it after deployment. Other upload routes have different limits; do not
apply a 256 KiB cap to image uploads. Host proxy configuration is user-managed
and has not been changed or inspected by this patch.

Email text is HTML-escaped and subject line breaks removed. Email CTA destinations
require an absolute HTTPS URL without credentials; local development/test may
use HTTP loopback URLs. Configured public origins and CORS values in shared
environments must be HTTPS origins without paths, queries or fragments. These
checks do not certify third-party destinations or fix the separate missing receipt
routes tracked in the audit. OTP provider failures never log codes or provider
response bodies.

## Database safety

UAT now creates a database archive and checksum before migrating. See
[database recovery](DATABASE_RECOVERY.md) for restore drills, migration rollback
limits and remaining host backup/retention requirements. No live restore or
backup schedule is enabled by this source change.

## Runtime and privacy review

See [runtime/privacy operations](RUNTIME_PRIVACY_OPERATIONS.md) for financial-key
configuration and legacy encryption, consent attestations, request/retention
limits, readiness, redacted logs and post-migration failure recovery.
