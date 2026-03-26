from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from ama.config import project_root
from ama.scale_engine.anomaly import AnomalyFlag
from ama.scale_engine.criticality import CriticalityResult
from ama.scale_engine.scorer import ConfidenceResult


def append_decision(
    table_key: str,
    decision: str,
    confidence: ConfidenceResult,
    criticality: CriticalityResult,
    anomaly_flags: list[AnomalyFlag],
    contract_id: str,
    approved_by: str,
    approved_at: str,
) -> None:
    path = project_root() / "audit_trail.jsonl"
    payload: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "table_key": table_key,
        "decision": decision,
        "confidence_score": confidence.score,
        "confidence_reason": confidence.reason,
        "criticality_score": criticality.score,
        "criticality_reason": criticality.reason,
        "anomaly_flags": [{"level": f.level, "name": f.name, "reason": f.reason} for f in anomaly_flags],
        "contract_id": contract_id,
        "approved_by": approved_by,
        "approved_at": approved_at,
        "primary_reason": anomaly_flags[0].reason if anomaly_flags else confidence.reason,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception as exc:  # noqa: BLE001
        logging.warning("Failed writing audit trail entry: %s", exc)
