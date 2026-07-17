"""Adversarial critics for deep research — gap analysis via auxiliary LLM.

In "full" mode, after the base synthesis, we run 2-3 critic prompts against
the markdown to find:
  - Missing perspectives / counter-arguments
  - Shallow sections that need more sources
  - Obvious topical gaps the corpus should cover but doesn't

Each critic returns structured findings.  The loop then generates targeted
follow-up search queries, extracts gap-filling sources, and patches the
final report with a "Gap Analysis" appendix.

No subagents needed — reuses the auxiliary LLM client already wired
in the planner.  No LLM summarisation of source content (raw markdown
append).
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

logger = logging.getLogger("hermes.deep-research.critics")


# ---------------------------------------------------------------------------
# Critics
# ---------------------------------------------------------------------------

CRITIC_PROFILES = [
    {
        "name": "counter-argument",
        "prompt_template": """You are an adversarial research critic reviewing a markdown research report.

Research topic: {query}

Read the following report and identify 2-4 specific perspectives, counter-arguments,
or sources the report is missing. Be concrete — suggest exact search queries
that would fill each gap.

Report:
{report_preview}

Return your answer as JSON with this exact shape:
{{
  "gaps": [
    {{
      "gap": "short description of the missing perspective",
      "severity": "high|medium|low",
      "search_query": "a search query that would find sources for this gap",
      "why_needed": "why this gap matters to the research"
    }}
  ]
}}
Return ONLY the JSON, no preamble, no explanation.""",
    },
    {
        "name": "depth",
        "prompt_template": """You are a depth critic reviewing a research report.

Research topic: {query}

Identify 2-3 topics within the report that are covered too shallowly —
where a reader would want more detail, data, or sources.

Report:
{report_preview}

Return your answer as JSON with this exact shape:
{{
  "gaps": [
    {{
      "gap": "the shallow topic",
      "severity": "high|medium|low",
      "search_query": "a search query for deeper sources",
      "why_needed": "what's missing in the current coverage"
    }}
  ]
}}
Return ONLY the JSON.""",
    },
    {
        "name": "width",
        "prompt_template": """You are a width critic reviewing a research report.

Research topic: {query}

Identify 1-3 adjacent topics or alternative angles the report ignores
but that a reader would reasonably expect. Suggest search queries to
bring in breadth.

Report:
{report_preview}

Return your answer as JSON with this exact shape:
{{
  "gaps": [
    {{
      "gap": "the overlooked angle",
      "severity": "high|medium|low",
      "search_query": "search query to find sources",
      "why_needed": "why this is relevant"
    }}
  ]
}}
Return ONLY the JSON.""",
    },
]


async def run_critics(
    query: str,
    full_markdown: str,
    llm_client: Optional[Any],
    llm_model: Optional[str],
    max_gaps: int = 5,
) -> list[dict]:
    """Run critic profiles against the report and return consolidated findings.

    Parameters
    ----------
    query:
        The original research query.
    full_markdown:
        The assembled markdown (may be truncated for LLM budget).
    llm_client:
        OpenAI-compatible async client, or None to skip.
    llm_model:
        Model name.
    max_gaps:
        Max total gaps to return (across all critics).

    Returns
    -------
    list[dict]:
        Each dict: {gap, severity, search_query, why_needed}
    """
    if llm_client is None:
        logger.info("no LLM client available — skipping critics")
        return []

    # Truncate report preview to fit in context
    preview = full_markdown[:12000]

    all_gaps: list[dict] = []
    seen_queries: set[str] = set()

    for profile in CRITIC_PROFILES:
        prompt = profile["prompt_template"].format(
            query=query,
            report_preview=preview,
        )
        try:
            response = await _call_llm(llm_client, prompt, llm_model)
            findings = _parse_critic_response(response)
            for gap in findings:
                sq = gap.get("search_query", "").strip()
                if sq and sq not in seen_queries:
                    seen_queries.add(sq)
                    all_gaps.append(gap)
        except Exception as exc:
            logger.warning("critic '%s' failed: %s", profile["name"], exc)

    # Sort by severity, deduplicate by query
    severity_order = {"high": 0, "medium": 1, "low": 2}
    all_gaps.sort(key=lambda g: severity_order.get(g.get("severity", "low"), 3))

    return all_gaps[:max_gaps]


def format_gap_report(gaps: list[dict], gap_sources: list[dict]) -> str:
    """Format gaps + any filled sources as a markdown appendix."""
    lines = ["", "---", "## Gap Analysis (Full Mode)", ""]

    if not gaps:
        lines.append("*No significant gaps identified.*")
        return "\n".join(lines)

    for i, gap in enumerate(gaps, 1):
        lines.append(f"### Gap {i}: {gap.get('gap', 'Unknown')}")
        lines.append(f"- **Severity:** {gap.get('severity', 'unknown')}")
        lines.append(f"- **Search query:** `{gap.get('search_query', 'N/A')}`")
        lines.append(f"- **Why needed:** {gap.get('why_needed', 'N/A')}")
        lines.append("")

    if gap_sources:
        lines.append("### Gap-filling Sources")
        for src in gap_sources:
            lines.append(f"- [{src.get('title', 'Source')}]({src.get('url', '#')}) — {src.get('snippet', '')[:200]}")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _call_llm(
    client: Any, prompt: str, model: Optional[str] = None
) -> str:
    kwargs = {
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 1500,
        "temperature": 0.4,
    }
    if model:
        kwargs["model"] = model
    r = await client.chat.completions.create(**kwargs)
    return r.choices[0].message.content or ""


def _parse_critic_response(response: str) -> list[dict]:
    """Parse critic JSON response, with fallback."""
    # Try to extract JSON block
    text = response.strip()
    # Remove markdown fences if present
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        text = text.rsplit("```", 1)[0]
    text = text.strip()

    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        logger.warning("critic returned unparseable JSON: %.200s", response)
        return []

    gaps = data.get("gaps", [])
    if not isinstance(gaps, list):
        return []
    return [g for g in gaps if isinstance(g, dict) and g.get("search_query")]


__all__ = [
    "run_critics",
    "format_gap_report",
    "CRITIC_PROFILES",
]