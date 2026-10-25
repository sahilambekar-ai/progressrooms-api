#!/usr/bin/env bash
set -euo pipefail

release="${1:?Commit SHA required}"
[[ "$release" =~ ^[0-9a-f]{40}$ ]] || exit 1
cd /opt/progressrooms/uat
# Serialize API deployments, including manual retries on the server.
exec 9>.api-deploy.lock
flock -n 9 || { echo 'An API deployment is already running'; exit 1; }
bundle="incoming/api-$release"
test -s api.env
test -s postgres.env
test -s .env
test -s "$bundle/compose.api.yaml"
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
# Migrations run once, before changing the running API container.
"${compose[@]}" run --rm --no-deps api alembic upgrade head
"${compose[@]}" up -d --no-deps --wait --wait-timeout 180 api
"${compose[@]}" up -d --no-deps zoom-maintenance
curl --fail --silent --show-error http://127.0.0.1:8100/health

# Keep the previous tag/compose in backups; never modify Web's .env.
cp "$bundle/compose.api.yaml" compose.api.yaml
cp "$bundle/setup-uat-validation.sh" setup-uat-validation.sh
chmod 700 setup-uat-validation.sh
umask 077
printf 'API_IMAGE_TAG=%s\n' "$release" > api-image.env.tmp
mv api-image.env.tmp api-image.env
rm "$bundle/api-image.tar.gz"
echo "API UAT deployed: $release"
