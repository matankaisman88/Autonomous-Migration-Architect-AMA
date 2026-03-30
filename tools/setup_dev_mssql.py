"""
Setup local SQL Server for the Kfar Supply demo.

Idempotent: safe to run multiple times; will re-create the `kfar_supply` database
inside the container on each run.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
KFAR_DIR = ROOT / "sample_data" / "kfar_supply"
KFAR_DDL_DIR = KFAR_DIR / "ddl"


CONTAINER_NAME = "ama-mssql-dev"
DEFAULT_DRIVER = "ODBC Driver 18 for SQL Server"
TARGET_DB = "kfar_supply"


@dataclass(frozen=True)
class SqlConnectionConfig:
    driver: str
    server: str
    database: str
    uid: str
    pwd: str

    def to_odbc_connection_string(self) -> str:
            return (
                f"DRIVER={{{self.driver}}};"
                f"SERVER={self.server};"
                f"DATABASE={self.database};"
                f"UID={self.uid};"
                f"PWD={self.pwd};"
                "Encrypt=yes;"
                "TrustServerCertificate=yes;"
            )


def _log(prefix: str, msg: str) -> None:
    print(f"[{prefix}] {msg}", flush=True)


def _run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    _log("DOCKER", " ".join(cmd))
    return subprocess.run(
        cmd,
        check=check,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _docker_container_exists(name: str) -> bool:
    try:
        proc = subprocess.run(
            ["docker", "inspect", name],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        return proc.returncode == 0
    except Exception as exc:
        # If docker itself isn't available, fail loudly.
        raise RuntimeError(f"Failed checking docker container: {exc}") from exc


def _docker_container_running(name: str) -> bool:
    try:
        proc = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", name],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if proc.returncode != 0:
            return False
        return proc.stdout.strip().lower() == "true"
    except Exception as exc:
        raise RuntimeError(f"Failed checking docker container state: {exc}") from exc


def _docker_container_ip(name: str, *, prefer_network: str = "bridge") -> str:
    """
    Resolve a container's IP address from `docker inspect`.

    This is useful because other containers may not reach `SERVER=localhost`;
    they usually need the SQL Server container IP (or a shared network alias).
    """
    try:
        proc = subprocess.run(
            ["docker", "inspect", name],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or "docker inspect failed")

        data = json.loads(proc.stdout)
        if not isinstance(data, list) or not data:
            raise RuntimeError("Unexpected docker inspect output")

        networks = (
            data[0]
            .get("NetworkSettings", {})
            .get("Networks", {})  # e.g. {"bridge": {...}, "my_net": {...}}
        )
        if not isinstance(networks, dict):
            raise RuntimeError("Unexpected docker inspect network structure")

        if prefer_network in networks:
            prefer_ip = networks[prefer_network].get("IPAddress")
            if isinstance(prefer_ip, str) and prefer_ip.strip():
                return prefer_ip.strip()

        # Fallback: return the first non-empty IP we find.
        for net in networks.values():
            if not isinstance(net, dict):
                continue
            ip = net.get("IPAddress")
            if isinstance(ip, str) and ip.strip():
                return ip.strip()
    except Exception as exc:
        _log("DOCKER", f"Failed to resolve {name} IP; falling back to localhost: {exc}")

    return "localhost"


def ensure_mssql_container(*, sa_password: str) -> None:
    """
    Ensure Docker container `ama-mssql-dev` exists and is running.
    """
    exists = _docker_container_exists(CONTAINER_NAME)
    if not exists:
        _log(
            "DOCKER",
            f"Creating container {CONTAINER_NAME} (port 1433 -> container 1433).",
        )
        try:
            _run(
                [
                    "docker",
                    "run",
                    "-e",
                    "ACCEPT_EULA=Y",
                    "-e",
                    f"MSSQL_SA_PASSWORD={sa_password}",
                    "-p",
                    "1433:1433",
                    "--name",
                    CONTAINER_NAME,
                    "-d",
                    "mcr.microsoft.com/mssql/server:2022-latest",
                ]
            )
        except Exception as exc:
            raise RuntimeError(f"Failed creating SQL Server container: {exc}") from exc
        return

    if not _docker_container_running(CONTAINER_NAME):
        _log("DOCKER", f"Starting existing container {CONTAINER_NAME}.")
        try:
            _run(["docker", "start", CONTAINER_NAME])
        except Exception as exc:
            raise RuntimeError(f"Failed starting SQL Server container: {exc}") from exc
        return

    _log("DOCKER", f"Container {CONTAINER_NAME} already running.")


def _iter_json_files(dir_path: Path) -> Iterable[Path]:
    for p in sorted(dir_path.glob("*.json")):
        if p.is_file():
            yield p


def _wait_for_sqlserver_ready(*, master_conn: SqlConnectionConfig) -> None:
    """
    Retry for up to 30 seconds, attempting a connection to `master`.
    """
    try:
        import pyodbc  # type: ignore
    except ImportError as exc:
        raise RuntimeError("pyodbc is required for tools/setup_dev_mssql.py") from exc

    deadline = time.time() + 30.0
    attempt = 0
    conn_str = master_conn.to_odbc_connection_string()

    while time.time() < deadline:
        attempt += 1
        _log("SQL", f"Readiness attempt {attempt} (timeout up to 30s)...")
        try:
            conn = pyodbc.connect(conn_str, timeout=3)
            try:
                cur = conn.cursor()
                cur.execute("SELECT 1")
                cur.fetchone()
                _log("SQL", "SQL Server is ready.")
                return
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
        except Exception as exc:
            _log("SQL", f"Not ready yet: {exc}")
            time.sleep(1.0)

    raise TimeoutError("SQL Server did not become ready within 30 seconds.")


def _execute_sql(*, conn_str: str, sql_text: str) -> None:
    try:
        import pyodbc  # type: ignore
    except ImportError as exc:
        raise RuntimeError("pyodbc is required for tools/setup_dev_mssql.py") from exc

    conn = None
    try:
        _log("SQL", f"Executing SQL ({len(sql_text)} chars).")
        conn = pyodbc.connect(conn_str, timeout=5)
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(sql_text)
    except Exception as exc:
        _log("SQL", f"SQL execution failed: {exc}")
        raise
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass


def _derive_schema_table_from_filename(path: Path) -> tuple[str, str] | None:
    """
    Heuristic: dbo_orders.json -> ("dbo", "orders")
    """
    stem = path.stem
    if "_" not in stem:
        return None
    schema, table = stem.split("_", 1)
    if not schema or not table:
        return None
    return schema, table


def _infer_sql_type(column_name: str) -> str:
    """
    Lightweight type inference for seed table creation fallback.
    """
    c = (column_name or "").lower()
    if c.endswith("_id") or c == "id":
        return "INT"
    if "amount" in c or "total" in c or "price" in c or "rate" in c or "vat" in c:
        return "DECIMAL(18,2)"
    if "date" in c or c.endswith("_at") or c.endswith("_time") or c.endswith("_dt"):
        return "DATETIME2"
    if "status" in c:
        return "NVARCHAR(50)"
    return "NVARCHAR(255)"


def _synthetic_value(column_name: str) -> Any:
    c = (column_name or "").lower()
    if c.endswith("_id") or c == "id":
        return 1001
    if "amount" in c or "total" in c or "price" in c or "rate" in c or "vat" in c:
        return 123.45
    if "date" in c or c.endswith("_at") or c.endswith("_time") or c.endswith("_dt"):
        return "2026-01-15T10:30:00"
    if "status" in c:
        return "active"
    if "email" in c:
        return "alice@example.com"
    if "phone" in c:
        return "050-1234567"
    return "sample"


def _sql_literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    # Escape single quotes for SQL literal.
    s = str(value).replace("'", "''")
    return f"N'{s}'"


def _ensure_database_fresh(*, master_conn: SqlConnectionConfig) -> None:
    master_cs = master_conn.to_odbc_connection_string()
    _log("SQL", f"Recreating database {TARGET_DB}.")
    sql = f"""
IF DB_ID(N'{TARGET_DB}') IS NOT NULL
BEGIN
    ALTER DATABASE [{TARGET_DB}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE;
    DROP DATABASE [{TARGET_DB}];
END;
CREATE DATABASE [{TARGET_DB}];
"""
    _execute_sql(conn_str=master_cs, sql_text=sql)


def _load_json(path: Path) -> dict[str, Any] | list[Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"Failed parsing JSON {path}: {exc}") from exc


def _inject_schema_and_collect_columns(*, db_conn: SqlConnectionConfig) -> dict[str, list[str]]:
    """
    Inject schema into the target DB and return {table_key: [columns...]} for seeding.
    """
    target_cs = db_conn.to_odbc_connection_string()
    specs: dict[str, list[str]] = {}

    if not KFAR_DDL_DIR.is_dir():
        raise FileNotFoundError(f"Missing directory: {KFAR_DDL_DIR}")

    for p in _iter_json_files(KFAR_DDL_DIR):
        if p.name == "kfar_manifest.json":
            continue

        payload = _load_json(p)
        if not isinstance(payload, dict):
            _log("SQL", f"Skipping {p.name}: expected JSON object.")
            continue

        schema_table = _derive_schema_table_from_filename(p)
        if schema_table is None:
            _log("SQL", f"Skipping {p.name}: cannot derive schema/table from filename.")
            continue
        schema, table = schema_table
        table_key = f"{schema}.{table}"

        ddl_text: str | None = None
        if "ddl" in payload:
            ddl_text = payload.get("ddl") if isinstance(payload.get("ddl"), str) else None
        elif "sql" in payload:
            ddl_text = payload.get("sql") if isinstance(payload.get("sql"), str) else None

        if ddl_text:
            _log("SQL", f"Injecting schema from {p.name} into {table_key}.")
            _execute_sql(conn_str=target_cs, sql_text=ddl_text)
            # If ddl/sql provided, we may not know columns for seeding; fall back to columns if available.

        columns = payload.get("columns") if isinstance(payload.get("columns"), list) else None
        if isinstance(columns, list) and all(isinstance(c, str) for c in columns):
            col_list = [str(c).strip() for c in columns if str(c).strip()]
            specs[table_key] = col_list

            # If no executable ddl/sql was found, generate a minimal CREATE TABLE and seed.
            if not ddl_text:
                _log(
                    "SQL",
                    f"No ddl/sql in {p.name}; generating CREATE TABLE from columns for {table_key}.",
                )
                cols_defs = []
                for col in col_list:
                    sql_type = _infer_sql_type(col)
                    cols_defs.append(f"{_quote_ident(col)} {sql_type} NULL")
                _execute_sql(
                    conn_str=target_cs,
                    sql_text=(
                        f"IF SCHEMA_ID(N'{schema}') IS NULL EXEC(N'CREATE SCHEMA [{schema}]'); "
                        f"IF OBJECT_ID(N'{schema}.{table}', N'U') IS NOT NULL DROP TABLE [{schema}].[{table}]; "
                        f"CREATE TABLE [{schema}].[{table}] ({', '.join(cols_defs)});"
                    ),
                )
        else:
            # If no columns list exists either, and ddl_text didn't exist, we cannot proceed.
            if not ddl_text:
                raise ValueError(
                    f"{p.name} has neither 'ddl'/'sql' nor 'columns' fields; cannot inject or seed."
                )

    return specs


def _quote_ident(ident: str) -> str:
    safe = str(ident).replace("]", "]]")
    return f"[{safe}]"


def _seed_tables(*, db_conn: SqlConnectionConfig, table_columns: dict[str, list[str]]) -> None:
    target_cs = db_conn.to_odbc_connection_string()
    if not table_columns:
        raise RuntimeError("No table columns discovered for seeding.")

    for table_key, cols in table_columns.items():
        schema, table = table_key.split(".", 1)
        _log("SEED", f"Seeding one row into {table_key}.")

        if not cols:
            _log("SEED", f"Skipping {table_key}: empty columns list.")
            continue

        values = [_synthetic_value(col) for col in cols]
        insert_cols = ", ".join(_quote_ident(c) for c in cols)
        insert_vals = ", ".join(_sql_literal(v) for v in values)
        sql = f"INSERT INTO {_quote_ident(schema)}.{_quote_ident(table)} ({insert_cols}) VALUES ({insert_vals});"
        _execute_sql(conn_str=target_cs, sql_text=sql)


def _run_kfar_seeding_script() -> None:
    """
    Call tools/generate_kfar_supply.py as a module.
    """
    script_path = ROOT / "tools" / "generate_kfar_supply.py"
    if not script_path.is_file():
        raise FileNotFoundError(f"Missing: {script_path}")

    spec = importlib.util.spec_from_file_location("generate_kfar_supply", script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Failed importing {script_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["generate_kfar_supply"] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]

    if hasattr(module, "main") and callable(module.main):
        _log("SEED", "Running tools/generate_kfar_supply.py.main()")
        module.main()
    else:
        # If no main() exists, just no-op with an explicit error.
        raise RuntimeError(f"{script_path} does not expose a callable main()")


def _update_env_file(*, sa_password: str, server: str = "localhost") -> str:
    """
    Update (or create) .env with MSSQL_CONNECTION_STRING.
    Returns the final connection string.
    """
    try:
        from dotenv import set_key  # type: ignore
    except ImportError as exc:
        raise RuntimeError("python-dotenv is required for tools/setup_dev_mssql.py") from exc

    env_path = ROOT / ".env"
    conn = SqlConnectionConfig(
        driver=DEFAULT_DRIVER,
        server=server,
        database=TARGET_DB,
        uid="sa",
        pwd=sa_password,
    ).to_odbc_connection_string()

    if not env_path.exists():
        env_path.write_text("", encoding="utf-8")

    set_key(str(env_path), "MSSQL_CONNECTION_STRING", conn, quote_mode="never")
    _log("ENV", f"Updated {env_path} MSSQL_CONNECTION_STRING.")
    return conn


def main() -> None:
    sa_password = os.environ.get("MSSQL_SA_PASSWORD", "").strip()
    if not sa_password:
        raise ValueError(
            "Missing MSSQL_SA_PASSWORD in environment. Example: "
            "set MSSQL_SA_PASSWORD=YourStrongPassword"
        )

    ensure_mssql_container(sa_password=sa_password)
    api_server_host = _docker_container_ip(CONTAINER_NAME, prefer_network="bridge")

    master_conn = SqlConnectionConfig(
        driver=DEFAULT_DRIVER,
        server="localhost",
        database="master",
        uid="sa",
        pwd=sa_password,
    )
    _wait_for_sqlserver_ready(master_conn=master_conn)

    # Fresh DB each run.
    _ensure_database_fresh(master_conn=master_conn)

    target_conn = SqlConnectionConfig(
        driver=DEFAULT_DRIVER,
        server="localhost",
        database=TARGET_DB,
        uid="sa",
        pwd=sa_password,
    )

    table_columns = _inject_schema_and_collect_columns(db_conn=target_conn)

    # Required by spec: run the dataset generation logic.
    _run_kfar_seeding_script()

    # Seed into SQL Server so the demo has usable data.
    _seed_tables(db_conn=target_conn, table_columns=table_columns)

    # The generated connection string is consumed by the API container, where
    # `localhost` does NOT refer to the SQL Server container.
    final_conn = _update_env_file(sa_password=sa_password, server=api_server_host)
    _log("ENV", f"Final connection string:\n{final_conn}")


if __name__ == "__main__":
    main()

