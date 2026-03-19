"""
Usage Cost Reporter (Python)

Native Python implementation that reports LLM API usage costs to the
central BudgetTracker file (.data/llm-usage-costs.json).

Mirrors the Node.js usage-cost-reporter.js logic:
- Same JSON file format
- Same file locking protocol (.lock file with stale detection)
- Same atomic write (tmp + rename)
- Same pricing table

Usage:
    from ..utils.usage_cost_reporter import report_usage_cost

    # After an Agent.run() call:
    result = await agent.run(prompt)
    report_usage_cost(
        provider="groq",
        model="llama-3.3-70b-versatile",
        input_tokens=result.usage().request_tokens or 0,
        output_tokens=result.usage().response_tokens or 0,
        source="code-graph-rag:cypher-generate",
    )
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger

# Pricing per million tokens (input/output) - keep in sync with usage-cost-reporter.js
PRICING: dict[str, dict[str, dict[str, float]]] = {
    "groq": {
        "llama-3.3-70b-versatile": {"input": 0.59, "output": 0.79},
        "llama-3.1-8b-instant": {"input": 0.05, "output": 0.08},
        "llama-4-scout-17b-16e-instruct": {"input": 0.11, "output": 0.34},
        "llama-4-maverick-17b-128e-instruct": {"input": 0.20, "output": 0.60},
        "qwen3-32b": {"input": 0.29, "output": 0.59},
        "openai/gpt-oss-120b": {"input": 0.15, "output": 0.60},
        "openai/gpt-oss-20b": {"input": 0.075, "output": 0.30},
        "mixtral-8x7b-32768": {"input": 0.24, "output": 0.24},
        "default": {"input": 0.50, "output": 0.50},
    },
    "anthropic": {
        "default": {"input": 3.00, "output": 15.00},
    },
    "openai": {
        "default": {"input": 5.00, "output": 15.00},
    },
}

# Month abbreviations
_MONTHS = [
    "JAN",
    "FEB",
    "MAR",
    "APR",
    "MAY",
    "JUN",
    "JUL",
    "AUG",
    "SEP",
    "OCT",
    "NOV",
    "DEC",
]


def _resolve_cost_file_path() -> Path:
    """Resolve path to .data/llm-usage-costs.json."""
    repo_root = os.environ.get("CODING_REPO")
    if repo_root:
        return Path(repo_root) / ".data" / "llm-usage-costs.json"
    # Walk up from this file to find the coding root
    # This file is at: coding/integrations/code-graph-rag/codebase_rag/utils/usage_cost_reporter.py
    # coding root is 5 levels up
    here = Path(__file__).resolve()
    coding_root = here.parents[4]  # coding/
    return coding_root / ".data" / "llm-usage-costs.json"


def _current_month_abbrev() -> str:
    """Get current month as 3-letter abbreviation."""
    return _MONTHS[datetime.now().month - 1]


def calculate_cost(
    provider: str, model: str, input_tokens: int, output_tokens: int
) -> float:
    """Calculate cost from token counts.

    Args:
        provider: Provider name (e.g. 'groq')
        model: Model name (e.g. 'llama-3.3-70b-versatile')
        input_tokens: Input/prompt token count
        output_tokens: Output/completion token count

    Returns:
        Cost in USD
    """
    provider_pricing = PRICING.get(provider, PRICING["groq"])
    model_pricing = provider_pricing.get(
        model, provider_pricing.get("default", {"input": 0.50, "output": 0.50})
    )
    input_cost = (input_tokens / 1_000_000) * model_pricing["input"]
    output_cost = (output_tokens / 1_000_000) * model_pricing["output"]
    return input_cost + output_cost


class _FileLock:
    """Simple file lock using a .lock file with stale lock detection.

    Matches the protocol in usage-cost-reporter.js:
    - O_CREAT | O_EXCL for atomic creation
    - Stale lock detection (>10s old)
    - Timeout with brief sleep between retries
    """

    def __init__(self, file_path: Path, timeout_s: float = 5.0) -> None:
        self.lock_path = Path(str(file_path) + ".lock")
        self.timeout_s = timeout_s
        self.acquired = False

    def __enter__(self) -> "_FileLock":
        start = time.monotonic()
        while time.monotonic() - start < self.timeout_s:
            try:
                # O_CREAT | O_EXCL: atomic create, fails if exists
                fd = os.open(str(self.lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                self.acquired = True
                return self
            except FileExistsError:
                # Lock held — check if stale (>10s old)
                try:
                    stat = self.lock_path.stat()
                    if time.time() - stat.st_mtime > 10:
                        try:
                            self.lock_path.unlink()
                        except OSError:
                            pass
                except OSError:
                    pass
                time.sleep(0.01)
            except OSError:
                return self  # unexpected error, proceed without lock
        # Timeout
        return self

    def __exit__(self, *_: Any) -> None:
        if self.acquired:
            try:
                self.lock_path.unlink()
            except OSError:
                pass


def report_usage_cost(
    *,
    provider: str = "groq",
    model: str = "default",
    input_tokens: int = 0,
    output_tokens: int = 0,
    total_tokens: int | None = None,
    source: str = "code-graph-rag",
) -> dict[str, float]:
    """Report usage cost to the central BudgetTracker file.

    Performs atomic read-modify-write with file locking.

    Args:
        provider: Provider name (e.g. 'groq')
        model: Model name (e.g. 'llama-3.3-70b-versatile')
        input_tokens: Input/prompt tokens
        output_tokens: Output/completion tokens
        total_tokens: Total tokens (if input/output unavailable)
        source: Source identifier for debugging

    Returns:
        Dict with 'cost' and 'total_provider_cost'
    """
    actual_input = input_tokens or 0
    actual_output = output_tokens or 0
    actual_total = (
        total_tokens if total_tokens is not None else (actual_input + actual_output)
    )

    cost = calculate_cost(provider, model, actual_input, actual_output)
    cost_file = _resolve_cost_file_path()
    current_month = _current_month_abbrev()

    with _FileLock(cost_file) as lock:
        if not lock.acquired:
            logger.warning(
                f"Could not acquire lock for {cost_file}, cost not persisted"
            )
            return {"cost": cost, "total_provider_cost": cost}

        # Read existing data
        data: dict[str, Any] = {
            "billingMonth": current_month,
            "lastUpdated": None,
            "providers": {},
        }

        try:
            if cost_file.exists():
                existing = json.loads(cost_file.read_text(encoding="utf-8"))
                if existing.get("billingMonth") == current_month:
                    data = existing
                # Different month: start fresh (month rollover)
        except (json.JSONDecodeError, OSError):
            pass  # Corrupt file; start fresh

        # Ensure provider entry exists
        if provider not in data["providers"]:
            data["providers"][provider] = {"cost": 0, "tokens": 0, "count": 0}

        # Increment
        data["providers"][provider]["cost"] = round(
            data["providers"][provider]["cost"] + cost, 6
        )
        data["providers"][provider]["tokens"] += actual_total
        data["providers"][provider]["count"] += 1
        data["lastUpdated"] = (
            datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        )

        # Atomic write (tmp + rename)
        cost_file.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = cost_file.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp_path.rename(cost_file)

    logger.debug(
        f"[usage-cost-reporter] Reported {source}: ${cost:.6f} "
        f"({actual_input}+{actual_output} tokens, {provider}/{model})"
    )

    return {
        "cost": cost,
        "total_provider_cost": data["providers"][provider]["cost"],
    }
