from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)
router = APIRouter()


class IngestRunRequest(BaseModel):
    sql_glob: str
    ddl_manifest: str
    output_dir: str
    output_name: str = "ama_report"
    glossary: str | None = None


@router.post("/run")
def ingest_run(body: IngestRunRequest) -> dict[str, Any]:
    """Run `ama-ingest run` as subprocess to reproduce dashboard ingest orchestration."""
    cmd = [
        "ama-ingest",
        "run",
        "--sql-glob",
        body.sql_glob,
        "--ddl-manifest",
        body.ddl_manifest,
        "--output-dir",
        body.output_dir,
        "--output-name",
        body.output_name,
    ]
    if body.glossary:
        cmd.extend(["--glossary", body.glossary])
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        return {
            "return_code": proc.returncode,
            "stdout": proc.stdout[-4000:],
            "stderr": proc.stderr[-4000:],
            "output_json": str((Path(body.output_dir) / f"{body.output_name}.json").resolve()),
        }
    except FileNotFoundError as exc:
        raise HTTPException(status_code=400, detail=f"ama-ingest not found: {exc}") from exc
    except Exception as exc:
        logger.exception("ingest run failed")
        raise HTTPException(status_code=500, detail=f"Ingest run failed: {exc}") from exc

