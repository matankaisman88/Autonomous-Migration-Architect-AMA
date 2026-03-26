from __future__ import annotations

from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, field_validator, model_validator


class TargetDialect(str, Enum):
    SNOWFLAKE = "snowflake"
    BIGQUERY = "bigquery"
    DUCKDB = "duckdb"
    REDSHIFT = "redshift"


class MigrationStatus(str, Enum):
    PENDING = "PENDING"
    GENERATING = "GENERATING"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    RUNNING = "RUNNING"
    FIXING = "FIXING"
    HITL_REQUIRED = "HITL_REQUIRED"
    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


class RunnerFinalStatus(str, Enum):
    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    FAILURE = "FAILURE"


class ModelRunState(str, Enum):
    PENDING = "PENDING"
    SUCCESS = "SUCCESS"
    HITL_REQUIRED = "HITL_REQUIRED"
    REJECTED = "REJECTED"


class MappingSource(str, Enum):
    GLOSSARY = "Glossary"
    TRANSLITERATION = "Transliteration"


class MappingRow(BaseModel):
    hebrew_name: str
    english_alias: str
    source: MappingSource
    confidence: float | None = None
    confidence_score: float = 0.0
    criticality_score: float = 0.0
    warning_flags: list[str] = Field(default_factory=list)


class BrokenLineageStub(BaseModel):
    table_key: str
    business_rationale: str
    identified_columns: list[str] = Field(default_factory=list)


class ModelArtifact(BaseModel):
    table_key: str
    model_name: str
    sql: str
    schema_yml: str
    mapping_rows: list[MappingRow] = Field(default_factory=list)
    generation_mode: str = "legacy"
    user_modified: bool = False
    generation_confidence: float = 0.0
    schema_agent_reasoning: str = ""
    dbt_agent_reasoning: str = ""
    ai_telemetry: list[dict[str, Any]] = Field(default_factory=list)
    fallback_reason: str = ""
    auth_error: bool = False
    rate_limit_error: bool = False
    complexity_score: float = 0.0
    translation_rationale: str = ""
    mapping_decision_tag: str = "HUMAN_REQUIRED"
    review_required: bool = False
    is_stub: bool = False
    # Populated when bounded self-healing fails and HITL is required.
    critical_reason: str = ""
    # QA-reported SQL rejection reasons from sqlglot / syntax validation.
    qa_error_reasons: list[str] = Field(default_factory=list)
    # Telemetry for bounded correction attempts (kept for transparency/debugging).
    self_heal_attempts: list[dict[str, Any]] = Field(default_factory=list)


class CheckpointAArtifact(BaseModel):
    wave_summary: str
    generated_models: list[ModelArtifact] = Field(default_factory=list)
    mapping_rows: list[MappingRow] = Field(default_factory=list)
    review_required_tables: list[str] = Field(default_factory=list)
    ai_telemetry: list[dict[str, Any]] = Field(default_factory=list)
    fallback_active: bool = False
    auth_error_detected: bool = False
    rate_limit_detected: bool = False
    model_insights: dict[str, Any] = Field(default_factory=dict)
    model_insights_path: str = ""
    synthetic_dataset_paths: dict[str, str] = Field(default_factory=dict)


class RunAttempt(BaseModel):
    attempt: int
    command: str
    return_code: int
    stdout: str = ""
    stderr: str = ""


class ModelMetadata(BaseModel):
    model_name: str
    relation_name: str
    materialized: str = "view"
    ddl_columns: list[str] = Field(default_factory=list)
    usage_columns: list[str] = Field(default_factory=list)
    selected_columns: list[str] = Field(default_factory=list)
    unique_key: str | list[str] | None = None
    model_path: str = ""
    config: dict[str, Any] = Field(default_factory=dict)


class ModelExecutionTrace(BaseModel):
    model_name: str
    state: ModelRunState = ModelRunState.PENDING
    fix_loop_count: int = 0
    records_impacted: int | None = None
    last_error_log: str = ""
    attempts: list[RunAttempt] = Field(default_factory=list)


class DlqRecord(BaseModel):
    original_payload: dict[str, Any]
    error_reason: str
    error_stage: str
    timestamp: str
    run_id: str


class ExecutionResult(BaseModel):
    run_id: UUID = Field(default_factory=uuid4)
    status: RunnerFinalStatus = RunnerFinalStatus.FAILURE
    duration_ms: int = 0
    fix_loop_count: dict[str, int] = Field(default_factory=dict)
    records_impacted: dict[str, int | None] = Field(default_factory=dict)
    model_results: list[ModelExecutionTrace] = Field(default_factory=list)
    dlq_records: list[DlqRecord] = Field(default_factory=list)
    last_error: str = ""
    summary_status: str = "REVIEW_REQUIRED"


class CheckpointBHistoryItem(BaseModel):
    timestamp: str
    error_snippet: str
    action_taken: str


class CheckpointBArtifact(BaseModel):
    model_name: str
    current_sql: str
    error_log: str
    attempt_history: list[dict[str, Any]] = Field(default_factory=list)
    failed_sql: str = ""
    suggested_sql: str = ""
    fix_agent_error_analysis: str = ""
    fix_confidence: float = 0.0
    tokens_used: int = 0
    is_fallback_active: bool = False
    auth_error: bool = False
    rate_limit_error: bool = False


class MigrationSessionState(BaseModel):
    target_dialect: TargetDialect
    status: MigrationStatus = MigrationStatus.PENDING
    checkpoint_approved: bool = False
    max_fix_attempts: int = 3
    attempts: list[RunAttempt] = Field(default_factory=list)
    review_required: list[str] = Field(default_factory=list)
    wave_telemetry: list[dict[str, Any]] = Field(default_factory=list)

    @field_validator("max_fix_attempts")
    @classmethod
    def _validate_attempts(cls, value: int) -> int:
        if value < 1:
            raise ValueError("max_fix_attempts must be >= 1")
        return value

    @model_validator(mode="after")
    def _validate_run_gate(self) -> "MigrationSessionState":
        if self.status in {MigrationStatus.RUNNING, MigrationStatus.FIXING, MigrationStatus.SUCCESS}:
            if not self.checkpoint_approved:
                raise ValueError("Checkpoint A must be approved before execution states")
        return self


class GenerationJobStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class JobProgressEvent(BaseModel):
    """
    Append-only progress event for UI.

    Notes:
    - This file is safe to be partially written; consumers should treat invalid lines as “skip”.
    - `event_type` is intentionally a free-form string (MODEL_START, MODEL_DONE, etc.).
    """

    event_type: str
    timestamp: str
    model_name: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class GenerationJobArtifact(BaseModel):
    """
    Persistent job state for async Checkpoint A generation.

    This enables the Streamlit UI to stay responsive and resume/reload progress.
    """

    job_id: str
    status: GenerationJobStatus = GenerationJobStatus.PENDING
    created_at: str
    updated_at: str
    # Progress counters for the ops console.
    total_models: int = 0
    completed_models: int = 0
    failed_models: int = 0
    # Error details when status == FAILED.
    error: str = ""
    # Minimal args snapshot for debugging/resume.
    report_path: str = ""
    glossary_path: str = ""
    target_dialect: str = ""
    dbt_models_dir: str = ""
    dbt_project_dir: str = ""
    checkpoint_dir: str = ""
