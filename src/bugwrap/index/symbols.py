"""AST symbol extraction: which function owns which line.

Tier 1 of Smart Context. Everything downstream — the call graph, signature
diffing, snippet extraction — is keyed off the symbols produced here.
"""

from __future__ import annotations

import ast
from pathlib import Path

from ..models import Symbol

_FUNC = (ast.FunctionDef, ast.AsyncFunctionDef)


def module_name(root: Path, path: Path) -> str:
    """Dotted module name, walking up while __init__.py exists (src-layout aware)."""
    package: list[str] = []
    directory = path.parent
    while (directory / "__init__.py").exists():
        package.append(directory.name)
        if directory == root or directory.parent == directory:
            break
        directory = directory.parent
    package.reverse()

    stem = path.stem
    if stem == "__init__":
        return ".".join(package) if package else path.parent.name
    return ".".join([*package, stem])


def signature_text(node: ast.AST) -> str:
    if isinstance(node, _FUNC):
        prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
        args = ast.unparse(node.args)
        returns = f" -> {ast.unparse(node.returns)}" if node.returns else ""
        return f"{prefix} {node.name}({args}){returns}"
    if isinstance(node, ast.ClassDef):
        bases = [ast.unparse(b) for b in node.bases]
        bases += [f"{kw.arg}={ast.unparse(kw.value)}" for kw in node.keywords]
        joined = f"({', '.join(bases)})" if bases else ""
        return f"class {node.name}{joined}"
    return ""


class _Collector(ast.NodeVisitor):
    def __init__(self, module: str, rel_path: str) -> None:
        self.module = module
        self.rel_path = rel_path
        self.symbols: list[Symbol] = []
        self._stack: list[str] = []
        self._kinds: list[str] = []

    def _record(self, node, kind: str) -> None:
        qualname = ".".join([*self._stack, node.name])
        decorators = tuple(ast.unparse(d) for d in node.decorator_list)
        start = min([d.lineno for d in node.decorator_list], default=node.lineno)
        bases = (
            tuple(ast.unparse(b) for b in node.bases)
            if isinstance(node, ast.ClassDef)
            else ()
        )
        self.symbols.append(
            Symbol(
                module=self.module,
                qualname=qualname,
                kind=kind,
                path=self.rel_path,
                lineno=node.lineno,
                end_lineno=getattr(node, "end_lineno", node.lineno),
                start_lineno=start,
                signature=signature_text(node),
                is_async=isinstance(node, ast.AsyncFunctionDef),
                decorators=decorators,
                parent=".".join(self._stack) or None,
                bases=bases,
            )
        )
        self._stack.append(node.name)
        self._kinds.append(kind)
        for child in node.body:
            self.visit(child)
        self._kinds.pop()
        self._stack.pop()

    def visit_FunctionDef(self, node):  # noqa: N802
        kind = "method" if self._kinds and self._kinds[-1] == "class" else "function"
        self._record(node, kind)

    visit_AsyncFunctionDef = visit_FunctionDef  # noqa: N815

    def visit_ClassDef(self, node):  # noqa: N802
        self._record(node, "class")


def extract_symbols_from_tree(tree: ast.AST, module: str, rel_path: str) -> list[Symbol]:
    collector = _Collector(module, rel_path)
    collector.visit(tree)
    return collector.symbols


def extract_symbols(source: str, module: str, rel_path: str) -> list[Symbol]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    return extract_symbols_from_tree(tree, module, rel_path)


class SymbolTable:
    """Repo-wide symbol index with line-precise lookup."""

    def __init__(self) -> None:
        self.symbols: list[Symbol] = []
        self.by_fq: dict[str, Symbol] = {}
        self.by_short: dict[str, list[Symbol]] = {}
        self.by_path: dict[str, list[Symbol]] = {}
        self.modules: dict[str, str] = {}  # module -> path
        self.bases_of: dict[str, list[str]] = {}  # class fq -> resolved base fqs
        self.subclasses_of: dict[str, list[str]] = {}  # class fq -> subclass fqs

    def add(self, symbols: list[Symbol]) -> None:
        for sym in symbols:
            self.symbols.append(sym)
            self.by_fq.setdefault(sym.fqname, sym)
            self.by_short.setdefault(sym.short_name, []).append(sym)
            self.by_path.setdefault(sym.path, []).append(sym)
            self.modules.setdefault(sym.module, sym.path)

    def enclosing(self, path: str, line: int) -> Symbol | None:
        """Innermost symbol containing `line`, preferring functions over classes."""
        candidates = [s for s in self.by_path.get(path, []) if s.contains(line)]
        if not candidates:
            return None
        # Smallest span wins; on a tie prefer a callable over its class.
        candidates.sort(key=lambda s: (s.span, s.kind == "class"))
        return candidates[0]

    def owning_function(self, path: str, line: int) -> Symbol | None:
        """Innermost *callable* containing `line` (skips bare class bodies)."""
        candidates = [
            s
            for s in self.by_path.get(path, [])
            if s.contains(line) and s.kind in ("function", "method")
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda s: s.span)
        return candidates[0]

    # -- class hierarchy -------------------------------------------------

    def link_hierarchy(self, imports_by_path: dict[str, set[str]]) -> None:
        """Resolve raw base-class names into edges, using the same evidence rule
        as the call resolver: same module, or the file imports the base's module."""
        self.bases_of.clear()
        self.subclasses_of.clear()
        for sym in self.symbols:
            if sym.kind != "class" or not sym.bases:
                continue
            resolved: list[str] = []
            for raw in sym.bases:
                base = self._resolve_base(raw, sym, imports_by_path.get(sym.path, set()))
                if base:
                    resolved.append(base)
                    self.subclasses_of.setdefault(base, []).append(sym.fqname)
            if resolved:
                self.bases_of[sym.fqname] = resolved

    def _resolve_base(self, raw: str, sym: Symbol, imports: set[str]) -> str | None:
        short = raw.rsplit(".", 1)[-1]
        if short in ("object", "Exception", "ABC", "Protocol", "Enum", "Generic",
                     "NamedTuple", "TypedDict", "BaseModel"):
            return None  # external/stdlib bases: no edges to draw
        # same module first
        local = f"{sym.module}.{short}"
        if local in self.by_fq and self.by_fq[local].kind == "class":
            return local
        candidates = [
            c
            for c in self.by_short.get(short, [])
            if c.kind == "class"
            and (
                c.path == sym.path
                or any(t == c.module or t.startswith(f"{c.module}.") for t in imports)
            )
        ]
        return candidates[0].fqname if len(candidates) == 1 else None

    def resolve_method(self, class_fq: str, method: str, _depth: int = 0) -> Symbol | None:
        """Walk the hierarchy upward: where would `class_fq().method()` land?"""
        if _depth > 8:  # cycles/deep towers
            return None
        own = self.by_fq.get(f"{class_fq}.{method}")
        if own is not None:
            return own
        for base in self.bases_of.get(class_fq, []):
            found = self.resolve_method(base, method, _depth + 1)
            if found is not None:
                return found
        return None

    def overrides_of(self, method_sym: Symbol) -> list[Symbol]:
        """Subclass methods that override `method_sym` (transitively)."""
        if method_sym.kind != "method" or method_sym.parent is None:
            return []
        class_fq = f"{method_sym.module}.{method_sym.parent}"
        name = method_sym.short_name
        out: list[Symbol] = []
        stack = list(self.subclasses_of.get(class_fq, []))
        seen: set[str] = set()
        while stack:
            sub = stack.pop()
            if sub in seen:
                continue
            seen.add(sub)
            if (override := self.by_fq.get(f"{sub}.{name}")) is not None:
                out.append(override)
            stack.extend(self.subclasses_of.get(sub, []))
        return out

    def __len__(self) -> int:
        return len(self.symbols)
