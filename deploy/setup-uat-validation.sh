#!/usr/bin/env bash
# Run on the VPS after deploying the API version containing admin bootstrap.
set -euo pipefail
umask 077

config="${1:?Usage: bash setup-uat-validation.sh /private/path/uat.env}"
[[ "$config" = /* && -f "$config" ]] || { echo 'Supply an existing absolute path to the private UAT seed config.' >&2; exit 1; }
cd /opt/progressrooms/uat
exec 9>.api-deploy.lock
flock -n 9 || { echo 'An API deployment/setup is already running.' >&2; exit 1; }
for file in api.env .env api-image.env compose.yaml compose.api.yaml; do
  test -s "$file" || { echo "Missing UAT deployment file: $file" >&2; exit 1; }
done
compose=(docker compose -p uat --env-file .env --env-file api-image.env -f compose.yaml -f compose.api.yaml)
# Fail before changing api.env if the deployed image does not include this feature.
"${compose[@]}" run --rm --no-deps -T api python -c 'from app.validation_seed import ensure_admin'
python3 - "$config" <<'PY'
import os, sys, tempfile
from datetime import datetime, timezone
from pathlib import Path

allowed = {'VALIDATION_SEED_ENABLED', 'VALIDATION_SEED_PROFILE', 'VALIDATION_SEED_SECRET',
           'VALIDATION_CREDENTIALS_PATH', 'VALIDATION_ADMIN_ENABLED', 'VALIDATION_ADMIN_EMAIL'}
def read(path):
    values = {}
    for raw in Path(path).read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith('#'): continue
        key, sep, value = line.partition('=')
        if not sep: raise SystemExit('Invalid seed config assignment')
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values
incoming = read(sys.argv[1])
if set(incoming) - allowed: raise SystemExit('Private seed config contains unexpected keys')
if incoming.get('VALIDATION_SEED_PROFILE') != 'uat': raise SystemExit('This setup command accepts only the UAT profile')
if incoming.get('VALIDATION_SEED_ENABLED') != 'true' or len(incoming.get('VALIDATION_SEED_SECRET', '')) < 32:
    raise SystemExit('UAT seed must be enabled with the original 32+ character secret')
if incoming.get('VALIDATION_CREDENTIALS_PATH') != '/app/data/validation-credentials.md':
    raise SystemExit('Keep credentials in the private persistent /app/data volume')
if incoming.get('VALIDATION_ADMIN_ENABLED') != 'true' or not incoming.get('VALIDATION_ADMIN_EMAIL'):
    raise SystemExit('Configure VALIDATION_ADMIN_ENABLED=true and VALIDATION_ADMIN_EMAIL')
existing_text = Path('api.env').read_text()
existing = read('api.env')
if existing.get('VALIDATION_SEED_PROFILE') not in {None, '', 'uat'}: raise SystemExit('api.env belongs to another seed profile')
if existing.get('VALIDATION_SEED_SECRET') not in {None, '', incoming['VALIDATION_SEED_SECRET']}:
    raise SystemExit('Seed secret differs from installed config; preserve the original secret and its credential document')
updates = {**incoming, 'APP_ENV': 'uat'}
lines = [line for line in existing_text.splitlines() if line.split('=', 1)[0].strip() not in updates]
content = '\n'.join(lines + [f'{key}={value}' for key, value in updates.items()]) + '\n'
backup = Path(f'api.env.before-validation-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}')
with backup.open('x') as handle: handle.write(existing_text)
backup.chmod(0o600)
fd, temp = tempfile.mkstemp(prefix='.api-env-', dir='.')
try:
    with os.fdopen(fd, 'w') as handle: handle.write(content)
    os.replace(temp, 'api.env')
finally:
    if os.path.exists(temp): os.unlink(temp)
print('UAT seed settings installed; prior api.env saved privately. No secrets printed.')
PY
"${compose[@]}" config --quiet
# Uses the matching private secret and a transaction; existing passwords are preserved.
"${compose[@]}" run --rm --no-deps -T api python -m app.validation_seed
# Persist settings on the running service and future container starts.
"${compose[@]}" up -d --no-deps --wait --wait-timeout 180 api
curl --fail --silent --show-error http://127.0.0.1:8100/health
printf '\nUAT validation accounts ready. Private credential document: /app/data/validation-credentials.md in the api container.\n'
