"""Deterministic contract checking at call sites.

CodeRabbit's precision comes from a linter/SAST layer under the LLM. This is our
version of that layer, specialized to the one thing our call graph makes provable:
when a signature changes, re-bind every known call site against the new signature
exactly the way CPython would, and report the ones that can no longer bind.

These findings don't need a model, don't cost tokens, and are near-100% precision —
they are emitted only when the breakage is provable from the AST. Anything uncertain
(star-args, **kwargs passthrough, decorated defs that rewrap the signature) is left
for the LLM pass instead of guessed at.
"""

from __future__ import annotations

import ast
import functools

from ..models import ChangeUnit, Finding, ImpactSite, Param, Symbol
from .signature import find_node, params_of

# Decorators that replace or rewrap the callable's signature; binding against the
# def's params would be wrong, so those symbols are skipped entirely.
# NOTE: matched by str.startswith — "cache" deliberately also catches
# "cached_property", "fixture" catches parametrized fixtures, "click." catches
# every click decorator.
_SIGNATURE_REWRAPPERS = (
    "contextmanager",
    "lru_cache",
    "cache",
    "singledispatch",
    "singledispatchmethod",
    "pytest.fixture",
    "fixture",
    "click.",
    "app.route",
)


def check_units(units: list[ChangeUnit], sources: dict[str, str]) -> list[Finding]:
    """Run every deterministic check across all packets.

    `sources` maps rel_path -> file text for every file that hosts a call site.
    """
    findings: list[Finding] = []
    for unit in units:
        if unit.symbol is None:
            continue
        if unit.removed_fields:
            findings.extend(_check_removed_fields(unit))
        elif unit.delta.kind == "removed":
            findings.extend(_check_removed(unit))
        elif unit.delta.kind == "signature_changed":
            findings.extend(_check_signature(unit, sources))
            findings.extend(_check_overrides(unit, sources))
    return findings


def _check_removed_fields(unit: ChangeUnit) -> list[Finding]:
    """Readers of a field the class no longer defines: AttributeError, provably."""
    out: list[Finding] = []
    removed = set(unit.removed_fields)
    for impact in unit.callers:
        if impact.hop != 1 or impact.site.resolution not in ("self", "typed"):
            continue
        attr = impact.site.target.rsplit(".", 1)[-1]
        if attr not in removed:
            continue
        out.append(
            Finding(
                file=impact.site.path,
                line=impact.site.line,
                symbol=unit.symbol.fqname if unit.symbol else None,
                severity="high",
                category="contract",
                title=f"reads removed attribute `{attr}`",
                detail=(
                    f"`{unit.symbol.fqname if unit.symbol else unit.path}` no longer "
                    f"defines `{attr}`, but this line still reads it: "
                    f"`{impact.site.text}`. This raises AttributeError at runtime."
                ),
                confidence=0.9,
                suggestion=f"remove the read or restore the `{attr}` attribute",
            )
        )
    return out


# --------------------------------------------------------------------------
# removed symbols
# --------------------------------------------------------------------------


def _check_removed(unit: ChangeUnit) -> list[Finding]:
    out: list[Finding] = []
    for impact in unit.callers:
        # Only certain resolutions prove the call bound to *this* symbol.
        if impact.hop != 1 or impact.site.resolution not in ("import", "module", "self", "local"):
            continue
        # Identity, not just name: `subprocess.run()` resolves to subprocess.run,
        # which merely SHARES a name with a deleted `tasks.run`. The index is
        # built on the new tree, so a caller whose import was updated in the same
        # PR resolves to the new location and correctly drops out here too.
        if impact.site.target != unit.symbol.fqname:
            continue
        out.append(
            Finding(
                file=impact.site.path,
                line=impact.site.line,
                symbol=unit.symbol.fqname if unit.symbol else None,
                severity="critical",
                category="contract",
                title=f"call to removed symbol `{unit.symbol.short_name}`",
                detail=(
                    f"`{unit.symbol.qualname}` was deleted from {unit.path}, but this "
                    f"call site still references it: `{impact.site.text}`. This raises "
                    "NameError/AttributeError (or ImportError at module load)."
                ),
                confidence=0.95,
                suggestion="update or remove the call, or restore the symbol",
            )
        )
    return out


# --------------------------------------------------------------------------
# signature changes
# --------------------------------------------------------------------------


def _check_signature(unit: ChangeUnit, sources: dict[str, str]) -> list[Finding]:
    symbol = unit.symbol
    if any(d.startswith(_SIGNATURE_REWRAPPERS) for d in symbol.decorators):
        return []

    new_source = sources.get(symbol.path)
    if new_source is None:
        return []
    node = find_node(new_source, symbol.qualname)
    if node is None or not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return []
    params = params_of(node)
    went_async = unit.delta.went_async

    out: list[Finding] = []
    for impact in unit.callers:
        if impact.hop != 1:
            continue  # 2-hop sites call the wrapper, not this symbol — never bind-check
        call = _call_node_at(sources.get(impact.site.path), impact.site.line, symbol.short_name)
        if call is None:
            continue
        problems = bind_call(call, params, symbol, impact)
        if problems:
            # One finding per call site, like one review comment per line.
            summary = problems[0] + (f" (+{len(problems) - 1} more)" if len(problems) > 1 else "")
            out.append(
                Finding(
                    file=impact.site.path,
                    line=impact.site.line,
                    symbol=symbol.fqname,
                    severity="high",
                    category="contract",
                    title=f"`{symbol.short_name}()` call no longer binds: {summary}",
                    detail=(
                        f"The signature changed to `{unit.delta.new_signature}` "
                        f"(was `{unit.delta.old_signature}`). This call — "
                        f"`{impact.site.text}` — "
                        + "; ".join(problems)
                        + ". This raises TypeError at runtime."
                    ),
                    confidence=0.95,
                    suggestion="update the call to match the new signature",
                )
            )
        if went_async and isinstance(node, ast.AsyncFunctionDef):
            if not _is_awaited(sources.get(impact.site.path), impact.site.line, call):
                out.append(
                    Finding(
                        file=impact.site.path,
                        line=impact.site.line,
                        symbol=symbol.fqname,
                        severity="high",
                        category="contract",
                        title=f"`{symbol.short_name}()` became async but is not awaited here",
                        detail=(
                            f"`{symbol.qualname}` is now a coroutine function; this call "
                            "site uses the result without `await`, so it gets a coroutine "
                            "object instead of the value (and the coroutine never runs)."
                        ),
                        confidence=0.9,
                        suggestion="await the call (the enclosing function may need to become async)",
                    )
                )
    return out


# --------------------------------------------------------------------------
# override divergence
# --------------------------------------------------------------------------


def _required_names(params: list[Param]) -> tuple[list[str], set[str]]:
    """(positional order, all required names), ignoring self/cls."""
    ps = [p for p in params if p.name not in ("self", "cls")]
    positional = [p.name for p in ps if p.kind in ("posonly", "arg")]
    required = {p.name for p in ps if p.kind in ("posonly", "arg", "kwonly") and not p.has_default}
    return positional, required


def _check_overrides(unit: ChangeUnit, sources: dict[str, str]) -> list[Finding]:
    """Parent method signature changed — do subclass overrides still match?

    Callers hold references polymorphically; an override that can no longer accept
    what the new parent signature accepts breaks substitution at runtime.
    """
    if not unit.overrides or unit.symbol is None:
        return []
    parent_source = sources.get(unit.symbol.path)
    if parent_source is None:
        return []
    parent_node = find_node(parent_source, unit.symbol.qualname)
    if not isinstance(parent_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return []
    if any(p.kind in ("vararg", "kwarg") for p in params_of(parent_node)):
        return []  # flexible parent — overrides can't be judged mechanically
    parent_pos, parent_req = _required_names(params_of(parent_node))

    out: list[Finding] = []
    for override in unit.overrides:
        override_source = sources.get(override.path)
        if override_source is None:
            continue
        node = find_node(override_source, override.qualname)
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        params = params_of(node)
        if any(p.kind in ("vararg", "kwarg") for p in params):
            continue  # *args/**kwargs override absorbs anything
        override_pos, override_req = _required_names(params)

        problems: list[str] = []
        if len(override_pos) < len(parent_pos):
            missing = parent_pos[len(override_pos):]
            problems.append(f"cannot accept positional argument(s) {', '.join(missing)}")
        extra_required = override_req - parent_req - set(parent_pos)
        if extra_required:
            problems.append(
                f"requires {', '.join(sorted(extra_required))} which the parent does not"
            )
        if problems:
            out.append(
                Finding(
                    file=override.path,
                    line=override.lineno,
                    symbol=override.fqname,
                    severity="high",
                    category="contract",
                    title=f"override `{override.qualname}` diverges from changed parent signature",
                    detail=(
                        f"`{unit.symbol.fqname}` changed to `{unit.delta.new_signature}`, "
                        f"but this override (`{override.signature}`) "
                        + "; ".join(problems)
                        + ". Code calling through the base class will break on this subclass."
                    ),
                    confidence=0.85,
                    suggestion="update the override to match the new parent signature",
                )
            )
    return out


# --------------------------------------------------------------------------
# locating the call expression
# --------------------------------------------------------------------------

@functools.lru_cache(maxsize=256)
def _parse(source: str) -> ast.AST | None:
    # Keyed by the text itself — never by id(), which gets recycled.
    try:
        return ast.parse(source)
    except SyntaxError:
        return None


def _call_node_at(source: str | None, line: int, short_name: str) -> ast.Call | None:
    """The ast.Call on `line` whose callee's last attribute is `short_name`."""
    if source is None:
        return None
    tree = _parse(source)
    if tree is None:
        return None
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or node.lineno != line:
            continue  # sites are recorded at the call's first line, so == is exact
        callee = node.func
        name = None
        if isinstance(callee, ast.Name):
            name = callee.id
        elif isinstance(callee, ast.Attribute):
            name = callee.attr
        if name == short_name:
            return node
    return None


def _is_awaited(source: str | None, line: int, call: ast.Call) -> bool:
    if source is None:
        return True  # can't prove otherwise — stay silent
    tree = _parse(source)
    if tree is None:
        return True
    for node in ast.walk(tree):
        if isinstance(node, ast.Await) and isinstance(node.value, ast.Call):
            if node.value.lineno == call.lineno and node.value.col_offset == call.col_offset:
                return True
    # `asyncio.run(f())`, `create_task(f())`, `gather(f())` also consume coroutines
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and call in getattr(node, "args", []):
            return True
    return False


# --------------------------------------------------------------------------
# argument binding — mirrors CPython's rules, conservatively
# --------------------------------------------------------------------------


def bind_call(
    call: ast.Call,
    params: list[Param],
    symbol: Symbol,
    impact: ImpactSite,
) -> list[str]:
    """Return provable binding failures for this call, or [] if it binds (or might).

    Conservative on purpose: any `*args` / `**kwargs` at the call site widens what
    could bind, so related checks are skipped rather than guessed.
    """
    ps = list(params)

    # Bound method call: `self` / `cls` is supplied implicitly.
    if symbol.kind == "method" and ps and ps[0].name in ("self", "cls"):
        is_attribute_call = isinstance(call.func, ast.Attribute)
        if "staticmethod" in symbol.decorators:
            pass  # no implicit first arg
        elif is_attribute_call or impact.site.resolution == "self":
            ps = ps[1:]
        else:
            # direct/unbound use — too unusual to reason about safely
            return []

    has_star = any(isinstance(a, ast.Starred) for a in call.args)
    has_double_star = any(kw.arg is None for kw in call.keywords)

    positional_params = [p for p in ps if p.kind in ("posonly", "arg")]
    accepts_vararg = any(p.kind == "vararg" for p in ps)
    accepts_kwarg = any(p.kind == "kwarg" for p in ps)

    n_positional = len(call.args)
    supplied_kw = {kw.arg for kw in call.keywords if kw.arg is not None}
    problems: list[str] = []

    # 1. too many positional arguments
    if not has_star and not accepts_vararg and n_positional > len(positional_params):
        problems.append(
            f"passes {n_positional} positional argument(s) but the function now "
            f"takes at most {len(positional_params)}"
        )

    # 2. unexpected keyword argument
    if not accepts_kwarg:
        valid_kw = {p.name for p in ps if p.kind in ("arg", "kwonly")}
        for name in sorted(supplied_kw - valid_kw):
            problems.append(f"passes unexpected keyword argument `{name}`")

    # 3. keyword targeting a positional-only parameter
    posonly_names = {p.name for p in ps if p.kind == "posonly"}
    for name in sorted(supplied_kw & posonly_names):
        if not accepts_kwarg:
            problems.append(f"passes positional-only parameter `{name}` as a keyword")

    # 4. missing required argument (only provable with no unpacking at the site)
    if not has_star and not has_double_star:
        filled = {p.name for p in positional_params[:n_positional]}
        for p in ps:
            if p.kind in ("posonly", "arg", "kwonly") and not p.has_default:
                if p.name in filled:
                    continue
                if p.kind != "posonly" and p.name in supplied_kw:
                    continue
                problems.append(f"is missing required argument `{p.name}`")

    # 5. duplicate: same param filled positionally and by keyword
    for p in positional_params[:n_positional]:
        if p.name in supplied_kw:
            problems.append(f"passes `{p.name}` both positionally and as a keyword")

    return problems
