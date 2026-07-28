"""Contract-change detection.

Tier 2 of Smart Context. A body-only edit is a local concern; a *signature*
change is a contract change whose blast radius is every call site in the repo.
Distinguishing the two is what lets the reviewer spend its context where it
matters, and is the difference between "looks fine" and "this breaks order.py:88".
"""

from __future__ import annotations

import ast

from ..models import Param, SignatureDelta, Symbol
from ..index.symbols import signature_text

_FUNC = (ast.FunctionDef, ast.AsyncFunctionDef)


def find_node(source: str, qualname: str) -> ast.AST | None:
    """Locate a definition by dotted qualname within a module's source."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None

    parts = qualname.split(".")
    node: ast.AST = tree
    for part in parts:
        body = getattr(node, "body", None)
        if body is None:
            return None
        for child in body:
            if isinstance(child, (*_FUNC, ast.ClassDef)) and child.name == part:
                node = child
                break
        else:
            return None
    return node


def params_of(node: ast.AST) -> list[Param]:
    if not isinstance(node, _FUNC):
        return []
    args = node.args
    out: list[Param] = []

    def ann(a: ast.arg) -> str | None:
        return ast.unparse(a.annotation) if a.annotation else None

    positional = [*args.posonlyargs, *args.args]
    defaults = list(args.defaults)
    pad = len(positional) - len(defaults)
    for i, a in enumerate(positional):
        has_default = i >= pad
        default = ast.unparse(defaults[i - pad]) if has_default else None
        kind = "posonly" if i < len(args.posonlyargs) else "arg"
        out.append(Param(a.arg, kind, ann(a), has_default, default))

    if args.vararg:
        out.append(Param(args.vararg.arg, "vararg", ann(args.vararg), True))
    for a, d in zip(args.kwonlyargs, args.kw_defaults):
        out.append(
            Param(a.arg, "kwonly", ann(a), d is not None, ast.unparse(d) if d else None)
        )
    if args.kwarg:
        out.append(Param(args.kwarg.arg, "kwarg", ann(args.kwarg), True))
    return out


def _returns(node: ast.AST) -> str | None:
    returns = getattr(node, "returns", None)
    return ast.unparse(returns) if returns else None


# --------------------------------------------------------------------------
# behaviour contract: what a function does to its callers beyond its signature
# --------------------------------------------------------------------------


def _own_body(node: ast.AST):
    """Walk a function's body without descending into nested defs/classes."""
    stack = list(getattr(node, "body", []))
    while stack:
        current = stack.pop()
        if isinstance(current, (*_FUNC, ast.ClassDef)):
            continue
        yield current
        for child in ast.iter_child_nodes(current):
            stack.append(child)


def raise_set(node: ast.AST) -> set[str]:
    """Exception names raised directly in this function's own body."""
    out: set[str] = set()
    for stmt in _own_body(node):
        if not isinstance(stmt, ast.Raise) or stmt.exc is None:
            continue
        exc = stmt.exc
        if isinstance(exc, ast.Call):
            exc = exc.func
        if isinstance(exc, ast.Name):
            out.add(exc.id)
        elif isinstance(exc, ast.Attribute):
            out.add(exc.attr)
    return out


def is_generator(node: ast.AST) -> bool:
    return any(isinstance(n, (ast.Yield, ast.YieldFrom)) for n in _own_body(node))


def return_shapes(node: ast.AST) -> set[str]:
    """Coarse shapes of the values this function returns."""
    shapes: set[str] = set()
    for stmt in _own_body(node):
        if not isinstance(stmt, ast.Return):
            continue
        value = stmt.value
        if value is None or (isinstance(value, ast.Constant) and value.value is None):
            shapes.add("None")
        elif isinstance(value, ast.Tuple):
            shapes.add(f"tuple[{len(value.elts)}]")
        elif isinstance(value, (ast.Dict, ast.DictComp)):
            shapes.add("dict")
        elif isinstance(value, (ast.List, ast.ListComp)):
            shapes.add("list")
        else:
            shapes.add("value")
    return shapes or {"None"}  # falls off the end -> implicit None


def behavior_delta(old_node: ast.AST, new_node: ast.AST) -> tuple[list[str], bool]:
    """(human-readable changes, any_breaking) for raise/return/generator contracts."""
    if not (isinstance(old_node, _FUNC) and isinstance(new_node, _FUNC)):
        return [], False
    notes: list[str] = []
    breaking = False

    old_gen, new_gen = is_generator(old_node), is_generator(new_node)
    if old_gen != new_gen:
        direction = "function -> generator" if new_gen else "generator -> function"
        notes.append(
            f"{direction} — every call site now receives a "
            f"{'generator object' if new_gen else 'plain value'}"
        )
        breaking = True

    added = raise_set(new_node) - raise_set(old_node)
    removed = raise_set(old_node) - raise_set(new_node)
    if added:
        notes.append(f"now raises: {', '.join(sorted(added))} — callers may not catch these")
    if removed:
        notes.append(f"no longer raises: {', '.join(sorted(removed))}")

    if not (old_gen or new_gen):
        old_shapes, new_shapes = return_shapes(old_node), return_shapes(new_node)
        if old_shapes != new_shapes:
            notes.append(
                f"return shape changed: {' | '.join(sorted(old_shapes))} -> "
                f"{' | '.join(sorted(new_shapes))}"
            )
    return notes, breaking


def compare(old_node: ast.AST | None, new_node: ast.AST | None) -> SignatureDelta:
    if old_node is None and new_node is None:
        return SignatureDelta(kind="body_only")
    if old_node is None:
        return SignatureDelta(
            kind="added",
            new_signature=signature_text(new_node) if new_node else None,
            details=["new definition"],
        )
    if new_node is None:
        return SignatureDelta(
            kind="removed",
            breaking=True,
            old_signature=signature_text(old_node),
            details=["definition removed or renamed"],
        )

    old_sig = signature_text(old_node)
    new_sig = signature_text(new_node)
    details: list[str] = []
    breaking = False
    went_async = False

    if isinstance(old_node, _FUNC) and isinstance(new_node, _FUNC):
        old_params = {p.name: p for p in params_of(old_node)}
        new_params = {p.name: p for p in params_of(new_node)}
        old_order = [p.name for p in params_of(old_node) if p.kind in ("posonly", "arg")]
        new_order = [p.name for p in params_of(new_node) if p.kind in ("posonly", "arg")]

        for name in old_params.keys() - new_params.keys():
            details.append(f"parameter removed: {name}")
            breaking = True
        for name in new_params.keys() - old_params.keys():
            param = new_params[name]
            if param.has_default or param.kind in ("vararg", "kwarg"):
                details.append(f"optional parameter added: {name}")
            else:
                details.append(f"REQUIRED parameter added: {name}")
                breaking = True

        for name in old_params.keys() & new_params.keys():
            old_p, new_p = old_params[name], new_params[name]
            if old_p.kind != new_p.kind:
                details.append(f"{name}: {old_p.kind} -> {new_p.kind}")
                breaking = True
            if old_p.has_default and not new_p.has_default:
                details.append(f"{name}: default removed")
                breaking = True
            elif not old_p.has_default and new_p.has_default:
                details.append(f"{name}: default added ({new_p.default})")
            elif old_p.default != new_p.default:
                details.append(f"{name}: default {old_p.default} -> {new_p.default}")
            if old_p.annotation != new_p.annotation:
                details.append(
                    f"{name}: type {old_p.annotation or 'untyped'} -> {new_p.annotation or 'untyped'}"
                )

        common = [n for n in old_order if n in new_order]
        if [n for n in new_order if n in common] != common:
            details.append("positional parameter order changed")
            breaking = True

        if _returns(old_node) != _returns(new_node):
            details.append(
                f"return type {_returns(old_node) or 'untyped'} -> {_returns(new_node) or 'untyped'}"
            )

        if isinstance(old_node, ast.AsyncFunctionDef) != isinstance(new_node, ast.AsyncFunctionDef):
            details.append("sync/async changed — every call site needs await added or removed")
            breaking = True
            went_async = isinstance(new_node, ast.AsyncFunctionDef)

    old_decorators = [ast.unparse(d) for d in getattr(old_node, "decorator_list", [])]
    new_decorators = [ast.unparse(d) for d in getattr(new_node, "decorator_list", [])]
    if old_decorators != new_decorators:
        details.append(f"decorators {old_decorators or '[]'} -> {new_decorators or '[]'}")
        contract_decorators = {"property", "staticmethod", "classmethod", "cached_property"}
        changed = set(old_decorators) ^ set(new_decorators)
        breaking = breaking or bool(changed & contract_decorators)

    behavior, behavior_breaking = behavior_delta(old_node, new_node)

    if details or old_sig != new_sig:
        return SignatureDelta(
            kind="signature_changed",
            breaking=breaking or behavior_breaking,
            details=details or ["signature text changed"],
            old_signature=old_sig,
            new_signature=new_sig,
            behavior=behavior,
            went_async=went_async,
        )
    return SignatureDelta(
        kind="body_only",
        breaking=behavior_breaking,
        old_signature=old_sig,
        new_signature=new_sig,
        behavior=behavior,
    )


def analyze_symbol(symbol: Symbol, old_source: str | None, new_source: str) -> SignatureDelta:
    new_node = find_node(new_source, symbol.qualname)
    old_node = find_node(old_source, symbol.qualname) if old_source else None
    return compare(old_node, new_node)


def source_slice(source: str, symbol: Symbol, max_lines: int = 400) -> str:
    lines = source.splitlines()
    start = max(symbol.start_lineno - 1, 0)
    end = min(symbol.end_lineno, len(lines))
    # Minimal-width gutter: at ~40 chars per code line, a padded 8-char gutter
    # is ~18% of the section; "N|" keeps citations possible at a fraction of that.
    width = len(str(end))
    numbered = [
        f"{symbol.start_lineno + i:>{width}}|{line}" for i, line in enumerate(lines[start:end])
    ]
    if len(numbered) > max_lines:
        elided = len(numbered) - (max_lines - 20) - 15
        numbered = [
            *numbered[: max_lines - 20],
            f"...| {elided} lines elided ...",
            *numbered[-15:],
        ]
    return "\n".join(numbered)
