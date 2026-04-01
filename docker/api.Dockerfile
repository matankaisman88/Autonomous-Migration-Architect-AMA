FROM python:3.11-slim

WORKDIR /app

# pyodbc requires the unixODBC shared library (libodbc.so.2) at runtime, plus
# an actual SQL Server ODBC driver (ODBC Driver 18).
#
# NOTE:
# In some build environments the Microsoft apt repo signature is rejected.
# For local dev containers we allow insecure repository access and
# unauthenticated package installs to make the driver available.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        unixodbc unixodbc-dev \
        curl ca-certificates gnupg2 \
    && rm -rf /var/lib/apt/lists/* \
    && curl -sSL https://packages.microsoft.com/config/debian/12/packages-microsoft-prod.deb -o /tmp/packages-microsoft-prod.deb \
    && dpkg -i /tmp/packages-microsoft-prod.deb \
    && rm -f /tmp/packages-microsoft-prod.deb \
    && apt-get update -o Acquire::AllowInsecureRepositories=true -o Acquire::AllowDowngradeToInsecureRepositories=true \
    && ACCEPT_EULA=Y apt-get install -y --no-install-recommends --allow-unauthenticated msodbcsql18 \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml .
COPY dbt_project.yml /opt/ama_defaults/dbt_project.yml
COPY src/ src/
COPY sample_data/ sample_data/
# Live JSONL uses the same distribution as ``tools/generate_kfar_supply.py`` (multi-table discovery).
COPY tools/generate_kfar_supply.py tools/generate_kfar_supply.py

RUN pip install --no-cache-dir -e ".[dev,sqlserver]" \
    && pip install --no-cache-dir fastapi uvicorn[standard] websockets

COPY docker/api_entrypoint.sh /entrypoint.sh
RUN sed -i 's/\r$//' /entrypoint.sh && chmod +x /entrypoint.sh

EXPOSE 8000
ENTRYPOINT ["/entrypoint.sh"]
