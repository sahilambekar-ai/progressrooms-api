#!/usr/bin/env sh
set -eu
# No-op unless explicitly configured. Runs before accepting traffic on every deployment.
python -m app.validation_seed
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --no-proxy-headers
