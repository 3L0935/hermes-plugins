"""Budget controller for deep research — caps pages, time, iterations."""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class Budget:
    # Pages
    max_pages: int = 30
    max_pages_per_extraction: int = 10

    # Search
    max_results_per_query: int = 10
    max_queries_per_iteration: int = 5

    # Iterations
    max_iterations: int = 5
    min_iterations: int = 2

    # Time
    timeout_seconds: int = 300
    per_page_timeout: int = 30

    # Crawl depth
    max_depth: int = 1  # maxDepth passed to web_crawl (1=homepage+1 link level)

    # Convergence
    novelty_threshold: float = 0.15
    convergence_window: int = 2

    # Cost
    max_llm_calls: int = 10

    def exhausted(self, iteration: int, pages_extracted: int) -> bool:
        if pages_extracted >= self.max_pages:
            return True
        if iteration >= self.max_iterations:
            return True
        return False


class BudgetGuard:
    """Runtime budget enforcement — tracks time and LLM call budget."""

    def __init__(self, budget: Budget):
        self.budget = budget
        self.start_time = time.monotonic()
        self.llm_calls = 0

    def time_remaining(self) -> float:
        elapsed = time.monotonic() - self.start_time
        return max(0.0, self.budget.timeout_seconds - elapsed)

    def can_call_llm(self) -> bool:
        return self.llm_calls < self.budget.max_llm_calls and self.time_remaining() > 5

    def can_extract(self, n_pages: int = 1) -> bool:
        return self.time_remaining() > self.budget.per_page_timeout

    def is_expired(self) -> bool:
        return self.time_remaining() <= 0
