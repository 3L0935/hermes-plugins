"""Data types for the deep-research plugin."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ResearchGoal:
    query: str
    focus_domains: list[str] = field(default_factory=list)
    exclude_domains: list[str] = field(default_factory=list)
    max_depth: int = 1  # crawl depth for web_crawl discovery (1=homepage+links)


@dataclass
class SearchQuery:
    text: str
    iteration: int
    is_followup: bool = False


@dataclass
class SearchResult:
    url: str
    title: str
    snippet: str
    domain: str
    relevance: float = 0.0


@dataclass
class Page:
    url: str
    title: str
    content: str  # raw markdown
    domain: str
    extracted_via: str  # "web_extract", "web_crawl", "browser"
    content_chars: int = 0
    relevance: float = 0.0
    extraction_error: Optional[str] = None

    def __post_init__(self):
        self.content_chars = len(self.content)


@dataclass
class ResearchResult:
    success: bool
    query: str
    iterations: int
    pages_extracted: int
    converged: bool
    novelty_final: float
    sources: list[dict]
    markdown: str
    full_content_path: Optional[str] = None
    truncated: bool = False
    total_chars: int = 0
    error: Optional[str] = None
