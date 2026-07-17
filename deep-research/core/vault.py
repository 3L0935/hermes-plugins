"""ResearchVault — persistent knowledge base for deep research outputs.

Stores every research run as an indexed markdown file + a JSON index
for full-text search across runs.  The vault lives at
``~/.hermes/deep-research/vault/`` and is git-friendly (plain files).

Structure::

  ~/.hermes/deep-research/vault/
    index.json         # {query_hash: {query, timestamp, mode, n_sources, n_gaps}}
    researches/        # Full markdown files, one per query
      <hash>.md
      <hash>.md
    gap-fills/         # Gap analysis extracts
      <hash>-gaps.md
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("hermes.deep-research.vault")


VAULT_DIR = Path.home() / ".hermes" / "deep-research" / "vault"
RESEARCHES_DIR = VAULT_DIR / "researches"
GAP_DIR = VAULT_DIR / "gap-fills"
INDEX_PATH = VAULT_DIR / "index.json"


class ResearchVault:
    """Persistent, searchable vault for deep research outputs.

    Usage::

        vault = ResearchVault()
        vault.index_research(query=data_query, markdown=md, sources=pages, gaps=gaps)

        # Later:
        results = vault.search("local inference MoE")
        vault.list_recent(5)
    """

    def __init__(self, vault_dir: Optional[Path] = None):
        self._vault_dir = Path(vault_dir or VAULT_DIR)
        self._researches_dir = self._vault_dir / "researches"
        self._gap_dir = self._vault_dir / "gap-fills"
        self._index_path = self._vault_dir / "index.json"
        self._ensure_dirs()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def index_research(
        self,
        query: str,
        markdown: str,
        sources: list[Any],
        gaps: Optional[list[dict]] = None,
    ) -> str:
        """Store a research run in the vault.

        Returns the query hash for later retrieval.
        """
        qhash = self._query_hash(query)
        timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        # Write full markdown
        md_path = self._researches_dir / f"{qhash}.md"
        md_path.write_text(markdown, encoding="utf-8")
        logger.debug("vault: wrote research %s (%d chars)", qhash, len(markdown))

        # Write gap analysis separately if present
        if gaps:
            gap_path = self._gap_dir / f"{qhash}-gaps.md"
            gap_content = self._format_gaps(query, gaps)
            gap_path.write_text(gap_content, encoding="utf-8")

        # Update index
        index = self._load_index()
        index[qhash] = {
            "query": query,
            "query_hash": qhash,
            "timestamp": timestamp,
            "n_sources": len(sources),
            "n_gaps": len(gaps or []),
            "char_count": len(markdown),
            "mode": "full" if gaps else "light",
        }
        self._save_index(index)

        return qhash

    def search(self, term: str, max_results: int = 10) -> list[dict]:
        """Simple substring search over vault queries and content.

        Returns entries where the query text or hash matches.
        For more advanced search, use the full-text markdown files directly.
        """
        term_lower = term.lower()
        index = self._load_index()
        results: list[dict] = []

        for qhash, entry in index.items():
            score = 0
            if term_lower in entry["query"].lower():
                score += 10 - min(9, len(term_lower.split()) * 2)
            if term_lower in qhash:
                score += 2

            if score > 0:
                md_path = self._researches_dir / f"{qhash}.md"
                results.append({
                    **entry,
                    "path": str(md_path),
                    "exists": md_path.exists(),
                    "relevance": score,
                })

        results.sort(key=lambda r: r["relevance"], reverse=True)
        return results[:max_results]

    def list_recent(self, limit: int = 10) -> list[dict]:
        """List the most recent research entries."""
        index = self._load_index()
        entries = list(index.values())
        entries.sort(key=lambda e: e.get("timestamp", ""), reverse=True)
        return entries[:limit]

    def get(self, query_hash: str) -> Optional[dict]:
        """Get a specific research entry by its query hash."""
        index = self._load_index()
        entry = index.get(query_hash)
        if not entry:
            return None

        md_path = self._researches_dir / f"{query_hash}.md"
        content = md_path.read_text(encoding="utf-8") if md_path.exists() else ""

        return {
            **entry,
            "content": content,
            "path": str(md_path),
        }

    def stats(self) -> dict:
        """Vault statistics."""
        index = self._load_index()
        total_chars = sum(e.get("char_count", 0) for e in index.values())
        return {
            "total_researches": len(index),
            "total_chars": total_chars,
            "vault_dir": str(self._vault_dir),
            "full_mode_entries": sum(1 for e in index.values() if e.get("mode") == "full"),
        }

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _ensure_dirs(self) -> None:
        self._vault_dir.mkdir(parents=True, exist_ok=True)
        self._researches_dir.mkdir(parents=True, exist_ok=True)
        self._gap_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _query_hash(query: str) -> str:
        """Stable hash of the query text (first 64 chars of SHA-256)."""
        return hashlib.sha256(query.strip().lower().encode()).hexdigest()[:16]

    def _load_index(self) -> dict[str, dict]:
        if self._index_path.exists():
            try:
                data = json.loads(self._index_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("vault index corrupt: %s — resetting", exc)
        return {}

    def _save_index(self, index: dict[str, dict]) -> None:
        self._index_path.write_text(
            json.dumps(index, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    @staticmethod
    def _format_gaps(query: str, gaps: list[dict]) -> str:
        lines = [
            f"# Gap Analysis: {query}",
            f"_Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}_",
            "",
            f"**{len(gaps)} gaps identified**",
            "",
        ]
        for i, g in enumerate(gaps, 1):
            lines.append(f"## Gap {i}: {g.get('gap', 'Unknown')}")
            lines.append(f"- **Severity:** {g.get('severity', 'unknown')}")
            lines.append(f"- **Search:** `{g.get('search_query', 'N/A')}`")
            lines.append(f"- **Why:** {g.get('why_needed', 'N/A')}")
            lines.append("")
        return "\n".join(lines)


__all__ = ["ResearchVault"]