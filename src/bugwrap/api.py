"""Programmatic API — the CLI is a thin shell over these functions."""

from __future__ import annotations

import dataclasses
from pathlib import Path

from .analysis import build_units, check_units
from .config import Config, load_config
from .models import ChangeUnit, Finding, ImpactSite
from .gitio import (
    diff_base,
    diff_pr,
    diff_range,
    diff_working,
    parse_unified_diff,
    repo_root,
    synthesize_full_review,
)
from .index import RepoIndex, build_index
from .review import ReviewResult, review_units


def _resolve_diffs(root: Path, base, rev_range, pr, paths, staged):
    if paths:
        rels = [str(Path(p).resolve().relative_to(root)) for p in paths]
        return synthesize_full_review(rels, root), "HEAD"
    if pr is not None:
        text, base_rev = diff_pr(pr, root)
    elif rev_range:
        text, base_rev = diff_range(rev_range, root)
    elif base:
        text, base_rev = diff_base(base, root)
    else:
        text, base_rev = diff_working(root, staged_only=staged)
    return parse_unified_diff(text), base_rev


def context_for(
    root: Path | str = ".",
    *,
    base: str | None = None,
    rev_range: str | None = None,
    pr: int | None = None,
    paths: list[str] | None = None,
    staged: bool = False,
    cfg: Config | None = None,
    index: RepoIndex | None = None,
) -> tuple[list[ChangeUnit], RepoIndex]:
    """Build Smart Context packets without calling a model."""
    root = repo_root(root)
    cfg = cfg or load_config(root)
    diffs, base_rev = _resolve_diffs(root, base, rev_range, pr, paths, staged)
    python_diffs = [d for d in diffs if d.is_python and not d.binary]
    index = index or build_index(root, cfg)
    return build_units(python_diffs, index, cfg, root, base_rev), index


def static_check(units, index) -> list[Finding]:
    """The deterministic contract-checker pass. Free, model-less, ~always right.

    The packet caps callers to fit the model's context window; this check is free,
    so it runs over EVERY call site the graph knows about, not the packed subset.
    """
    checked = []
    sources: dict[str, str] = {}
    for unit in units:
        if (
            unit.symbol is not None
            and unit.delta.kind in ("signature_changed", "removed")
            and not unit.removed_fields  # field units already carry reader sites
        ):
            if unit.delta.kind == "removed":
                sites = index.graph.by_name(unit.symbol.short_name, exclude_path=unit.path)
            else:
                sites = [
                    s
                    for s in index.graph.callers_of(unit.symbol.fqname)
                    if not (s.path == unit.path and unit.symbol.contains(s.line))
                ]
            unit = dataclasses.replace(
                unit, callers=[ImpactSite(site=s, snippet="") for s in sites]
            )
        checked.append(unit)
        for path in {
            unit.path,
            *(i.site.path for i in unit.callers),
            *(o.path for o in unit.overrides),
        }:
            if path not in sources:
                text = index.source_of(path)
                if text is not None:
                    sources[path] = text
    return check_units(checked, sources)


def review_changes(
    root: Path | str = ".",
    *,
    base: str | None = None,
    rev_range: str | None = None,
    pr: int | None = None,
    paths: list[str] | None = None,
    staged: bool = False,
    cfg: Config | None = None,
) -> ReviewResult:
    """Build packets and run them through the configured model."""
    root = repo_root(root)
    cfg = cfg or load_config(root)
    units, _ = context_for(
        root, base=base, rev_range=rev_range, pr=pr, paths=paths, staged=staged, cfg=cfg
    )
    if not units:
        return ReviewResult(units=0)
    return review_units(units, cfg)
