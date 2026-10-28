#!/usr/bin/env bash
set -euo pipefail

release="${1:?Commit SHA required}"
[[ "$release" =~ ^[0-9a-f]{40}$ ]] || exit 1
cd "${DEPLOY_ROOT:-/opt/progressrooms/uat}"
# Serialize API deployments, including manual retries on the server.
exec 9>.api-deploy.lock
flock -n 9 || { echo 'An API deployment is already running'; exit 1; }
bundle="incoming/api-$release"
test -s api.env
test -s postgres.env
test -s .env
test -s "$bundle/compose.api.yaml"
test -s "$bundle/backup-db.sh"
test -s "$bundle/api-image.tar.gz"

backup="backups/api-$(date -u +%Y%m%dT%H%M%SZ)-$release"
mkdir -p "$backup"
chmod 700 "$backup"
for file in compose.api.yaml api-image.env; do
  if [[ -f "$file" ]]; then cp -p "$file" "$backup/"; fi
done

docker load --input "$bundle/api-image.tar.gz"
export API_IMAGE_TAG="$release"
compose=(docker compose -p uat --env-file .env -f compose.yaml -f "$bundle/compose.api.yaml" --project-directory "$PWD")
"${compose[@]}" config --quiet
"${compose[@]}" up -d --wait --wait-timeout 120 postgres
# A database archive must succeed before any migration runs. This is a logical
# database backup; uploads/keys and full recovery are covered by the runbook.
bash "$bundle/backup-db.sh" "$backup/database.dump" "${compose[@]}"
# Migrations run once, before changing the running API container.
"${compose[@]}" run --rm --no-deps api alembic upgrade head
if ! "${compose[@]}" up -d --no-deps --wait --wait-timeout 180 api; then
  echo "API readiness failed after migration; writers are stopped for compatible forward recovery." >&2
  "${compose[@]}" stop api zoom-maintenance meeting-email-worker || true
  exit 1
fi
if ! "${compose[@]}" up -d --no-deps zoom-maintenance meeting-email-worker ||    ! curl --fail --silent --show-error --max-time 5 http://127.0.0.1:8100/ready; then
  echo "Post-migration worker/readiness check failed; writers are stopped for recovery." >&2
  "${compose[@]}" stop api zoom-maintenance meeting-email-worker || true
  exit 1
fi

# Keep the previous tag/compose in backups; never modify Web's .env.
cp "$bundle/compose.api.yaml" compose.api.yaml
cp "$bundle/setup-uat-validation.sh" setup-uat-validation.sh
chmod 700 setup-uat-validation.sh
umask 077
printf 'API_IMAGE_TAG=%s\n' "$release" > api-image.env.tmp
mv api-image.env.tmp api-image.env
rm "$bundle/api-image.tar.gz"
echo "API UAT deployed: $release"
