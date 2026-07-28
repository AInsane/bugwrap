"""Repository indexing: walk the tree once, produce symbols + a call graph."""

from __future__ import annotations

import ast
import json
import os
from dataclasses import asdict
from pathlib import Path

from ..config import Config
from ..models import CallSite, Symbol
from .callgraph import CallGraph, collect_calls_from_tree
from .symbols import SymbolTable, extract_symbols_from_tree, module_name

__all__ = ["RepoIndex", "build_index", "SymbolTable", "CallGraph"]

CACHE_VERSION = 5


class RepoIndex:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.symbols = SymbolTable()
        self.graph = CallGraph(self.symbols)
        self.files: list[str] = []
        self.skipped: list[str] = []
        self.sources: dict[str, str] = {}

    def source_of(self, rel_path: str) -> str | None:
        if rel_path in self.sources:
            return self.sources[rel_path]
        try:
            text = (self.root / rel_path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        self.sources[rel_path] = text
        return text

    @property
    def stats(self) -> dict:
        return {
            "files": len(self.files),
            "symbols": len(self.symbols),
            "unparsable": len(self.skipped),
            **self.graph.stats,
        }


def iter_python_files(root: Path, cfg: Config):
    excluded = set(cfg.exclude)
    search_roots = [root / r for r in cfg.roots] if cfg.roots else [root]
    for base in search_roots:
        if not base.exists():
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in excluded and not d.startswith(".")]
            for name in filenames:
                if name.endswith(".py"):
                    yield Path(dirpath) / name


def build_index(root: Path, cfg: Config, quiet: bool = True) -> RepoIndex:
    index = RepoIndex(root)
    cache = _Cache(root, enabled=cfg.use_cache)

    for abs_path in iter_python_files(root, cfg):
        rel = str(abs_path.relative_to(root))
        try:
            stat = abs_path.stat()
            source = abs_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        entry = cache.get(rel, stat.st_mtime_ns, stat.st_size)
        if entry is None:
            module = module_name(root, abs_path)
            try:
                tree = ast.parse(source)
            except (SyntaxError, ValueError):
                # Python 2 leftovers, templates, deliberately-broken test fixtures.
                # Deliberately not cached, so the count stays honest on warm runs
                # and a fixed file is picked up immediately.
                index.skipped.append(rel)
                continue
            # One parse, two walks — the tree is the expensive part.
            symbols = extract_symbols_from_tree(tree, module, rel)
            collected = collect_calls_from_tree(tree, module, rel, source.splitlines())
            calls, imports, reads = collected.calls, collected.imports, collected.reads
            cache.put(rel, stat.st_mtime_ns, stat.st_size, symbols, calls, imports, reads)
        else:
            symbols, calls, imports, reads = entry

        index.files.append(rel)
        index.symbols.add(symbols)
        index.graph.add(calls)
        index.graph.add_imports(rel, imports)
        index.graph.add_reads(reads)

    index.symbols.link_hierarchy(index.graph.imports_by_path)
    index.graph.link()
    cache.save()
    return index


# --------------------------------------------------------------------------
# on-disk cache
# --------------------------------------------------------------------------


class _Cache:
    """mtime+size keyed cache of parsed symbols and call edges."""

    def __init__(self, root: Path, enabled: bool = True) -> None:
        self.enabled = enabled
        self.path = root / ".bugwrap" / "index.json"
        self.data: dict = {"version": CACHE_VERSION, "files": {}}
        self.next: dict = {"version": CACHE_VERSION, "files": {}}
        self.dirty = False
        if enabled and self.path.is_file():
            try:
                loaded = json.loads(self.path.read_text())
                if loaded.get("version") == CACHE_VERSION:
                    self.data = loaded
            except (OSError, json.JSONDecodeError):
                pass

    def get(self, rel: str, mtime: int, size: int):
        if not self.enabled:
            return None
        entry = self.data["files"].get(rel)
        if not entry or entry.get("mtime") != mtime or entry.get("size") != size:
            return None
        self.next["files"][rel] = entry
        symbols = [Symbol(**s) for s in entry["symbols"]]
        for sym in symbols:
            sym.decorators = tuple(sym.decorators)
            sym.bases = tuple(sym.bases)
        calls = [CallSite(**c) for c in entry["calls"]]
        reads = [CallSite(**r) for r in entry.get("reads", [])]
        return symbols, calls, set(entry.get("imports", [])), reads

    def put(self, rel: str, mtime: int, size: int, symbols, calls, imports=(), reads=()) -> None:
        if not self.enabled:
            return
        self.next["files"][rel] = {
            "mtime": mtime,
            "size": size,
            "symbols": [asdict(s) for s in symbols],
            "calls": [asdict(c) for c in calls],
            "imports": sorted(imports),
            "reads": [asdict(r) for r in reads],
        }
        self.dirty = True

    def save(self) -> None:
        if not self.enabled or not self.dirty:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            gitignore = self.path.parent / ".gitignore"
            if not gitignore.exists():
                gitignore.write_text("*\n")
            self.path.write_text(json.dumps(self.next))
        except OSError:
            pass
