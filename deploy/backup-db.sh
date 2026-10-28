#!/usr/bin/env bash
# Usage: backup-db.sh /private/backup/database.dump docker compose [options...]
# Connection credentials remain in the existing postgres container environment.
set -euo pipefail
umask 077
output="${1:?Backup destination required}"
shift
(( $# > 0 )) || { echo 'Compose command required' >&2; exit 1; }
[[ ! -e "$output" && ! -e "$output.sha256" ]] || { echo 'Backup destination already exists' >&2; exit 1; }
mkdir -p "$(dirname "$output")"
temporary=$(mktemp "$output.partial.XXXXXX")
trap 'rm -f "$temporary"' EXIT
"$@" exec -T postgres sh -ec 'exec pg_dump --format=custom --no-owner --no-acl -U "$POSTGRES_USER" -d "$POSTGRES_DB"' > "$temporary"
test -s "$temporary"
# Validate the archive header/TOC before publishing. Restoration is a separate drill.
"$@" exec -T postgres pg_restore --list < "$temporary" > /dev/null
ln "$temporary" "$output"
rm "$temporary"
# Hash the basename so verification still works after copying the backup directory.
(cd "$(dirname "$output")" && sha256sum "$(basename "$output")") > "$output.sha256"
echo 'Database archive and checksum saved; restore verification is still required.'
