"""Tests for deep-research full mode features: critics, vault, mode param."""

from __future__ import annotations

import sys
import json
import tempfile
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))

from core.types import Page, ResearchGoal, ResearchMode, ResearchResult
from core.critics import run_critics, format_gap_report, _parse_critic_response
from core.vault import ResearchVault


# ── ResearchMode enum tests ───────────────────────────────────────────────────


def test_research_mode_values():
    assert ResearchMode.LIGHT == "light"
    assert ResearchMode.FULL == "full"


def test_research_goal_default_mode():
    goal = ResearchGoal(query="test")
    assert goal.mode == ResearchMode.LIGHT


def test_research_goal_full_mode():
    goal = ResearchGoal(query="test", mode="full")
    assert goal.mode == ResearchMode.FULL


# ── Critics tests ─────────────────────────────────────────────────────────────


def test_parse_critic_response_valid_json():
    response = '''{"gaps": [{"gap": "missing X", "severity": "high", "search_query": "X review", "why_needed": "needed for context"}]}'''
    gaps = _parse_critic_response(response)
    assert len(gaps) == 1
    assert gaps[0]["gap"] == "missing X"
    assert gaps[0]["severity"] == "high"


def test_parse_critic_response_with_fences():
    response = '```json\n{"gaps": [{"gap": "missing Y", "severity": "medium", "search_query": "Y analysis", "why_needed": "fills gap"}]}\n```'
    gaps = _parse_critic_response(response)
    assert len(gaps) == 1
    assert gaps[0]["gap"] == "missing Y"


def test_parse_critic_response_empty():
    assert _parse_critic_response("not json") == []


def test_parse_critic_response_missing_search_query():
    response = '''{"gaps": [{"gap": "no query", "severity": "low", "search_query": "", "why_needed": "test"}]}'''
    gaps = _parse_critic_response(response)
    assert len(gaps) == 0  # filtered out — empty search_query


def test_run_critics_no_llm():
    """Should return empty list when no LLM client."""
    import asyncio
    gaps = asyncio.run(run_critics("test query", "some markdown", None, None))
    assert gaps == []


def test_format_gap_report_empty():
    report = format_gap_report([], [])
    assert "No significant gaps identified" in report


def test_format_gap_report_with_gaps():
    gaps = [
        {"gap": "missing benchmark", "severity": "high", "search_query": "new benchmark", "why_needed": "comparison data"},
    ]
    report = format_gap_report(gaps, [])
    assert "missing benchmark" in report
    assert "high" in report
    assert "new benchmark" in report


def test_format_gap_report_with_sources():
    gaps = [{"gap": "missing X", "severity": "high", "search_query": "X review", "why_needed": "needed"}]
    sources = [{"url": "https://example.com", "title": "Example Source", "snippet": "some content"}]
    report = format_gap_report(gaps, sources)
    assert "Example Source" in report
    assert "Gap-filling Sources" in report


# ── Vault tests ───────────────────────────────────────────────────────────────


def test_vault_index_research():
    with tempfile.TemporaryDirectory() as tmp:
        vault = ResearchVault(vault_dir=Path(tmp))

        qhash = vault.index_research(
            query="test query",
            markdown="# Research\n\nSome content here",
            sources=[Page(url="https://a.com", title="A", content="hello", domain="a.com", extracted_via="test")],
            gaps=None,
        )

        # Should have created the markdown file
        md_path = Path(tmp) / "researches" / f"{qhash}.md"
        assert md_path.exists()
        assert "Research" in md_path.read_text(encoding="utf-8")

        # Should be in index
        index_path = Path(tmp) / "index.json"
        assert index_path.exists()
        index = json.loads(index_path.read_text(encoding="utf-8"))
        assert qhash in index
        assert index[qhash]["query"] == "test query"
        assert index[qhash]["n_sources"] == 1
        assert index[qhash]["mode"] == "light"


def test_vault_index_with_gaps():
    with tempfile.TemporaryDirectory() as tmp:
        vault = ResearchVault(vault_dir=Path(tmp))

        gaps = [{"gap": "missing X", "severity": "high", "search_query": "X review", "why_needed": "needed"}]
        qhash = vault.index_research(
            query="test with gaps",
            markdown="# Research",
            sources=[],
            gaps=gaps,
        )

        # Gap file should exist
        gap_path = Path(tmp) / "gap-fills" / f"{qhash}-gaps.md"
        assert gap_path.exists()
        assert "missing X" in gap_path.read_text(encoding="utf-8")

        # Index should show full mode
        index = json.loads((Path(tmp) / "index.json").read_text(encoding="utf-8"))
        assert index[qhash]["mode"] == "full"
        assert index[qhash]["n_gaps"] == 1


def test_vault_search():
    with tempfile.TemporaryDirectory() as tmp:
        vault = ResearchVault(vault_dir=Path(tmp))

        vault.index_research(query="deep learning transformers", markdown="# DL", sources=[], gaps=None)
        vault.index_research(query="local LLM inference", markdown="# LLM", sources=[], gaps=None)

        results = vault.search("deep learning")
        assert len(results) >= 1
        assert any("deep learning" in r["query"] for r in results)


def test_vault_search_no_match():
    with tempfile.TemporaryDirectory() as tmp:
        vault = ResearchVault(vault_dir=Path(tmp))
        vault.index_research(query="python", markdown="# py", sources=[], gaps=None)
        results = vault.search("quantum physics")
        assert len(results) == 0


def test_vault_list_recent():
    with tempfile.TemporaryDirectory() as tmp:
        vault = ResearchVault(vault_dir=Path(tmp))
        vault.index_research(query="alpha", markdown="# a", sources=[], gaps=None)
        vault.index_research(query="beta", markdown="# b", sources=[], gaps=None)

        recent = vault.list_recent(5)
        assert len(recent) == 2


def test_vault_get():
    with tempfile.TemporaryDirectory() as tmp:
        vault = ResearchVault(vault_dir=Path(tmp))
        qhash = vault.index_research(query="test", markdown="# Hello World", sources=[], gaps=None)

        entry = vault.get(qhash)
        assert entry is not None
        assert entry["query"] == "test"
        assert "Hello World" in entry["content"]


def test_vault_get_missing():
    with tempfile.TemporaryDirectory() as tmp:
        vault = ResearchVault(vault_dir=Path(tmp))
        assert vault.get("nonexistenthash123") is None


def test_vault_stats():
    with tempfile.TemporaryDirectory() as tmp:
        vault = ResearchVault(vault_dir=Path(tmp))
        vault.index_research(query="test", markdown="# Hello", sources=[], gaps=None)

        stats = vault.stats()
        assert stats["total_researches"] == 1
        assert stats["total_chars"] > 0


# ── ResearchResult mode field tests ───────────────────────────────────────────


def test_research_result_includes_mode():
    result = ResearchResult(
        success=True,
        query="test",
        iterations=2,
        pages_extracted=5,
        converged=True,
        novelty_final=0.1,
        sources=[],
        markdown="# Test",
        mode="full",
    )
    assert result.mode == "full"
    assert result.gaps == []
    assert result.gap_sources == []


def test_research_result_default_mode():
    result = ResearchResult(
        success=True,
        query="test",
        iterations=1,
        pages_extracted=0,
        converged=False,
        novelty_final=0.0,
        sources=[],
        markdown="# Test",
    )
    assert result.mode == "light"


# ── Run ──────────────────────────────────────────────────────────────────────


if __name__ == "__main__":
    import traceback
    tests = [n for n in dir() if n.startswith("test_")]
    passed = 0
    failed = 0
    for name in tests:
        try:
            globals()[name]()
            print(f"  ✅ {name}")
            passed += 1
        except Exception as e:
            print(f"  ❌ {name}: {e}")
            traceback.print_exc()
            failed += 1
    print(f"\n{'='*40}")
    print(f"  {passed} passed, {failed} failed")