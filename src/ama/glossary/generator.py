"""
Main entry point: orchestrates co-occurrence mining + optional LLM translation.
"""

from __future__ import annotations

from ama.glossary.cooccurrence import cooccurrence_candidates, mine_cooccurrences
from ama.glossary.llm_translate import translate_rtl_tokens
from ama.glossary.models import GlossaryCandidate, GlossaryGenerationResult


def generate_glossary_from_logs(
    sql_log_paths: list[Path],
    ddl_columns: list[str],
    *,
    env_filter: str | None = "prod",
    min_cooccurrence_count: int = 3,
    llm_enabled: bool = True,
    max_records: int = 0,
) -> GlossaryGenerationResult:
    """
    Two-stage glossary generation from SQL logs.

    Stage 1 — Co-occurrence mining (always):
        Find RTL tokens and DDL columns that appear in the same SQL queries.
        Rank by frequency. High-frequency pairs become high-confidence candidates.

    Stage 2 — LLM translation (if AMA_OPENAI_API_KEY is set and llm_enabled=True):
        For RTL tokens not resolved by co-occurrence, ask GPT to translate them
        given the DDL column list as context.

    Parameters
    ----------
    sql_log_paths : list of Path to JSONL SQL log files
    ddl_columns : list of target DDL column names (English)
    env_filter : filter log rows by env field (default "prod"; None = all)
    min_cooccurrence_count : minimum co-occurrences to count as a signal (default 3)
    llm_enabled : set False to skip LLM even if API key is present
    max_records : cap on records read per file (0 = unlimited)
    """
    result = GlossaryGenerationResult(ddl_columns_used=list(ddl_columns))

    # --- Stage 1: Co-occurrence ---
    raw_pairs = mine_cooccurrences(
        sql_log_paths,
        ddl_columns,
        env_filter=env_filter,
        max_records=max_records,
    )
    result.rtl_tokens_found = len(raw_pairs)

    ranked = cooccurrence_candidates(
        raw_pairs,
        min_count=min_cooccurrence_count,
        top_k=3,
    )

    # Compute max count for normalization
    max_count = max(
        (hits[0][1] for hits in ranked.values() if hits),
        default=1,
    )

    resolved_by_cooccurrence: set[str] = set()
    candidates: list[GlossaryCandidate] = []

    for rtl_token, hits in ranked.items():
        if not hits:
            continue
        best_ddl, best_count = hits[0]
        # Normalize confidence: log-scaled frequency ratio (0.3 floor, 0.95 ceiling)
        raw_conf = min(0.95, 0.3 + 0.65 * (best_count / max(max_count, 1)))
        alternatives = [d for d, _ in hits[1:]]
        candidates.append(
            GlossaryCandidate(
                source_term=rtl_token,
                target_column=best_ddl,
                confidence=round(raw_conf, 4),
                method="cooccurrence",
                co_occurrence_count=best_count,
                alternatives=alternatives,
            )
        )
        resolved_by_cooccurrence.add(rtl_token)

    result.rtl_tokens_resolved = len(resolved_by_cooccurrence)

    # --- Stage 2: LLM for unresolved RTL tokens ---
    unresolved = [t for t in raw_pairs if t not in resolved_by_cooccurrence]

    if llm_enabled and unresolved:
        llm_results = translate_rtl_tokens(unresolved, ddl_columns)
        if llm_results:
            result.llm_used = True
            result.llm_tokens_translated = len(llm_results)
            for rtl_token, info in llm_results.items():
                tgt = info.get("target_column", "")
                conf = float(info.get("confidence", 0.0))
                expl = str(info.get("explanation", ""))
                if not tgt or conf < 0.3:
                    continue
                # Check if co-occurrence also found something for this token
                existing = next((c for c in candidates if c.source_term == rtl_token), None)
                if existing:
                    # Blend: LLM refines co-occurrence
                    if tgt == existing.target_column:
                        existing.confidence = min(0.97, (existing.confidence + conf) / 2 + 0.05)
                        existing.method = "cooccurrence+llm"
                        existing.llm_explanation = expl
                    # else: co-occurrence wins, just note the disagreement
                else:
                    candidates.append(
                        GlossaryCandidate(
                            source_term=rtl_token,
                            target_column=tgt,
                            confidence=round(conf, 4),
                            method="llm",
                            llm_explanation=expl,
                        )
                    )
                    result.rtl_tokens_resolved += 1
        else:
            if unresolved:
                result.warnings.append(
                    f"{len(unresolved)} RTL token(s) not resolved by co-occurrence; "
                    "LLM translation not available (no API key or call failed)."
                )
    elif unresolved and not llm_enabled:
        result.warnings.append(
            f"{len(unresolved)} RTL token(s) found in logs but not resolved by co-occurrence. "
            "Set AMA_OPENAI_API_KEY and re-run to attempt LLM translation."
        )

    result.candidates = candidates
    return result
