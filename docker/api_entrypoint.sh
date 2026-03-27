#!/bin/sh
set -e
mkdir -p /app/dbt_project/models/ama_generated /app/dbt_project/target/checkpoints
if [ ! -f /app/dbt_project/dbt_project.yml ] && [ -f /opt/ama_defaults/dbt_project.yml ]; then
  cp /opt/ama_defaults/dbt_project.yml /app/dbt_project/dbt_project.yml
fi
exec uvicorn ama.api.main:app --host 0.0.0.0 --port 8000
