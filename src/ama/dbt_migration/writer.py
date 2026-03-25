from __future__ import annotations

from pathlib import Path

from ama.dbt_migration.models import ModelArtifact


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
