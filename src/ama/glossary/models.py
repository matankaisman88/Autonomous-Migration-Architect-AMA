"""Data models for glossary generation results."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class GlossaryCandidate:
    """One candidate Hebrew→English mapping with provenance."""

    source_term: str  # Hebrew/RTL token as seen in SQL logs
    target_column: str  # Best matching English DDL column name
    confidence: float  # 0.0–1.0: co-occurrence frequency or LLM score
    method: str  # "cooccurrence" | "llm" | "cooccurrence+llm"
    co_occurrence_count: int = 0  # How many queries contained both terms
    llm_explanation: str = ""  # Non-empty only when method includes "llm"
    alternatives: list[str] = field(default_factory=list)  # Other DDL columns seen nearby


@dataclass
class GlossaryGenerationResult:
    """Full result from generate_glossary_from_logs."""

    candidates: list[GlossaryCandidate] = field(default_factory=list)
    rtl_tokens_found: int = 0  # Distinct RTL tokens seen in logs
    rtl_tokens_resolved: int = 0  # Tokens with at least one candidate
    ddl_columns_used: list[str] = field(default_factory=list)
    llm_used: bool = False
    llm_tokens_translated: int = 0
    warnings: list[str] = field(default_factory=list)

    def to_glossary_dict(self) -> dict[str, str]:
        """
        Return the flat Hebrew→English dict in the existing glossary format.
        Only includes candidates with confidence >= min_confidence_floor (0.3).
        Sorted by confidence descending.
        """
        MIN_CONF = 0.3
        out: dict[str, str] = {}
        for c in sorted(self.candidates, key=lambda x: -x.confidence):
            if c.confidence >= MIN_CONF and c.source_term not in out:
                out[c.source_term] = c.target_column
        return out

    def to_export_dict(self) -> dict:
        """
        Full export dict including _meta block for human review.
        Write this to candidate_glossary.json.
        """
        glossary = self.to_glossary_dict()
        meta = {
            "_meta": {
                "generated_by": "ama.glossary",
                "rtl_tokens_found": self.rtl_tokens_found,
                "rtl_tokens_resolved": self.rtl_tokens_resolved,
                "llm_used": self.llm_used,
                "llm_tokens_translated": self.llm_tokens_translated,
                "warnings": self.warnings,
                "candidates": [
                    {
                        "source_term": c.source_term,
                        "target_column": c.target_column,
                        "confidence": round(c.confidence, 4),
                        "method": c.method,
                        "co_occurrence_count": c.co_occurrence_count,
                        "llm_explanation": c.llm_explanation,
                        "alternatives": c.alternatives,
                    }
                    for c in sorted(self.candidates, key=lambda x: -x.confidence)
                ],
            }
        }
        return {**glossary, **meta}
