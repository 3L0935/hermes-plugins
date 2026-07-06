"""Synthesizer — assembles raw markdown from extracted pages.

**No LLM summarisation.**  The synthesizer is pure structural string
assembly:

1. Group pages by source domain.
2. Deduplicate near-identical pages (hash-based + token overlap).
3. Sort by relevance score (computed during search/convergence).
4. Emit as markdown with headers, source attribution, metadata.
5. If the total exceeds ``MAX_TOOL_RESULT_CHARS`` (200K), write the full
   document to ``~/.hermes/deep-research/<timestamp>.md`` and return a
   truncated version with a pointer to the file.
"""

from __future__ import annotations

import hashlib
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

from .convergence import is_near_duplicate

if TYPE_CHECKING:
    from .types import Page, ResearchGoal

logger = logging.getLogger("hermes.deep-research.synthesizer")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_TOOL_RESULT_CHARS = 200_000
OUTPUT_DIR = Path.home() / ".hermes" / "deep-research"


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------


def _content_hash(page: Page) -> str:
    """SHA-256 of normalised page content (first 5K chars)."""
    normalised = page.content[:5000].strip().lower().replace(" ", "")
    return hashlib.sha256(normalised.encode("utf-8", errors="replace")).hexdigest()


def deduplicate_pages(pages: list[Page]) -> list[Page]:
    """Remove exact-duplicate (by hash) and near-duplicate (by token overlap) pages.

    Keeps the first occurrence (highest relevance, since the list is
    expected to be sorted by relevance descending).
    """
    seen_hashes: set[str] = set()
    unique: list[Page] = []

    for page in pages:
        h = _content_hash(page)
        if h in seen_hashes:
            continue

        # Near-duplicate check against already-kept pages
        is_dup = False
        for kept in unique:
            if is_near_duplicate(page, kept, threshold=0.85):
                is_dup = True
                break

        if is_dup:
            continue

        seen_hashes.add(h)
        unique.append(page)

    return unique


# ---------------------------------------------------------------------------
# Markdown assembly
# ---------------------------------------------------------------------------


def _group_by_domain(pages: list[Page]) -> dict[str, list[Page]]:
    """Group pages by their domain, preserving order within groups."""
    groups: dict[str, list[Page]] = {}
    for p in pages:
        groups.setdefault(p.domain or "unknown", []).append(p)
    return groups


def _format_page(page: Page, index: int) -> str:
    """Format a single page as a markdown section."""
    header = f"## Source {index}: {page.domain} -- \"{page.title or 'Untitled'}\""
    meta_lines = [
        f"**URL:** {page.url}",
        f"**Relevance:** {page.relevance:.2f}",
        f"**Extracted via:** {page.extracted_via}",
        f"**Content length:** {page.content_chars:,} chars",
    ]
    if page.extraction_error:
        meta_lines.append(f"**Extraction error:** {page.extraction_error}")

    meta = "\n".join(meta_lines)
    content = page.content.strip() if page.content else "(no content extracted)"

    return f"{header}\n\n{meta}\n\n{content}\n"


def _build_markdown(
    corpus: list[Page],
    goal: ResearchGoal,
    novelty_history: list[float],
    queries_used: list[str] = None,
    capabilities_used: list[str] = None,
) -> str:
    """Assemble the full markdown document from the corpus."""
    # Deduplicate
    unique = deduplicate_pages(corpus)

    # Sort by relevance descending
    unique.sort(key=lambda p: p.relevance, reverse=True)

    # Group by domain
    domain_groups = _group_by_domain(unique)

    # Header
    lines: list[str] = []
    lines.append(f"# Deep Research: {goal.query}\n")

    # Metadata block
    lines.append("**Research metadata:**")
    lines.append(f"- Pages extracted (raw): {len(corpus)}")
    lines.append(f"- Pages after dedup: {len(unique)}")
    lines.append(f"- Unique domains: {len(domain_groups)}")
    lines.append(f"- Novelty trajectory: {' -> '.join(f'{n:.2f}' for n in novelty_history) if novelty_history else 'n/a'}")
    if capabilities_used:
        lines.append(f"- Capabilities used: {', '.join(capabilities_used)}")
    if queries_used:
        lines.append(f"- Queries used ({len(queries_used)}):")
        for i, q in enumerate(queries_used, 1):
            lines.append(f"  {i}. \"{q}\"")
    lines.append("")

    # Source sections
    lines.append("---\n")
    for i, page in enumerate(unique, 1):
        lines.append(_format_page(page, i))
        lines.append("---\n")

    # Research notes
    lines.append("## Research Notes\n")
    if novelty_history:
        converged = novelty_history[-1] < 0.15 if novelty_history else False
        lines.append(f"- Convergence: {'yes' if converged else 'no'} (final novelty: {novelty_history[-1]:.2f})")
        lines.append(f"- Novelty trajectory: {' -> '.join(f'{n:.2f}' for n in novelty_history)}")
    skipped = sum(1 for p in corpus if p.extraction_error)
    if skipped:
        lines.append(f"- Pages skipped (extraction failed): {skipped}")
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Truncation
# ---------------------------------------------------------------------------


def _truncate_markdown(markdown: str, max_chars: int) -> str:
    """Truncate markdown to ``max_chars``, keeping the head and a tail marker."""
    if len(markdown) <= max_chars:
        return markdown

    # Keep head with some margin for the truncation notice
    head_budget = max_chars - 500
    head = markdown[:head_budget]
    total = len(markdown)
    omitted = total - len(head)

    notice = (
        f"\n\n---\n\n"
        f"**[TRUNCATED: {omitted:,} characters omitted — full content written to disk]**\n"
    )
    return head + notice


# ---------------------------------------------------------------------------
# Disk writing
# ---------------------------------------------------------------------------


def _write_to_disk(markdown: str, goal: ResearchGoal) -> Path:
    """Write the full markdown to ``~/.hermes/deep-research/<timestamp>.md``."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Sanitise the query for use in a filename
    safe_query = "".join(
        c if c.isalnum() or c in "-_ " else "_" for c in goal.query[:60]
    ).strip().replace(" ", "_")

    timestamp = time.strftime("%Y%m%d-%H%M%S")
    filename = f"{timestamp}_{safe_query}.md"
    path = OUTPUT_DIR / filename

    path.write_text(markdown, encoding="utf-8")
    logger.info("full research markdown written to %s (%d chars)", path, len(markdown))
    return path


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def assemble(
    corpus: list[Page],
    goal: ResearchGoal,
    novelty_history: list[float],
    queries_used: list[str] = None,
    capabilities_used: list[str] = None,
) -> dict:
    """Assemble the final markdown document and handle truncation.

    Returns a dict with keys::

        {
          "markdown": str,           # full or truncated markdown
          "full_content_path": str?, # present if truncated
          "truncated": bool,
          "total_chars": int,
        }
    """
    markdown = _build_markdown(
        corpus, goal, novelty_history, queries_used, capabilities_used
    )
    total_chars = len(markdown)

    if total_chars > MAX_TOOL_RESULT_CHARS:
        file_path = _write_to_disk(markdown, goal)
        truncated = _truncate_markdown(markdown, MAX_TOOL_RESULT_CHARS)
        return {
            "markdown": truncated,
            "full_content_path": str(file_path),
            "truncated": True,
            "total_chars": total_chars,
        }

    return {
        "markdown": markdown,
        "full_content_path": None,
        "truncated": False,
        "total_chars": total_chars,
    }


__all__ = [
    "assemble",
    "deduplicate_pages",
    "MAX_TOOL_RESULT_CHARS",
    "OUTPUT_DIR",
]