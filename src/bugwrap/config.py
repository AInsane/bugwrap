"""Configuration: defaults <- .bugwrap.toml <- environment <- CLI flags."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path

CONFIG_NAMES = (".bugwrap.toml", "bugwrap.toml")

DEFAULT_EXCLUDES = (
    ".git",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    "node_modules",
    "build",
    "dist",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "site-packages",
    ".bugwrap",
)


@dataclass
class Config:
    # --- model ---
    host: str = "http://localhost:11434"
    model: str = "qwen2.5-coder:14b"
    num_ctx: int = 16384
    temperature: float = 0.1
    think: bool = False
    timeout: int = 300

    # --- review ---
    fail_on: str = "high"
    min_confidence: float = 0.45
    workers: int = 2
    max_callers: int = 8
    snippet_radius: int = 3
    include_tests: bool = True
    max_unit_tokens: int = 0  # 0 -> derived from num_ctx
    max_symbol_lines: int = 400
    verify: bool = True  # adversarial second pass over LLM findings
    co_change: bool = True  # mine git history for files that ship together
    two_hop: bool = True  # trace wrapper functions one level up on breaking changes

    # --- index ---
    exclude: tuple[str, ...] = DEFAULT_EXCLUDES
    roots: tuple[str, ...] = ()  # limit indexing to these subtrees
    use_cache: bool = True

    source: str | None = None  # path of the config file that was loaded

    @property
    def unit_budget(self) -> int:
        """Token budget for a single review packet.

        Leaves ~35% of the context for the system prompt, chat overhead and the
        model's own output. Local models silently truncate when you overrun
        num_ctx, so this ceiling is load-bearing, not cosmetic.
        """
        if self.max_unit_tokens:
            return self.max_unit_tokens
        return max(1200, int(self.num_ctx * 0.65))


def find_config(start: Path) -> Path | None:
    for directory in [start, *start.parents]:
        for name in CONFIG_NAMES:
            candidate = directory / name
            if candidate.is_file():
                return candidate
    return None


def load_config(start: Path | None = None) -> Config:
    cfg = Config()
    path = find_config(start or Path.cwd())
    if path:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
        cfg = _apply_toml(cfg, data)
        cfg = replace(cfg, source=str(path))
    return _apply_env(cfg)


def _apply_toml(cfg: Config, data: dict) -> Config:
    model = data.get("model", {})
    review = data.get("review", {})
    index = data.get("index", {})
    updates: dict = {}

    for key in ("host", "num_ctx", "temperature", "think", "timeout"):
        if key in model:
            updates[key] = model[key]
    if "name" in model:
        updates["model"] = model["name"]
    if "model" in model:  # tolerate [model] model = "..."
        updates["model"] = model["model"]

    for key in (
        "fail_on",
        "min_confidence",
        "workers",
        "max_callers",
        "snippet_radius",
        "include_tests",
        "max_unit_tokens",
        "max_symbol_lines",
        "verify",
        "co_change",
        "two_hop",
    ):
        if key in review:
            updates[key] = review[key]

    if "exclude" in index:
        updates["exclude"] = tuple(index["exclude"])
    if "roots" in index:
        updates["roots"] = tuple(index["roots"])
    if "cache" in index:
        updates["use_cache"] = bool(index["cache"])

    return replace(cfg, **updates)


def _apply_env(cfg: Config) -> Config:
    updates: dict = {}
    host = os.environ.get("BUGWRAP_HOST") or os.environ.get("OLLAMA_HOST")
    if host:
        if not host.startswith("http"):
            host = f"http://{host}"
        updates["host"] = host.rstrip("/")
    if model := os.environ.get("BUGWRAP_MODEL"):
        updates["model"] = model
    if ctx := os.environ.get("BUGWRAP_NUM_CTX"):
        try:
            updates["num_ctx"] = int(ctx)
        except ValueError:
            pass
    return replace(cfg, **updates) if updates else cfg


DEFAULT_TOML = """\
# bugwrap configuration

[model]
host = "http://localhost:11434"
name = "qwen2.5-coder:14b"
num_ctx = 16384          # must match what your model can actually hold
temperature = 0.1
think = false            # set true for reasoning models (deepseek-r1, qwq, ...)

[review]
fail_on = "high"         # exit 1 when a finding at or above this severity survives
min_confidence = 0.45    # local models over-report; this is the floor
verify = true            # adversarial second pass over model findings
workers = 2              # concurrent requests to ollama
max_callers = 8          # impact sites per packet before budget packing kicks in
snippet_radius = 3       # lines of context around each call site
include_tests = true
co_change = true         # mine git history for files that ship together
two_hop = true           # trace *args/**kwargs wrappers one level up on breaking changes

[index]
exclude = [".venv", "venv", "build", "dist", "node_modules", "__pycache__"]
cache = true
"""
