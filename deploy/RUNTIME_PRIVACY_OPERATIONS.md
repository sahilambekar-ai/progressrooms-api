# Runtime, privacy and recovery operations

This runbook records local engineering checks. It does not establish production
readiness, a legal retention schedule, regulatory compliance or a WCAG certification.

## Privacy safeguards and rollout

New/changed bank account values use versioned Fernet ciphertext (`fin1:`) in the
existing column. Owner and super-admin responses show only a mask and at most the
last four characters. Ciphertext itself is not serialized. Shared environments
refuse new bank writes without `FINANCIAL_DATA_ENCRYPTION_KEY`; malformed configured
keys fail settings validation. Local/test mode has an explicitly domain-separated
fallback for synthetic development. Use an independently generated Fernet key in
shared environments, store it in the existing protected `api.env`/secret store,
and back it up separately from both database archives and authentication signing
keys. Never paste it into a ticket, terminal recording or source control.

Existing plaintext is not silently migrated on startup. After a verified backup,
configure the independent key, run the dry-run, review counts, then apply during a
quiet window with compatible code:

```sh
python scripts/encrypt_bank_accounts.py          # counts only; no writes
python scripts/encrypt_bank_accounts.py --apply  # explicit single-transaction migration
python scripts/encrypt_bank_accounts.py          # verify plaintext_rows == 0
```

Run these in the API container/environment with `PYTHONPATH=.`. The tool streams
rows in batches, locks rows for apply, validates existing ciphertext with the
configured key, and is idempotent. New values accept at most 100 ASCII letters,
digits, spaces or hyphens so encrypted values fit the existing column. Invalid
legacy values need deliberate correction, not truncation. Wrong/missing keys do
not fall back to treating ciphertext as plaintext. Preserve the key across restarts;
rotation needs a separately tested decrypt/re-encrypt procedure with the old key.
Restoring a database without its matching key cannot restore readable financial
fields. Encrypting current rows does not remove plaintext from older backups.

Studio edits of another user's personal profile now require both prior-consent
attestation and a reference. This covers student name/contact/address/location,
and teacher name/contact/email/username/address/location/country changes. Actual
changes are compared, so unchanged fields and studio-local notes/status/assignment
do not require an unrelated attestation. Self-edits remain self-service. The audit
row commits atomically with the update and contains actor, subject, studio, time,
changed field names and a consent reference, not old/new personal values. The
reference is free text: enter a request/ticket ID, not private details. This is an
accountable staff attestation, not proof that the affected individual consented;
retain evidence in the approved request system. Profile data is global across
studio relationships; the UI states that scope before staff changes it.

## Data inventory and request handling

| Data | Location / purpose | Boundary / handling |
|---|---|---|
| Identity/contact/location, country, role/status | users; service/account administration | Self-service plus permitted staff; personal staff changes now attested |
| Password hashes/session versions/OTP hashes | users, user_otps, auth_rate_limits | Never include in a data export; access revocation tested; OTPs expire but historical row retention still needs a policy |
| Managed teacher credentials | encrypted recoverable password field | Owner reconfirmation; no-store; tightly restrict support access; reset is preferable to distributing credentials |
| Consent/audit records | audit_logs | Registration policy version/audience/time; profile attestation fields; privileged operational access |
| Memberships, assignments, notes, classes, attendance | organization and teaching tables | Tenant/role checks; notes can contain sensitive material and need minimization |
| Orders, payments, provider IDs, billing snapshots | commerce/SaaS/platform tables | Reconcile and preserve justified ledger records; do not export another person's transactions |
| Bank/tax/business settings | organization_settings | Bank fields newly encrypted/masked; tax/contact fields remain sensitive personal/business data |
| Provider tokens and OAuth candidates | Zoom tables | Separate token key; expired candidates are cleared by maintenance; reconnect/revoke on departure |
| Support messages and email activity/body previews | support/communication tables | Restricted views; free-text content and provider errors may contain personal data |
| Uploaded media and generated/public websites | file volumes + website metadata | Publication is disclosure; verify person authorization; unpublishing does not retract copies/search caches |
| Browser credentials/preferences | local/session storage | Shared-device sign-out; logout/reset revoke sessions; storage remains exposed to any same-origin script compromise |
| Health metrics | bounded SQLite samples | No identity/body/token fields; 32-day automatic metric retention |

The current product offers profile correction and administrative soft deletion,
not a comprehensive self-service export/erasure workflow. Existing privacy contact
links remain the request entry point. The operator must implement and verify the
following process rather than treat a soft-delete response as erasure:

1. Verify the requester's identity through an existing authenticated channel; do
   not collect extra identity documents without a reason. Record request kind,
   scope, reference, owner and due date in the controlled request system. Never ask
   for a password/OTP or full payment credentials.
2. For access/export, assemble the verified subject's account and relevant
   studio/teaching, attendance, purchase, support and consent records through scoped
   queries. Exclude secrets, internal security material and other people's data;
   review shared conversations/notes. Deliver through a protected channel and
   expire any export copy. The repository does not yet automate this full export.
3. For correction, obtain the person's approval of exact changes and retain its
   reference. Use self-service where available; staff attestation is required for
   personal changes. Re-verify login identities as needed; do not substitute a
   database edit for an account-verification process.
4. For deletion/withdrawal, first revoke access and provider authorization where
   appropriate. Soft deletion retains personal/historical rows and files. Determine
   records that must be retained, document why and for how long, anonymize or remove
   the remainder with a reviewed procedure, handle public artifacts and third-party
   records, and record completion/exceptions. No blanket hard-delete job is enabled.
5. Define retention/expiry for expired OTPs/OAuth metadata, support, notes, logs,
   audit records, financial records, uploads, exports and backups. The code does not
   currently enforce a universal schedule. Legal/accounting/privacy owners must
   establish justified categories and deletion holds before automated purging.
   Test procedures against a disposable restore, including shared-user/tenant and
   financial-ledger integrity, before enabling a live schedule.

Children/guardian verification, actual subprocessor arrangements and transfers,
privacy contact responsiveness, incident ownership, request deadlines and legal
retention remain operator review. No actual user records were exported or erased
by this audit. Review old logs/backups separately for historical plaintext bank
fields or previously logged OTPs; changing current code does not purge old copies.

## Runtime and observability

`/health` is liveness. `/ready` performs a bounded database check and returns 503
without connection details on failure. Compose and deployment checks now use
readiness. Readiness does not certify workers, email/Zoom/payment availability,
correct schema revision, backups or downstream delivery.

Requests receive a generated `X-Request-ID`. Request events contain method,
matched route template, status and duration; query strings, request/response bodies,
credentials, raw object paths and supplied IDs are omitted. Generic failures return
a request reference and log exception type rather than exception text. Default
Uvicorn access logging is disabled in `start-api.sh` to avoid restoring raw callback
query strings through another log stream. Streaming failures, provider activity
records, reverse proxies and existing historical logs require separate operational
review. Worker failures log record IDs and error types, not tracebacks that may
contain SQL/provider payloads. API responses use no-store/nosniff/same-origin
referrers. Root and mounted API docs/OpenAPI are disabled in shared modes.

Web responses add nosniff, same-origin referrers/embedding and a limited CSP for
frame ancestors/base/object sources; production adds HSTS without changing sibling
subdomain policy. This is not a complete script-src CSP or an XSS certification.
Razorpay/Zoom browser flows pass with the headers in local controlled tests; verify
real provider integrations and reverse-proxy headers before rollout.

Route telemetry is bounded and privacy-minimized, but error logs alone are not an
alerting system. Configure and test alerts for readiness failure, 5xx rate/latency,
worker cycle failure/queue delay/UNCERTAIN deliveries, provider authorization errors,
backup failures/age, disk/memory/pool pressure and failed deployments. Verify
recipients, escalation and a quiet-hours policy on the real host. No external
monitoring/notification service was contacted or configured here.

## Resources and performance limits

Password hash/verify work now runs in a separate four-operation-per-worker thread
limiter; cancellation does not abandon unbounded CPU threads. Multiply its memory
budget across configured API workers. API lifespan and worker shutdown dispose
owned database pools; telemetry cancellation is protected even when lifespan exits
with an exception. The website editor no longer allocates unused inline-preview
blobs, caps retained preview URLs at five and releases downloads promptly/unmount;
old evicted preview URLs may need regenerating if a tab is reloaded. Calendar
scroll timeouts are cancelled/replaced and removed on unmount.

The local resource soak uses real PostgreSQL but in-process ASGI, a bounded
100-class synthetic catalog, 3,000 requests/concurrency10 and no live providers.
Tracemalloc substantially distorts performance; its run is memory evidence only.
The separate untraced run and password-work microbenchmark are not a production
capacity commitment. Browser mount/unmount cycles use controlled API responses.
Paint/layout samples are local lab observations, not real-user Core Web Vitals,
physical-device or assistive-technology certification. Full dashboard surfaces,
real payment/conferencing integration, multi-hour concurrency and production-sized
write/worker workloads remain deployment/performance verification.

## Deployment recovery

See [database recovery](DATABASE_RECOVERY.md) and [auth recovery](AUTH_SECURITY.md).
Backup failure prevents migration. Failed migration prevents new API activation.
Failure after migration/readiness/worker start stops API and both writers, preserves
the incoming image/archive and leaves release metadata unchanged. It deliberately
does not put an incompatible old binary on a newer schema. Choose a compatible
forward fix or an explicit coherent restore. Database/files/config/provider-state
reconciliation and key recovery remain essential. Six simulated phase tests use
fake Docker/curl to exercise control flow; real Docker/host failure drills have not
been run because Docker is unavailable locally.

Reproduce local verification with the documented PostgreSQL16 test URLs and:
`pytest -q`, `pytest tests/test_deployment_recovery.py -q`,
`APP_ENV=test PYTHONPATH=. python scripts/profile_runtime.py`, and
`APP_ENV=test PYTHONPATH=. python scripts/profile_password_work.py`.
Use `PROFILE_TRACE_FRAMES=0` / a separate `PROFILE_OUTPUT` for untraced latency.
Web: build with the isolated `NEXT_DIST_DIR=.next-audit-item7` and test API URL,
then `PLAYWRIGHT_PRODUCTION=1 PLAYWRIGHT_CHANNEL=chrome npm run test:browser`.
Keep production secrets and actual personal data out of audit evidence.
