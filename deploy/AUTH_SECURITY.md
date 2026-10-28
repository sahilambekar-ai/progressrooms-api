# Authentication security transition (migration 033)

Apply only through the normal dev → UAT → production promotion process. This
patch does not authorize a deployment or push.

## Behavior

New passwords use Argon2id (19 MiB, two iterations, one lane, individual salts).
Registration/reset accept 8–128 Unicode code points; no trimming or Unicode
normalization occurs. Login still accepts older short credentials, so a legacy
six-character reset password can be used until its next change.

Successful bcrypt login below 72 UTF-8 bytes upgrades the hash. Inputs of **72
bytes or more** cannot safely prove the original password: the legacy system
stored only that prefix. They are rejected without changing the hash. Use the
existing verified email recovery flow to establish a full-length Argon2 password.
An exact 72-byte prefix is also rejected because it could be a truncated longer
password. Do not bulk-rehash unknown plaintext or silently truncate a password.

JWTs carry an account session version and a unique token ID. Password resets,
studio-managed teacher password changes, and `/auth/logout` atomically increment
that version. This signs out **all devices**. Reset also consumes every existing
OTP for that account and clears any obsolete recoverable teacher credential.
Tokens issued before this release have no version and are rejected; users must
sign in again. Requests already authorized before revocation can finish.

## Abuse controls

Limits count all attempts, including successful ones, and return 429 with
`Retry-After`. Fixed windows allow a boundary burst; keep independent edge limits
for network floods. Counters are shared by workers through PostgreSQL atomic
upserts and survive failures/restarts. Service-level account quotas also cover
activation emails triggered by password login.

| Scope | Quota |
| --- | --- |
| Account password login | 10 / 15 minutes |
| Account OTP verification and password-reset verification together | 10 / 15 minutes |
| Account delivery, shared across registration, activation/login OTP and reset | 5 / 15 minutes and 20 / UTC day |
| Source password login (including developer login) | 30 / 15 minutes |
| Source verification/reset | 30 / 15 minutes |
| Source OTP delivery/registration/forgot password | 20 / 15 minutes |
| All authentication attempts | 10,000 / UTC day |

The global budget precedes source keys and source budgets precede account keys,
bounding new records during account/IP rotation. Rows expire at window end and
are purged through an expiry index on subsequent auth requests. Counters contain
HMAC keys rather than raw emails/IPs; this is pseudonymization, not anonymization.
Counts saturate. Database errors fail closed; there is no unbounded in-memory
fallback. The global ceiling is a conservative capacity guard and can deny
legitimate traffic during a distributed attack; review it against measured demand
before changing it. Do not disable account controls to accommodate a shared proxy.

Source identity comes from the ASGI peer (IPv6 grouped by /64). Configure Uvicorn
proxy trust to accept forwarding only from the actual reverse proxy, and ensure
the proxy overwrites client-supplied forwarding headers. An arbitrary
X-Forwarded-For header is not read by application code. Edge deployment verification
remains necessary; do not trust all proxy sources on a publicly reachable API.

## Rollout and recovery

1. Take a coherent database backup and verify availability of email recovery.
2. Stop/drain API traffic, apply `alembic upgrade head`, and start the compatible
   API, then deploy the web update. Verify login, recovery, logout and 429 headers.
3. Rebuild both images from the patched dependency files. Validate native Argon2
   wheels in the target Linux/Python 3.11 image; local tests do not certify that image.
4. Watch 401/429 and delivery/provider errors without logging passwords, OTPs,
   bearer tokens or raw counter subjects.

Migration 033 only adds a version column (existing accounts start at zero) and
an empty counter table. It does not modify existing hashes or account status.
**Downgrade is intentionally refused**: old code cannot verify newly stored
Argon2 hashes and would accept revoked bearer tokens. Use a compatible forward
fix. Disaster recovery to old code requires a coherent pre-release backup plus
signing-key rotation, and loses post-backup writes; it must be separately planned
and authorized. Restoring only hashes or only schema is unsafe.
