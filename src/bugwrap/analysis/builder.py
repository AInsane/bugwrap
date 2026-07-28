"""Smart Context assembly.

Turns a diff into a list of ChangeUnits — self-contained review packets, each
holding the diff, the changed symbol, and the call sites it can break, packed
to fit the model's context window.

Ranking, in order of what a reviewer actually needs:
  1. the diff hunks                        (always)
  2. the full body of the changed symbol   (always)
  3. the previous signature                (only on a contract change)
  4. call sites, best-resolved first       (budget-packed)
  5. in-repo callees' signatures           (filler)
"""

from __future__ import annotations

import re
from pathlib import Path

from ..config import Config
from ..gitio import co_change_counts, show_file
from ..index import RepoIndex
from ..index.symbols import extract_symbols, module_name
from ..models import ChangeUnit, FileDiff, ImpactSite, SignatureDelta, Symbol
from .signature import analyze_symbol, find_node, source_slice

TEST_RE = re.compile(r"(^|/)(tests?/|test_[^/]*\.py$|[^/]*_test\.py$|conftest\.py$)")


def is_test_path(path: str) -> bool:
    return bool(TEST_RE.search(path))


def estimate_tokens(text: str) -> int:
    """Real code tokenizes at ~3.4-3.8 chars/token; 3.3 keeps a safety margin
    without systematically underfilling the packet budget (the ollama-side
    `truncated` flag is the guardrail if we ever run over)."""
    return max(1, int(len(text) / 3.3))


def build_units(
    diffs: list[FileDiff],
    index: RepoIndex,
    cfg: Config,
    root: Path,
    base_rev: str,
) -> list[ChangeUnit]:
    units: list[ChangeUnit] = []
    try:
        co_changes = co_change_counts(root) if cfg.co_change else {}
    except Exception:  # noqa: BLE001 — history mining must never break a review
        co_changes = {}
    changed_paths = {d.path for d in diffs}

    for fd in diffs:
        if fd.binary or not fd.is_python:
            continue
        if fd.status == "D":
            units.append(_deleted_file_unit(fd, index, root, base_rev, cfg))
            continue

        new_source = index.source_of(fd.path)
        if new_source is None:
            continue
        old_source = show_file(base_rev, fd.old_path or fd.path, root) if fd.status != "A" else None

        touched = _group_by_symbol(fd, index, cfg)
        for symbol, lines in touched["symbols"]:
            unit = _symbol_unit(fd, symbol, lines, new_source, old_source, index, cfg)
            _note_co_changes(unit, co_changes, changed_paths)
            units.append(unit)
        for symbol in _removed_symbols(fd, old_source, index, root):
            units.append(_removed_unit(fd, symbol, old_source, index, cfg))
        if old_source and fd.status in ("M", "R"):
            units.extend(_field_units(fd, old_source, new_source, index, cfg))
        if touched["module_lines"]:
            units.append(
                _module_unit(fd, touched["module_lines"], new_source, index, cfg)
            )

    if all(d.status == "F" for d in diffs) and units:
        # Whole-file audit: no diff to prioritize by, so review by centrality —
        # the load-bearing symbols get the tokens first.
        from ..index.rank import pagerank

        rank = pagerank(index.graph)
        units.sort(
            key=lambda u: rank.get(u.symbol.fqname, 0.0) if u.symbol else 0.0,
            reverse=True,
        )
    else:
        units.sort(key=_unit_priority, reverse=True)
    return units


def _unit_priority(unit: ChangeUnit) -> tuple:
    return (
        unit.delta.breaking,
        unit.delta.is_contract_change,
        unit.total_callers,
        len(unit.diff_text),
    )


def _group_by_symbol(fd: FileDiff, index: RepoIndex, cfg: Config) -> dict:
    """Attribute every touched line to the callable that owns it."""
    by_symbol: dict[str, tuple[Symbol, set[int]]] = {}
    module_lines: set[int] = set()

    for line in sorted(fd.new_lines):
        symbol = index.symbols.owning_function(fd.path, line)
        if symbol is None:
            symbol = index.symbols.enclosing(fd.path, line)
        if symbol is None:
            module_lines.add(line)
            continue
        entry = by_symbol.setdefault(symbol.fqname, (symbol, set()))
        entry[1].add(line)

    # Deleted-only hunks (no new lines) still need attention.
    if not fd.new_lines and fd.old_lines:
        module_lines.update({h.new_start for h in fd.hunks})

    return {
        "symbols": [(sym, lines) for sym, lines in by_symbol.values()],
        "module_lines": module_lines,
    }


def _hunks_for(fd: FileDiff, lines: set[int]) -> str:
    """Only the hunks that overlap the given new-side lines."""
    if fd.status == "F":
        return "(whole-file review — no diff)"
    chosen = []
    for hunk in fd.hunks:
        span = range(hunk.new_start, hunk.new_start + max(hunk.new_count, 1))
        if lines & set(span):
            chosen.append(hunk.text())
    if not chosen:
        # No hunk overlaps the requested lines (e.g. a synthesized unit):
        # fall back to the whole diff rather than showing nothing.
        chosen = [h.text() for h in fd.hunks]
    return "\n".join(chosen)


def _symbol_unit(
    fd: FileDiff,
    symbol: Symbol,
    lines: set[int],
    new_source: str,
    old_source: str | None,
    index: RepoIndex,
    cfg: Config,
) -> ChangeUnit:
    if fd.status == "F":
        delta = SignatureDelta(kind="full_review", new_signature=symbol.signature)
    else:
        delta = analyze_symbol(symbol, old_source, new_source)

    unit = ChangeUnit(
        path=fd.path,
        symbol=symbol,
        delta=delta,
        diff_text=_hunks_for(fd, lines),
        new_source=source_slice(new_source, symbol, cfg.max_symbol_lines),
    )

    # The previous body is mostly the DIFF's minus-side restated; the old
    # signature is already in the contract block. Only behaviour deltas
    # ("no longer raises X") genuinely need the old body to judge against.
    if delta.is_contract_change and delta.behavior and old_source:
        old_node = find_node(old_source, symbol.qualname)
        if old_node is not None:
            unit.old_source = _node_slice(old_source, old_node, cfg.max_symbol_lines)

    if symbol.kind == "method":
        unit.overrides = index.symbols.overrides_of(symbol)
        if unit.overrides and delta.is_contract_change:
            unit.notes.append(
                "This method is overridden in subclasses (listed below). A contract "
                "change here usually requires matching changes in every override."
            )

    _attach_impact(unit, symbol, index, cfg)
    _attach_callees(unit, symbol, index, cfg)
    _pack(unit, cfg)
    return unit


def _node_slice(source: str, node, max_lines: int) -> str:
    lines = source.splitlines()
    start = min([d.lineno for d in getattr(node, "decorator_list", [])], default=node.lineno)
    end = getattr(node, "end_lineno", node.lineno)
    body = lines[start - 1 : end]
    if len(body) > max_lines:
        body = [*body[: max_lines - 5], f"# ... {len(body) - max_lines + 5} lines elided ..."]
    return "\n".join(body)


def _removed_symbols(
    fd: FileDiff, old_source: str | None, index: RepoIndex, root: Path
) -> list[Symbol]:
    """Symbols that existed at the base revision and are gone from the new tree.

    Deleting or renaming a called function is the harshest contract change there is,
    and it is invisible to the diff-hunk view: the breakage is entirely in other files.
    """
    if not old_source or fd.status in ("A", "F"):
        return []
    module = module_name(root, root / fd.path)
    old_symbols = extract_symbols(old_source, module, fd.path)
    if not old_symbols:
        return []
    surviving = {s.qualname for s in index.symbols.by_path.get(fd.path, [])}

    removed = []
    for symbol in old_symbols:
        if symbol.qualname in surviving or symbol.kind == "class":
            continue
        # only if the diff actually touched the lines it used to occupy
        span = set(range(symbol.start_lineno, symbol.end_lineno + 1))
        if fd.old_lines & span:
            removed.append(symbol)
    # a removed method inside a removed class would double-report
    return [s for s in removed if not any(s.qualname.startswith(f"{o.qualname}.") for o in removed)]


def _removed_unit(
    fd: FileDiff, symbol: Symbol, old_source: str, index: RepoIndex, cfg: Config
) -> ChangeUnit:
    delta = SignatureDelta(
        kind="removed",
        breaking=True,
        details=[f"{symbol.qualname} no longer exists in {fd.path}"],
        old_signature=symbol.signature,
    )
    unit = ChangeUnit(
        path=fd.path,
        symbol=symbol,
        delta=delta,
        diff_text=_hunks_for(fd, set()),
        new_source="",
        old_source=source_slice(old_source, symbol, cfg.max_symbol_lines),
        notes=[
            "This symbol was deleted or renamed. The call sites below were resolved "
            "by name, so some may be unrelated functions that share the name — check "
            "the import in each file before reporting it as broken."
        ],
    )
    sites = index.graph.by_name(symbol.short_name, exclude_path=fd.path)
    # Prefer sites that provably bound to THIS symbol; bare name-matches are only
    # worth showing when nothing exact exists (dynamic usage hint for the model).
    exact = [s for s in sites if s.target == symbol.fqname]
    if exact:
        sites = exact
        unit.notes = [
            "This symbol was deleted or renamed. The call sites below resolved to it "
            "through their imports and are now broken."
        ]
    unit.total_callers = len(sites)
    for site in sites[: cfg.max_callers]:
        snippet = _snippet(site, index, cfg.snippet_radius)
        if snippet is None:
            continue
        enclosing = index.symbols.owning_function(site.path, site.line)
        unit.callers.append(
            ImpactSite(
                site=site,
                snippet=snippet,
                enclosing_signature=enclosing.signature if enclosing else None,
                is_test=is_test_path(site.path),
            )
        )
    unit.dropped_callers = max(0, unit.total_callers - len(unit.callers))
    _pack(unit, cfg)
    return unit


def _field_units(
    fd: FileDiff, old_source: str, new_source: str, index: RepoIndex, cfg: Config
) -> list[ChangeUnit]:
    """One packet per class that lost data attributes in this change."""
    from .fields import removed_fields

    units: list[ChangeUnit] = []
    for symbol in index.symbols.by_path.get(fd.path, []):
        if symbol.kind != "class":
            continue
        lost = removed_fields(old_source, new_source, symbol.qualname)
        if not lost:
            continue
        names = [name for name, _ in lost]
        unit = ChangeUnit(
            path=fd.path,
            symbol=symbol,
            delta=SignatureDelta(
                kind="signature_changed",
                breaking=True,
                details=[f"field removed: {name}" for name in names],
                old_signature=f"{symbol.signature} with fields including {', '.join(names)}",
                new_signature=f"{symbol.signature} without {', '.join(names)}",
            ),
            diff_text=_hunks_for(fd, fd.new_lines),
            new_source=source_slice(new_source, symbol, cfg.max_symbol_lines),
            removed_fields=names,
            notes=[
                "Attribute(s) removed from this class. Every reader below now "
                "raises AttributeError."
            ],
        )
        for name in names:
            for site in index.graph.readers_of(symbol.fqname, name):
                if symbol.contains(site.line) and site.path == symbol.path:
                    continue  # reads inside the class body were part of the edit
                snippet = _snippet(site, index, cfg.snippet_radius)
                if snippet is None:
                    continue
                enclosing = index.symbols.owning_function(site.path, site.line)
                unit.callers.append(
                    ImpactSite(
                        site=site,
                        snippet=snippet,
                        enclosing_signature=enclosing.signature if enclosing else None,
                        is_test=is_test_path(site.path),
                    )
                )
        unit.total_callers = len(unit.callers)
        if unit.callers or unit.removed_fields:
            _pack(unit, cfg)
            units.append(unit)
    return units


def _module_unit(
    fd: FileDiff, lines: set[int], new_source: str, index: RepoIndex, cfg: Config
) -> ChangeUnit:
    src_lines = new_source.splitlines()
    window: list[str] = []
    shown = sorted(lines)[:60]
    width = len(str(shown[-1])) if shown else 1
    for line in shown:
        if 0 < line <= len(src_lines):
            window.append(f"{line:>{width}}|{src_lines[line - 1]}")
    unit = ChangeUnit(
        path=fd.path,
        symbol=None,
        delta=SignatureDelta(
            kind={"A": "added", "F": "full_review"}.get(fd.status, "body_only"),
            details=["module-level change (imports, constants, top-level code)"],
        ),
        diff_text=_hunks_for(fd, lines),
        new_source="\n".join(window),
        notes=["Module-level edit: check import cycles, side effects at import time, "
               "and constants that other modules read."],
    )
    _pack(unit, cfg)
    return unit


def _deleted_file_unit(
    fd: FileDiff, index: RepoIndex, root: Path, base_rev: str, cfg: Config
) -> ChangeUnit:
    old_path = fd.old_path or fd.path
    old_source = show_file(base_rev, old_path, root) or ""
    lost = extract_symbols(old_source, module_name(root, root / old_path), old_path)
    top_level = [s for s in lost if s.parent is None and s.kind != "class"]

    unit = ChangeUnit(
        path=fd.path,
        symbol=None,
        delta=SignatureDelta(
            kind="removed",
            breaking=True,
            details=[f"file deleted ({len(lost)} symbols)"]
            + [f"lost: {s.signature}" for s in top_level[:10]],
        ),
        diff_text=fd.text()[:4000],
        new_source="",
        old_source=old_source[:4000],
        notes=["Whole file deleted. Any surviving import of it now fails at import time."],
    )
    seen: set[tuple[str, int]] = set()
    all_sites = []
    for symbol in top_level:
        for site in index.graph.by_name(symbol.short_name, exclude_path=fd.path):
            key = (site.path, site.line)
            if key not in seen:
                seen.add(key)
                all_sites.append(site)
    unit.total_callers = len(all_sites)  # counted BEFORE capping, so "N omitted" renders
    for site in all_sites[: cfg.max_callers]:
        snippet = _snippet(site, index, cfg.snippet_radius)
        if snippet:
            unit.callers.append(
                ImpactSite(site=site, snippet=snippet, is_test=is_test_path(site.path))
            )
    unit.dropped_callers = unit.total_callers - len(unit.callers)
    _pack(unit, cfg)
    return unit


def _note_co_changes(unit: ChangeUnit, co_changes: dict, changed_paths: set[str]) -> None:
    """History says these files usually ship together — and this diff doesn't touch them."""
    if not unit.delta.is_contract_change:
        return
    row = co_changes.get(unit.path, {})
    if not row:
        return
    total = max(row.values())
    companions = [
        path
        for path, n in sorted(row.items(), key=lambda kv: -kv[1])[:3]
        if n >= 3 and n >= total * 0.4 and path not in changed_paths
    ]
    if companions:
        unit.notes.append(
            "Historically co-changed with this file but untouched in this diff: "
            + ", ".join(companions)
            + ". Worth asking whether they need a matching update."
        )


# --------------------------------------------------------------------------
# impact
# --------------------------------------------------------------------------


def _attach_impact(unit: ChangeUnit, symbol: Symbol, index: RepoIndex, cfg: Config) -> None:
    sites = index.graph.callers_of(symbol.fqname)
    sites = [s for s in sites if not (s.path == symbol.path and symbol.contains(s.line))]
    unit.total_callers = len(sites)

    contract = unit.delta.is_contract_change
    if not cfg.include_tests:
        # filter BEFORE slicing, or excluded test sites eat packet slots
        sites = [s for s in sites if not is_test_path(s.path)]
    scored = sorted(
        sites,
        key=lambda s: _site_score(s, symbol, contract),
        reverse=True,
    )

    for site in scored[: cfg.max_callers]:
        snippet = _snippet(site, index, cfg.snippet_radius)
        if snippet is None:
            continue
        enclosing = index.symbols.owning_function(site.path, site.line)
        unit.callers.append(
            ImpactSite(
                site=site,
                snippet=snippet,
                enclosing_signature=enclosing.signature if enclosing else None,
                is_test=is_test_path(site.path),
            )
        )
        # Two-hop: a wrapper that forwards *args/**kwargs hides the real arguments
        # one level up. For breaking changes, pull in the wrapper's own callers —
        # that's where the actual breakage lives.
        if (
            cfg.two_hop
            and contract
            and unit.delta.breaking
            and enclosing is not None
            and "*" in enclosing.signature
        ):
            _attach_second_hop(unit, enclosing, index, cfg)
    unit.dropped_callers = max(0, unit.total_callers - len(unit.callers))


def _attach_second_hop(unit: ChangeUnit, wrapper: Symbol, index: RepoIndex, cfg: Config) -> None:
    existing = {(i.site.path, i.site.line) for i in unit.callers}
    budget = max(0, cfg.max_callers + 4 - len(unit.callers))  # small, hard cap
    for site in index.graph.callers_of(wrapper.fqname)[:budget]:
        if (site.path, site.line) in existing:
            continue
        snippet = _snippet(site, index, cfg.snippet_radius)
        if snippet is None:
            continue
        enclosing = index.symbols.owning_function(site.path, site.line)
        unit.callers.append(
            ImpactSite(
                site=site,
                snippet=snippet,
                enclosing_signature=enclosing.signature if enclosing else None,
                is_test=is_test_path(site.path),
                hop=2,
                via=wrapper.fqname,
            )
        )
        existing.add((site.path, site.line))


def _site_score(site, symbol: Symbol, contract: bool) -> float:
    score = site.confidence
    if site.path != symbol.path:
        score += 0.3  # cross-file impact is the whole point
    if is_test_path(site.path):
        score += 0.15 if contract else -0.1
    return score


def _snippet(site, index: RepoIndex, radius: int) -> str | None:
    source = index.source_of(site.path)
    if source is None:
        return None
    lines = source.splitlines()
    start = max(site.line - radius - 1, 0)
    end = min(site.line + radius, len(lines))
    out = []
    width = len(str(end))
    for i in range(start, end):
        marker = ">" if i + 1 == site.line else " "
        out.append(f"{marker}{i + 1:>{width}}|{lines[i]}")
    return "\n".join(out)


def _attach_callees(unit: ChangeUnit, symbol: Symbol, index: RepoIndex, cfg: Config) -> None:
    if unit.delta.is_contract_change:
        return  # the review question is "which CALLERS break" — callees are noise here
    seen: set[str] = set()
    for edge in index.graph.callees_of(symbol.fqname):
        target = index.symbols.by_fq.get(edge.target)
        if target is None or target.fqname in seen or target.fqname == symbol.fqname:
            continue
        seen.add(target.fqname)
        unit.callees.append(target)
        if len(unit.callees) >= 6:
            break


# --------------------------------------------------------------------------
# budget packing
# --------------------------------------------------------------------------


def _pack(unit: ChangeUnit, cfg: Config) -> None:
    """Drop the lowest-value context until the packet fits the window."""
    budget = cfg.unit_budget

    def size() -> int:
        total = estimate_tokens(unit.diff_text) + estimate_tokens(unit.new_source)
        total += estimate_tokens(unit.old_source or "")
        total += sum(estimate_tokens(c.snippet) + 20 for c in unit.callers)
        total += 25 * len(unit.callees)
        return total + 200  # framing overhead

    while size() > budget and unit.callees:
        unit.callees.pop()
    while size() > budget and unit.callers:
        unit.callers.pop()  # already sorted best-first
        unit.dropped_callers += 1
    if size() > budget and unit.old_source:
        unit.old_source = None
        unit.notes.append("previous version elided to fit the context window")
    if size() > budget:
        keep = max(budget - estimate_tokens(unit.diff_text) - 200, 400) * 3
        if len(unit.new_source) > keep:
            unit.new_source = unit.new_source[:keep] + "\n... (truncated to fit context)"
            unit.notes.append("symbol body truncated to fit the context window")

    unit.tokens = size()
