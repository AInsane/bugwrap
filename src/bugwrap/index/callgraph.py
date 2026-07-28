"""Call graph construction: who calls what.

Tier 3 of Smart Context. Import-aware, relative-import-aware, and `self.`-aware,
with a unique-short-name fallback for calls it cannot resolve statically. Each
edge carries a resolution quality so the context builder can rank impact sites
instead of trusting them all equally.
"""

from __future__ import annotations

import ast
import builtins
from dataclasses import dataclass, field, replace

from ..models import CallSite
from .symbols import SymbolTable

# How many same-named definitions we are willing to attribute an unresolvable
# attribute call to before giving up. Beyond this the signal is worthless.
AMBIGUOUS_FANOUT = 3

_BUILTIN_NAMES = frozenset(dir(builtins))

# Protocol methods: every container implements them, so `x.append(...)` says nothing
# about which `append` in the repo is being called.
_PROTOCOL_METHODS = frozenset(
    """
    get set add append extend insert remove pop clear update items keys values copy
    read write close flush seek tell send recv connect start stop run join split
    strip encode decode count index find replace sort reverse
    put acquire release wait notify cancel result submit
    """.split()
)


def _annotation_name(node: ast.AST | None) -> str | None:
    """`Cart`, `cart.Cart`, `Optional[Cart]`, `Cart | None` -> the class name."""
    if node is None:
        return None
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return dotted_name(node)
    if isinstance(node, ast.Subscript):  # Optional[Cart], list[Cart] -> inner name
        return _annotation_name(node.slice)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):  # Cart | None
        return _annotation_name(node.left) or _annotation_name(node.right)
    if isinstance(node, ast.Constant) and isinstance(node.value, str):  # "Cart"
        return node.value
    return None


def dotted_name(node: ast.AST) -> str | None:
    """Flatten `a.b.c` / `a.b().c` into a dotted string where possible."""
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
        return ".".join(reversed(parts))
    if isinstance(current, ast.Call):  # chained: f().g -> give up on the head
        return None
    return None


class _CallCollector(ast.NodeVisitor):
    def __init__(self, module: str, rel_path: str, source_lines: list[str]) -> None:
        self.module = module
        self.rel_path = rel_path
        self.lines = source_lines
        self.aliases: dict[str, str] = {}  # local name -> fq symbol/module
        self.modmap: dict[str, str] = {}  # local name -> module path
        self.local_defs: set[str] = set()
        self.local_classes: set[str] = set()
        self.imports: set[str] = set()  # every module this file binds a name from
        self.calls: list[CallSite] = []
        # Attribute *reads* on receivers whose class is proven (self/typed only —
        # precision over recall, same policy as everywhere else). This is what
        # lets a removed dataclass field find its readers.
        self.reads: list[CallSite] = []
        self._stack: list[str] = []
        self._classes: list[str] = []
        # Flow-insensitive local type bindings, one dict per lexical scope:
        # `cart = Cart(...)` / `cart: Cart` / `def f(cart: Cart)` all prove that
        # `cart.add(...)` is a call into Cart. This is the cheap 80% of what an
        # LSP would buy us, from evidence that is actually in the file.
        self._types: list[dict[str, str]] = [{}]

    # -- imports ---------------------------------------------------------

    def visit_Import(self, node: ast.Import):  # noqa: N802
        for alias in node.names:
            if alias.asname:
                self.modmap[alias.asname] = alias.name
            else:
                head = alias.name.split(".")[0]
                self.modmap[head] = head
                self.modmap[alias.name] = alias.name
            self.imports.add(alias.name)

    def visit_ImportFrom(self, node: ast.ImportFrom):  # noqa: N802
        if node.level:
            pkg = self.module.split(".")[:-1]
            if node.level > 1:
                pkg = pkg[: -(node.level - 1)] if len(pkg) >= node.level - 1 else []
            base_parts = [*pkg, node.module] if node.module else pkg
            base = ".".join(p for p in base_parts if p)
        else:
            base = node.module or ""
        if base:
            self.imports.add(base)
        for alias in node.names:
            if alias.name == "*":
                continue
            self.imports.add(f"{base}.{alias.name}" if base else alias.name)
            local = alias.asname or alias.name
            target = f"{base}.{alias.name}" if base else alias.name
            self.aliases[local] = target
            self.modmap[local] = target

    # -- scopes ----------------------------------------------------------

    def _enter(self, node, is_class: bool):
        if not self._stack:
            self.local_defs.add(node.name)
            if is_class:
                self.local_classes.add(node.name)
        self._stack.append(node.name)
        if is_class:
            self._classes.append(node.name)
        self._types.append({})
        if not is_class:
            self._bind_annotated_params(node)
        for child in node.body:
            self.visit(child)
        self._types.pop()
        if is_class:
            self._classes.pop()
        self._stack.pop()

    def _bind_annotated_params(self, node) -> None:
        args = node.args
        for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
            if arg.annotation is None:
                continue
            cls = self._class_fq(_annotation_name(arg.annotation))
            if cls:
                self._types[-1][arg.arg] = cls

    def visit_FunctionDef(self, node):  # noqa: N802
        for dec in node.decorator_list:
            self.visit(dec)
        for default in [*node.args.defaults, *(d for d in node.args.kw_defaults if d)]:
            self.visit(default)
        self._enter(node, is_class=False)

    visit_AsyncFunctionDef = visit_FunctionDef  # noqa: N815

    def visit_ClassDef(self, node):  # noqa: N802
        for dec in node.decorator_list:
            self.visit(dec)
        for base in node.bases:
            self.visit(base)
        self._enter(node, is_class=True)

    # -- type bindings ---------------------------------------------------

    def _class_fq(self, name: str | None) -> str | None:
        """Resolve a (dotted) name to a class fqname, if it looks like a class."""
        if not name:
            return None
        last = name.rsplit(".", 1)[-1]
        if not last[:1].isupper():  # CapWords heuristic — cheap and usually right
            return None
        if name in self.aliases:
            return self.aliases[name]
        parts = name.split(".")
        for i in range(len(parts), 0, -1):
            prefix = ".".join(parts[:i])
            if prefix in self.modmap:
                return ".".join([self.modmap[prefix], *parts[i:]])
        if name in self.local_classes:
            return f"{self.module}.{name}"
        return None

    def _record_type(self, target, cls: str | None) -> None:
        if cls and isinstance(target, ast.Name):
            self._types[-1][target.id] = cls

    def _type_of(self, var: str) -> str | None:
        for scope in reversed(self._types):
            if var in scope:
                return scope[var]
        return None

    def visit_Assign(self, node: ast.Assign):  # noqa: N802
        if len(node.targets) == 1 and isinstance(node.value, ast.Call):
            cls = self._class_fq(dotted_name(node.value.func))
            self._record_type(node.targets[0], cls)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign):  # noqa: N802
        self._record_type(node.target, self._class_fq(_annotation_name(node.annotation)))
        self.generic_visit(node)

    # -- attribute reads -------------------------------------------------

    def visit_Attribute(self, node: ast.Attribute):  # noqa: N802
        if isinstance(node.ctx, ast.Load) and isinstance(node.value, ast.Name):
            receiver = node.value.id
            cls: str | None = None
            resolution = ""
            if receiver in ("self", "cls") and self._classes:
                cls = f"{self.module}.{'.'.join(self._classes)}"
                resolution = "self"
            elif (bound := self._type_of(receiver)) is not None:
                cls = bound
                resolution = "typed"
            if cls:
                caller = (
                    f"{self.module}.{'.'.join(self._stack)}"
                    if self._stack
                    else f"{self.module}.<module>"
                )
                line = node.lineno
                self.reads.append(
                    CallSite(
                        path=self.rel_path,
                        line=line,
                        caller=caller,
                        target=f"{cls}.{node.attr}",
                        raw=f"{receiver}.{node.attr}",
                        resolution=resolution,
                        text=self.lines[line - 1].strip() if 0 < line <= len(self.lines) else "",
                    )
                )
        self.generic_visit(node)

    # -- calls -----------------------------------------------------------

    def visit_Call(self, node: ast.Call):  # noqa: N802
        raw = dotted_name(node.func)
        if raw:
            target, resolution = self._resolve(raw)
            caller = f"{self.module}.{'.'.join(self._stack)}" if self._stack else f"{self.module}.<module>"
            line = node.lineno
            self.calls.append(
                CallSite(
                    path=self.rel_path,
                    line=line,
                    caller=caller,
                    target=target,
                    raw=raw,
                    resolution=resolution,
                    text=self.lines[line - 1].strip() if 0 < line <= len(self.lines) else "",
                )
            )
        self.generic_visit(node)

    def _resolve(self, raw: str) -> tuple[str, str]:
        parts = raw.split(".")
        head = parts[0]

        if head in ("self", "cls") and self._classes:
            owner = ".".join(self._classes)
            return f"{self.module}.{owner}." + ".".join(parts[1:]), "self"

        # `cart.add(...)` where a local binding proved cart's class
        if len(parts) > 1 and (cls := self._type_of(head)):
            return ".".join([cls, *parts[1:]]), "typed"

        if head in self.aliases and len(parts) == 1:
            return self.aliases[head], "import"

        # longest matching import prefix, so `pkg.mod.func` resolves cleanly
        for i in range(len(parts), 0, -1):
            prefix = ".".join(parts[:i])
            if prefix in self.modmap:
                tail = parts[i:]
                resolved = ".".join([self.modmap[prefix], *tail])
                return resolved, "import" if i == 1 and prefix in self.aliases else "module"

        if len(parts) == 1 and head in self.local_defs:
            return f"{self.module}.{head}", "local"

        # `obj.method()` on a receiver we cannot type. Kept as a distinct kind:
        # duck typing makes a unique method match a fair bet, a module-level
        # function match a bad one.
        if len(parts) > 1:
            return raw, "attr"

        return raw, "name"


@dataclass
class ModuleCalls:
    calls: list[CallSite]
    imports: set[str]
    reads: list[CallSite] = field(default_factory=list)  # reads on proven receivers


def collect_calls_from_tree(
    tree: ast.AST, module: str, rel_path: str, source_lines: list[str]
) -> ModuleCalls:
    collector = _CallCollector(module, rel_path, source_lines)
    collector.visit(tree)
    return ModuleCalls(collector.calls, collector.imports, collector.reads)


def collect_calls(source: str, module: str, rel_path: str) -> list[CallSite]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    return collect_calls_from_tree(tree, module, rel_path, source.splitlines()).calls


class CallGraph:
    """Reverse index from a symbol to every place that calls it."""

    def __init__(self, symbols: SymbolTable) -> None:
        self.symbols = symbols
        self.edges: list[CallSite] = []
        self._callers: dict[str, list[CallSite]] = {}
        self._callees: dict[str, list[CallSite]] = {}
        self._by_name: dict[str, list[CallSite]] = {}
        self._imports: dict[str, set[str]] = {}  # rel_path -> modules it imports from
        self.reads: list[CallSite] = []
        self._readers: dict[str, list[CallSite]] = {}  # "cls_fq.attr" -> read sites
        self.unresolved = 0

    def add(self, calls: list[CallSite], imports: set[str] | None = None) -> None:
        self.edges.extend(calls)
        if imports and calls:
            self._imports.setdefault(calls[0].path, set()).update(imports)

    def add_imports(self, rel_path: str, imports: set[str]) -> None:
        self._imports.setdefault(rel_path, set()).update(imports)

    def add_reads(self, reads: list[CallSite]) -> None:
        self.reads.extend(reads)

    @property
    def imports_by_path(self) -> dict[str, set[str]]:
        """Public view for consumers (hierarchy linking, full-context baselines)."""
        return self._imports

    @property
    def callers_by_target(self) -> dict[str, list[CallSite]]:
        """Public view of resolved edges for ranking (PageRank)."""
        return self._callers

    def readers_of(self, class_fq: str, attr: str) -> list[CallSite]:
        """Every proven read of `<class>.<attr>` — through subclasses too, since a
        field removed from Base also vanishes from every Derived that inherits it."""
        classes = [class_fq]
        stack = list(self.symbols.subclasses_of.get(class_fq, []))
        seen = set(classes)
        while stack:
            sub = stack.pop()
            if sub in seen:
                continue
            seen.add(sub)
            # a subclass that redefines the field keeps its readers working
            if f"{sub}.{attr}" not in self.symbols.by_fq:
                classes.append(sub)
            stack.extend(self.symbols.subclasses_of.get(sub, []))
        out: list[CallSite] = []
        dedupe: set[tuple[str, int]] = set()
        for cls in classes:
            for site in self._readers.get(f"{cls}.{attr}", []):
                key = (site.path, site.line)
                if key not in dedupe:
                    dedupe.add(key)
                    out.append(site)
        return out

    def link(self) -> None:
        """Second pass: bind each edge to a real symbol now that all files are indexed."""
        self._callers.clear()
        self._callees.clear()
        self._by_name.clear()
        self._readers.clear()
        self.unresolved = 0
        for read in self.reads:
            self._readers.setdefault(read.target, []).append(read)
        for edge in self.edges:
            # Name index over *every* edge, resolved or not. This is what finds the
            # callers of a symbol that the change deleted — it no longer exists to
            # resolve against, but the calls to it very much still do.
            self._by_name.setdefault(edge.raw.rsplit(".", 1)[-1], []).append(edge)
        for edge in self.edges:
            resolved = self.symbols.by_fq.get(edge.target)
            if resolved is None:
                resolved = self._inherited(edge.target)
            if resolved is None:
                resolved = self._fallback(edge)
            if resolved is None:
                continue
            edge.target = resolved.fqname
            self._callers.setdefault(resolved.fqname, []).append(edge)
            self._callees.setdefault(edge.caller, []).append(edge)

    def _inherited(self, target: str):
        """`pkg.mod.Derived.method` where Derived inherits method from a base:
        bind the call to the base's definition instead of dropping it."""
        if "." not in target:
            return None
        cls_part, method = target.rsplit(".", 1)
        cls = self.symbols.by_fq.get(cls_part)
        if cls is None or cls.kind != "class":
            return None
        found = self.symbols.resolve_method(cls_part, method)
        # resolve_method also returns the class's own method, but that case was
        # already a by_fq hit — reaching here means it came from a base.
        return found

    def _fallback(self, edge: CallSite):
        """Resolve a call the import map could not bind, without inventing edges.

        The governing rule is *evidence*: a name match only counts if the calling
        file could plausibly be calling that symbol — same file, or it imports the
        module the candidate lives in. Without this, `d.get(k)` makes every dict
        access in the repo a caller of some unrelated module-level `get`, and a
        packet fills up with call sites that have nothing to do with the change.
        """
        short = edge.target.rsplit(".", 1)[-1]
        if edge.resolution == "name" and short in _BUILTIN_NAMES:
            self.unresolved += 1
            return None
        if edge.resolution in ("attr", "typed") and short in _PROTOCOL_METHODS:
            # An untyped receiver tells us nothing, and the method name tells us
            # nothing either when it is one every container in Python implements.
            self.unresolved += 1
            return None

        matches = self.symbols.by_short.get(short, [])
        if not matches:
            return None

        # A dotted qualname tail (`Cart.total` matching `shop.cart.Cart.total`) is real
        # evidence. A bare one is not: without the dot boundary, `anything.get` would
        # "tail match" every module-level function named `get` in the repo.
        tail = [
            m
            for m in matches
            if "." in m.qualname and edge.target.endswith(f".{m.qualname}")
        ]
        if len(tail) == 1:
            return tail[0]

        supported = [m for m in matches if self._has_evidence(edge, m)]
        if len(supported) == 1:
            return supported[0]
        if 1 < len(supported) <= AMBIGUOUS_FANOUT:
            # A couple of plausible same-named definitions: attribute a per-candidate
            # COPY to each (a shared edge would carry one stale target string) and
            # let the low confidence sink them in the ranking.
            for match in supported:
                self._callers.setdefault(match.fqname, []).append(
                    replace(edge, target=match.fqname, resolution="ambiguous")
                )
            return None

        self.unresolved += 1
        return None

    def _has_evidence(self, edge: CallSite, candidate) -> bool:
        if candidate.path == edge.path:
            return True
        imported = self._imports.get(edge.path)
        if not imported:
            return False
        module = candidate.module
        # Either the file imported that module, or it imported a name out of it.
        # Deliberately NOT the reverse: `import re` is no evidence of a call into
        # `re._parser`, and treating it as such was worth ~900 phantom call sites.
        return any(
            target == module or target.startswith(f"{module}.") for target in imported
        )

    def callers_of(self, fqname: str) -> list[CallSite]:
        sites = self._callers.get(fqname, [])
        # de-dup identical (path, line)
        seen: set[tuple[str, int]] = set()
        unique: list[CallSite] = []
        for site in sites:
            key = (site.path, site.line)
            if key in seen:
                continue
            seen.add(key)
            unique.append(site)
        return unique

    def callees_of(self, caller_fqname: str) -> list[CallSite]:
        return self._callees.get(caller_fqname, [])

    def by_name(self, short_name: str, exclude_path: str | None = None) -> list[CallSite]:
        """Every call written as `...short_name(...)`, however it resolved.

        Used for symbols that no longer exist in the new tree (deleted or renamed),
        where there is nothing left for the resolver to bind to.
        """
        sites = [s for s in self._by_name.get(short_name, []) if s.path != exclude_path]
        # Sites that bound the name through a real import are far likelier to be the
        # thing that broke than an incidental method call sharing the name.
        sites.sort(key=lambda s: s.confidence, reverse=True)
        seen: set[tuple[str, int]] = set()
        unique: list[CallSite] = []
        for site in sites:
            key = (site.path, site.line)
            if key not in seen:
                seen.add(key)
                unique.append(site)
        return unique

    @property
    def stats(self) -> dict:
        resolved = sum(len(v) for v in self._callers.values())
        return {
            "edges": len(self.edges),
            "resolved": resolved,
            "targets": len(self._callers),
            "too_ambiguous": self.unresolved,
        }
