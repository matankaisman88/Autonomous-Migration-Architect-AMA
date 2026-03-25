from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, Field, field_validator


class DbtAutomationConfig(BaseModel):
    manifest_path: Path
    usage_csv_path: Path
    models_output_dir: Path
    dbt_project_dir: Path
    dlq_dir: Path
    max_attempts: int = 3

    @field_validator("manifest_path", "usage_csv_path")
    @classmethod
    def _must_exist(cls, value: Path) -> Path:
        if not value.is_file():
            raise ValueError(f"required file not found: {value}")
        return value

    @field_validator("models_output_dir", "dbt_project_dir", "dlq_dir")
    @classmethod
    def _normalize_path(cls, value: Path) -> Path:
        return value

    @field_validator("max_attempts")
    @classmethod
    def _validate_attempts(cls, value: int) -> int:
        if value < 1:
            raise ValueError("max_attempts must be >= 1")
        return value


def load_config_from_env() -> DbtAutomationConfig:
    return DbtAutomationConfig(
        manifest_path=Path(os.environ["AMA_DBT_MANIFEST_PATH"]).expanduser().resolve(),
        usage_csv_path=Path(os.environ["AMA_DBT_USAGE_CSV_PATH"]).expanduser().resolve(),
        models_output_dir=Path(os.environ.get("AMA_DBT_MODELS_OUTPUT_DIR", "models/ama_generated")).expanduser().resolve(),
        dbt_project_dir=Path(os.environ.get("AMA_DBT_PROJECT_DIR", ".")).expanduser().resolve(),
        dlq_dir=Path(os.environ.get("AMA_DBT_DLQ_DIR", "out/dbt_dlq")).expanduser().resolve(),
        max_attempts=int(os.environ.get("AMA_DBT_MAX_ATTEMPTS", "3")),
    )
