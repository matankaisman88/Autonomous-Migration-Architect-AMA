FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml .
COPY dbt_project.yml /opt/ama_defaults/dbt_project.yml
COPY src/ src/
COPY sample_data/ sample_data/

RUN pip install --no-cache-dir -e ".[dev]" \
    && pip install --no-cache-dir fastapi uvicorn[standard] websockets

COPY docker/api_entrypoint.sh /entrypoint.sh
RUN sed -i 's/\r$//' /entrypoint.sh && chmod +x /entrypoint.sh

EXPOSE 8000
ENTRYPOINT ["/entrypoint.sh"]
