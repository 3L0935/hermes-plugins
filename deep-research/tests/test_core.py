"""Tests for deep-research plugin core modules."""

from __future__ import annotations

import sys
import json
from pathlib import Path

# Add plugin dirs to path — works from repo root (tests/ is one level deep)
PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))

from core.types import ResearchGoal, Page, SearchResult
from core.budget import Budget, BudgetGuard
from core.convergence import compute_novelty, should_converge, is_near_duplicate
from core.synthesizer import assemble
from capabilities.registry import CapabilityRegistry, BUILTIN_TOOL_CAPABILITY_MAP


# ── Budget tests ─────────────────────────────────────────────────────────────


def test_budget_exhausted_by_pages():
    b = Budget(max_pages=5)
    assert b.exhausted(0, 5)
    assert not b.exhausted(0, 4)


def test_budget_exhausted_by_iterations():
    b = Budget(max_iterations=3)
    assert b.exhausted(3, 0)
    assert not b.exhausted(2, 0)


def test_budget_guard_time():
    guard = BudgetGuard(Budget(timeout_seconds=60))
    assert guard.time_remaining() > 50
    assert not guard.is_expired()


# ── Convergence tests ───────────────────────────────────────────────────────


def test_novelty_identical_pages():
    p1 = Page(url="a.com", title="A", content="hello world foo bar",
              domain="a.com", extracted_via="test")
    assert compute_novelty([p1], [p1]) < 0.1


def test_novelty_completely_different():
    p1 = Page(url="a.com", title="A", content="alpha beta gamma delta",
              domain="a.com", extracted_via="test")
    p2 = Page(url="b.com", title="B", content="one two three four five six",
              domain="b.com", extracted_via="test")
    assert compute_novelty([p2], [p1]) > 0.5


def test_novelty_empty_corpus():
    p = Page(url="a.com", title="A", content="hello world",
             domain="a.com", extracted_via="test")
    assert compute_novelty([p], []) == 1.0


def test_novelty_empty_new():
    assert compute_novelty([], [Page(url="a.com", title="A", content="x",
                                     domain="a.com", extracted_via="test")]) == 0.0


def test_should_converge():
    history = [1.0, 0.5, 0.1, 0.08]
    assert should_converge(history, threshold=0.15, window=2, min_iterations=2)


def test_should_not_converge_before_min():
    history = [0.1, 0.08]
    assert not should_converge(history, threshold=0.15, window=2, min_iterations=3)


def test_should_not_converge_high_novelty():
    history = [1.0, 0.9, 0.8, 0.7]
    assert not should_converge(history, threshold=0.15, window=2, min_iterations=2)


def test_is_near_duplicate():
    a = Page(url="a.com", title="A", content="The quick brown fox jumps over the lazy dog",
             domain="a.com", extracted_via="test")
    b = Page(url="b.com", title="B", content="The quick brown fox jumps over the lazy dog.",
             domain="b.com", extracted_via="test")
    assert is_near_duplicate(a, b, threshold=0.9)


def test_is_not_near_duplicate():
    a = Page(url="a.com", title="A", content="The quick brown fox jumps over the lazy dog",
             domain="a.com", extracted_via="test")
    b = Page(url="b.com", title="B", content="Completely different content about something else entirely",
             domain="b.com", extracted_via="test")
    assert not is_near_duplicate(a, b, threshold=0.5)


# ── Synthesizer tests ────────────────────────────────────────────────────────


def test_assemble_empty():
    result = assemble([], ResearchGoal(query="test"), novelty_history=[])
    assert "Pages extracted (raw): 0" in result["markdown"]
    assert not result["truncated"]


def test_assemble_single_page():
    pages = [
        Page(url="https://example.com", title="Example", content="# Hello\nWorld",
             domain="example.com", extracted_via="web_extract", relevance=0.9),
    ]
    result = assemble(pages, ResearchGoal(query="test query"), novelty_history=[1.0])
    assert "Example" in result["markdown"]
    assert "Hello" in result["markdown"]
    assert not result["truncated"]


def test_assemble_dedup():
    pages = [
        Page(url="https://a.com/1", title="A1", content="same content here",
             domain="a.com", extracted_via="web_extract"),
        Page(url="https://a.com/2", title="A2", content="same content here",
             domain="a.com", extracted_via="web_extract"),
    ]
    result = assemble(pages, ResearchGoal(query="test"), novelty_history=[1.0])
    # Should deduplicate near-identical content
    assert "Pages after dedup: 1" in result["markdown"]


def test_assemble_metadata():
    pages = [
        Page(url="https://example.com", title="Test", content="content",
             domain="example.com", extracted_via="web_extract"),
    ]
    result = assemble(pages, ResearchGoal(query="test"), novelty_history=[1.0, 0.5, 0.1])
    assert "Novelty trajectory: 1.00 -> 0.50 -> 0.10" in result["markdown"]
    assert "Pages extracted (raw): 1" in result["markdown"]


# ── CapabilityRegistry tests ─────────────────────────────────────────────────


def test_capability_registry_empty():
    reg = CapabilityRegistry(set())
    caps = reg.get_capabilities()
    assert isinstance(caps, dict)


def test_capability_registry_with_tools():
    reg = CapabilityRegistry({"web_search", "web_extract"})
    caps = reg.get_capabilities()
    assert "search" in caps
    assert "page-extract" in caps
    assert "deep-crawl" not in caps


def test_capability_registry_all():
    reg = CapabilityRegistry(set(BUILTIN_TOOL_CAPABILITY_MAP.keys()))
    caps = reg.get_capabilities()
    assert "search" in caps
    assert "page-extract" in caps
    assert "deep-crawl" in caps
    assert "site-map" in caps
    assert "browser-scrape" in caps


# ── max_depth / ResearchGoal tests ──────────────────────────────────────────


def test_research_goal_max_depth_default():
    goal = ResearchGoal(query="test")
    assert goal.max_depth == 1


def test_research_goal_max_depth_custom():
    goal = ResearchGoal(query="test", max_depth=3)
    assert goal.max_depth == 3


def test_budget_max_depth_default():
    b = Budget()
    assert b.max_depth == 1


def test_budget_max_depth_custom():
    b = Budget(max_depth=2)
    assert b.max_depth == 2


# ── _guess_urls tests ────────────────────────────────────────────────────────


def test_guess_urls_bare_model():
    from core.searcher import _guess_urls
    urls = _guess_urls("Qwen3.6-35B-A3B")
    # Should include the Qwen org page
    assert any("huggingface.co/Qwen/Qwen3.6-35B-A3B" in u for u in urls)
    # Should include GGUF variants
    assert any("unsloth/Qwen3.6-35B-A3B-GGUF" in u for u in urls)
    assert any("bartowski/Qwen3.6-35B-A3B-GGUF" in u for u in urls)
    # Should include search fallback
    assert any("models?search=Qwen3.6-35B-A3B" in u for u in urls)


def test_guess_urls_org_model():
    from core.searcher import _guess_urls
    urls = _guess_urls("Qwen/Qwen3.6-35B-A3B")
    assert any("huggingface.co/Qwen/Qwen3.6-35B-A3B" in u for u in urls)
    assert any("unsloth/Qwen3.6-35B-A3B-GGUF" in u for u in urls)


def test_guess_urls_deepseek():
    from core.searcher import _guess_urls
    urls = _guess_urls("DeepSeek-V3")
    assert any("deepseek-ai/DeepSeek-V3" in u for u in urls)


def test_guess_urls_no_model():
    from core.searcher import _guess_urls
    urls = _guess_urls("what is the weather today")
    assert urls == []


# ── Searcher max_depth tests ────────────────────────────────────────────────


def test_searcher_max_depth_default():
    from core.searcher import Searcher
    s = Searcher(lambda *a, **kw: None)
    assert s._max_depth == 1


def test_searcher_max_depth_custom():
    from core.searcher import Searcher
    s = Searcher(lambda *a, **kw: None, max_depth=3)
    assert s._max_depth == 3


def test_searcher_max_depth_clamped():
    from core.searcher import Searcher
    s = Searcher(lambda *a, **kw: None, max_depth=0)
    assert s._max_depth == 1  # clamped to minimum


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
