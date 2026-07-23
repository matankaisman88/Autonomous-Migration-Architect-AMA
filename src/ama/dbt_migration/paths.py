from __future__ import annotations

from pathlib import Path


def default_models_output_dir(dbt_project_dir: Path) -> Path:
    """Primary generated-models directory (matches dbt_project.yml model-paths)."""
    return (dbt_project_dir / "models" / "ama_generated").resolve()


def find_model_sql_path(*, dbt_project_dir: Path, model_name: str) -> Path | None:
    """
    Resolve a model SQL file under ``dbt_project_dir/models``.

    Prefers ``models/ama_generated/`` when multiple matches exist (legacy flat layout).
    """
    models_dir = dbt_project_dir / "models"
    if not models_dir.is_dir():
        return None
    matches = sorted(models_dir.rglob(f"{model_name}.sql"))
    if not matches:
        return None
    for path in matches:
        if "ama_generated" in path.parts:
            return path
    return matches[0]


def model_sql_path_for_write(*, dbt_project_dir: Path, model_name: str) -> Path:
    """Target path for writing model SQL (existing file or default ama_generated layout)."""
    existing = find_model_sql_path(dbt_project_dir=dbt_project_dir, model_name=model_name)
    if existing is not None:
        return existing
    return default_models_output_dir(dbt_project_dir) / f"{model_name}.sql"
