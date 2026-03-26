from __future__ import annotations

from pathlib import Path

from ama.dbt_migration.models import ModelArtifact


def _write_model_files(*, output_dir: Path, model_name: str, sql: str, schema_yml: str) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    sql_path = output_dir / f"{model_name}.sql"
    schema_path = output_dir / f"{model_name}.schema.yml"
    sql_path.write_text(str(sql or "").rstrip() + "\n", encoding="utf-8")
    if str(schema_yml or "").strip():
        schema_path.write_text(str(schema_yml), encoding="utf-8")
    return sql_path, schema_path


def write_model_artifacts(models_dir: Path, artifacts: list[ModelArtifact]) -> list[Path]:
    models_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for artifact in artifacts:
        sql_path = models_dir / f"{artifact.model_name}.sql"
        yml_path = models_dir / f"{artifact.model_name}.schema.yml"
        sql_header = (
            f"-- review_required: {str(artifact.review_required).lower()}\n"
            f"-- broken_lineage: {str(artifact.is_stub).lower()}\n"
        )
        sql_path.write_text(sql_header + artifact.sql + "\n", encoding="utf-8")
        yml_path.write_text(artifact.schema_yml, encoding="utf-8")
        written.extend([sql_path, yml_path])
    return written
