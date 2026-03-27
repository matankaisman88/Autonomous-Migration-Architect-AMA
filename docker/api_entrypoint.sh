#!/bin/sh
set -e
mkdir -p /app/dbt_project/models /app/dbt_project/target/checkpoints
exec uvicorn ama.api.main:app --host 0.0.0.0 --port 8000
