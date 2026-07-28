"""bugwrap CLI."""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

from . import __version__
from .analysis import build_units
from .config import DEFAULT_TOML, Config, load_config
from .gitio import (
    GitError,
    default_base,
    diff_base,
    diff_pr,
    diff_range,
    diff_working,
    parse_unified_diff,
    repo_root,
    synthesize_full_review,
    untracked_python_files,
)
from .index import build_index
from .llm import OllamaClient, OllamaUnavailable
from .models import severity_rank
from .report import Painter, post_to_github, print_findings, print_units, units_json, use_color, write_json
from .review import review_units
from .review.runner import ReviewResult, merge_findings, postprocess

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_ERROR = 2


# --------------------------------------------------------------------------
# argument wiring
# --------------------------------------------------------------------------


def add_selector_args(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("what to review")
    group.add_argument("--base", metavar="REF", help="diff against the merge-base with REF")
    group.add_argument("--range", dest="rev_range", metavar="A..B", help="review a commit range")
    group.add_argument("--pr", type=int, metavar="N", help="review GitHub PR #N (needs gh)")
    group.add_argument("paths", nargs="*", help="whole-file review of these paths")
    group.add_argument("--staged", action="store_true", help="only staged changes")
    group.add_argument(
        "--untracked", action="store_true", help="also review new, untracked .py files"
    )


def add_model_args(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("model")
    group.add_argument("--model", "-m", help="ollama model tag")
    group.add_argument("--host", help="ollama host (default http://localhost:11434)")
    group.add_argument("--num-ctx", type=int, help="context window to request")
    group.add_argument("--temperature", type=float)
    group.add_argument("--think", action="store_true", help="enable reasoning mode")
    group.add_argument("--workers", type=int, help="concurrent requests")
    group.add_argument("--timeout", type=int, help="per-request timeout in seconds")


def add_context_args(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("smart context")
    group.add_argument("--max-callers", type=int, help="impact sites per packet")
    group.add_argument("--snippet-radius", type=int, help="lines around each call site")
    group.add_argument("--no-tests", action="store_true", help="exclude test files from impact")
    group.add_argument("--no-cache", action="store_true", help="ignore the index cache")


def apply_overrides(cfg: Config, args) -> Config:
    updates = {}
    for attr, key in (
        ("model", "model"),
        ("host", "host"),
        ("num_ctx", "num_ctx"),
        ("temperature", "temperature"),
        ("workers", "workers"),
        ("timeout", "timeout"),
        ("max_callers", "max_callers"),
        ("snippet_radius", "snippet_radius"),
    ):
        value = getattr(args, attr, None)
        if value is not None:
            updates[key] = value
    if getattr(args, "think", False):
        updates["think"] = True
    if getattr(args, "no_tests", False):
        updates["include_tests"] = False
    if getattr(args, "no_cache", False):
        updates["use_cache"] = False
    if getattr(args, "fail_on", None):
        updates["fail_on"] = args.fail_on
    if getattr(args, "min_confidence", None) is not None:
        updates["min_confidence"] = args.min_confidence
    return replace(cfg, **updates) if updates else cfg


# --------------------------------------------------------------------------
# pipeline
# --------------------------------------------------------------------------


def collect_units(args, cfg: Config, root: Path, paint: Painter):
    """Resolve the selector into diffs, index the repo, and build packets."""
    if args.paths:
        rels = []
        skipped = []
        for raw in args.paths:
            path = Path(raw).resolve()
            candidates = sorted(path.rglob("*.py")) if path.is_dir() else [path]
            for candidate in candidates:
                if candidate.suffix != ".py":
                    continue
                try:
                    rels.append(str(candidate.relative_to(root)))
                except ValueError:
                    skipped.append(str(candidate))
        if skipped:
            print(
                paint(f"skipping {len(skipped)} path(s) outside {root}", "yellow"),
                file=sys.stderr,
            )
        diffs = synthesize_full_review(sorted(set(rels)), root)
        base = "HEAD"
        label = f"{len(diffs)} file(s), whole-file review"
    elif args.pr:
        diff_text, base = diff_pr(args.pr, root)
        diffs = parse_unified_diff(diff_text)
        label = f"PR #{args.pr}"
    elif args.rev_range:
        diff_text, base = diff_range(args.rev_range, root)
        diffs = parse_unified_diff(diff_text)
        label = args.rev_range
    elif args.base:
        ref = args.base if args.base != "auto" else default_base(root)
        diff_text, base = diff_base(ref, root)
        diffs = parse_unified_diff(diff_text)
        label = f"HEAD vs {ref}"
    else:
        diff_text, base = diff_working(root, staged_only=args.staged)
        diffs = parse_unified_diff(diff_text)
        label = "staged changes" if args.staged else "working tree"

    if getattr(args, "untracked", False):
        extra = [p for p in untracked_python_files(root) if p not in {d.path for d in diffs}]
        diffs.extend(synthesize_full_review(extra, root))

    python_diffs = [d for d in diffs if d.is_python and not d.binary]
    if not python_diffs:
        return [], None, label, base

    print(paint(f"indexing {root.name}…", "dim"), file=sys.stderr)
    index = build_index(root, cfg)
    print(
        paint(
            f"  {index.stats['files']} files · {index.stats['symbols']} symbols · "
            f"{index.stats['resolved']} resolved call edges",
            "dim",
        ),
        file=sys.stderr,
    )
    units = build_units(python_diffs, index, cfg, root, base)
    return units, index, label, base


def static_findings(units, index):
    from .api import static_check

    return static_check(units, index)


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------


def cmd_review(args) -> int:
    paint = Painter(use_color(sys.stderr))
    root = repo_root(Path.cwd()) if not args.paths else _root_for_paths(args)
    cfg = apply_overrides(load_config(root), args)

    units, index, label, base = collect_units(args, cfg, root, paint)
    if not units:
        print(paint(f"no Python changes to review ({label})", "dim"))
        return EXIT_OK

    print(
        paint(
            f"reviewing {len(units)} packet(s) from {label} with {cfg.model}",
            "bold",
        ),
        file=sys.stderr,
    )

    client = OllamaClient(
        host=cfg.host,
        model=cfg.model,
        num_ctx=cfg.num_ctx,
        temperature=cfg.temperature,
        think=cfg.think,
        timeout=cfg.timeout,
    )
    ok, message = client.health()
    if not ok:
        print(paint(f"ollama: {message}", "red"), file=sys.stderr)
        return EXIT_ERROR
    if "not found" in message:
        print(paint(f"ollama: {message}", "yellow"), file=sys.stderr)

    trained = client.model_context_length()
    if trained and cfg.num_ctx > trained:
        print(
            paint(
                f"warning: num_ctx={cfg.num_ctx} exceeds {cfg.model}'s trained "
                f"context of {trained}; lowering",
                "yellow",
            ),
            file=sys.stderr,
        )
        client.num_ctx = trained

    done = [0]

    def progress(unit, error):
        done[0] += 1
        status = paint("!", "red") if error else paint("·", "green")
        print(f"  {status} [{done[0]}/{len(units)}] {unit.title}", file=sys.stderr)

    # Deterministic layer first — its findings go into the prompt so the model
    # doesn't re-report them, and they own their lines at merge time.
    static = static_findings(units, index)
    result = review_units(units, cfg, client=client, on_progress=progress, known=static)
    result.findings = postprocess(merge_findings(static, result.findings), cfg)

    print_findings(result, Painter(use_color(sys.stdout)))
    if args.json:
        write_json(result, args.json, meta={"target": label, "base": base, "model": cfg.model})
    if args.post:
        if not args.pr:
            print(paint("--post requires --pr", "red"), file=sys.stderr)
        else:
            ok, message = post_to_github(result, args.pr, root)
            print(paint(f"github: {message or 'posted'}", "dim" if ok else "red"), file=sys.stderr)

    if cfg.fail_on != "none":
        threshold = severity_rank(cfg.fail_on)
        if any(severity_rank(f.severity) >= threshold for f in result.findings):
            return EXIT_FINDINGS
    return EXIT_OK


def cmd_check(args) -> int:
    """Static contract check only — no model, no tokens, CI-safe."""
    paint = Painter(use_color(sys.stderr))
    root = repo_root(Path.cwd()) if not args.paths else _root_for_paths(args)
    cfg = apply_overrides(load_config(root), args)
    units, index, label, base = collect_units(args, cfg, root, paint)

    result = ReviewResult(units=len(units))
    if units:
        result.findings = postprocess(static_findings(units, index), cfg)

    print_findings(result, Painter(use_color(sys.stdout)))
    if args.json:
        write_json(result, args.json, meta={"target": label, "base": base, "mode": "static"})

    if cfg.fail_on != "none":
        threshold = severity_rank(cfg.fail_on)
        if any(severity_rank(f.severity) >= threshold for f in result.findings):
            return EXIT_FINDINGS
    return EXIT_OK


def cmd_context(args) -> int:
    paint = Painter(use_color(sys.stderr))
    root = repo_root(Path.cwd()) if not args.paths else _root_for_paths(args)
    cfg = apply_overrides(load_config(root), args)
    units, index, label, base = collect_units(args, cfg, root, paint)

    if args.json:
        print(units_json(units))
        return EXIT_OK
    if not units:
        print(paint(f"no Python changes ({label})", "dim"))
        return EXIT_OK

    print_units(units, Painter(use_color(sys.stdout)), verbose=args.prompt)
    total = sum(u.tokens for u in units)
    print(
        Painter(use_color(sys.stdout))(
            f"\n{len(units)} packet(s), ~{total} tokens total "
            f"(budget {cfg.unit_budget}/packet at num_ctx={cfg.num_ctx})",
            "dim",
        )
    )
    return EXIT_OK


def cmd_index(args) -> int:
    paint = Painter(use_color(sys.stdout))
    root = repo_root(Path.cwd())
    cfg = apply_overrides(load_config(root), args)
    index = build_index(root, cfg)
    stats = index.stats
    print(paint(f"{root}", "bold"))
    for key in ("files", "symbols", "edges", "resolved", "targets", "unparsable"):
        print(f"  {key:<12} {stats.get(key, 0)}")
    if index.skipped and args.verbose:
        print(paint("\nunparsable:", "yellow"))
        for path in index.skipped[:20]:
            print(f"  {path}")
    if args.top:
        from .index.rank import pagerank

        rank = pagerank(index.graph)
        ranked = sorted(
            ((score, fq) for fq, score in rank.items() if fq in index.symbols.by_fq),
            reverse=True,
        )
        print(paint(f"\nmost central symbols (PageRank over {len(rank)} nodes):", "bold"))
        for score, fq in ranked[: args.top]:
            sym = index.symbols.by_fq[fq]
            callers = len(index.graph.callers_of(fq))
            print(f"  {score:.4f}  {fq}  ({callers} callers)  {sym.path}:{sym.lineno}")
    if args.symbol:
        target = args.symbol
        matches = [s for s in index.symbols.symbols if target in s.fqname]
        for sym in matches[:20]:
            callers = index.graph.callers_of(sym.fqname)
            print(paint(f"\n{sym.fqname}", "bold"))
            print(f"  {sym.path}:{sym.lineno}  {sym.signature}")
            print(f"  {len(callers)} caller(s)")
            for site in callers[:15]:
                print(f"    ← {site.path}:{site.line}  [{site.resolution}]  {site.text[:70]}")
    return EXIT_OK


def cmd_doctor(args) -> int:
    paint = Painter(use_color(sys.stdout))
    cfg = apply_overrides(load_config(Path.cwd()), args)
    print(paint("bugwrap doctor", "bold"))
    print(f"  version      {__version__}")
    print(f"  python       {sys.version.split()[0]}")
    print(f"  config       {cfg.source or '(defaults)'}")

    try:
        root = repo_root(Path.cwd())
        print(f"  git repo     {root}")
        print(f"  default base {default_base(root)}")
    except GitError as exc:
        print(paint(f"  git repo     {exc}", "red"))

    print(f"  ollama host  {cfg.host}")
    client = OllamaClient(host=cfg.host, model=cfg.model, num_ctx=cfg.num_ctx)
    try:
        models = client.list_models()
    except OllamaUnavailable as exc:
        print(paint(f"  ollama       {exc}", "red"))
        print(
            paint(
                "\n  install: https://ollama.com/download\n"
                f"  then:    ollama pull {cfg.model} && ollama serve",
                "dim",
            )
        )
        return EXIT_ERROR

    print(f"  models       {len(models)} available")
    ok, message = client.health()
    print(("  " + ("✓ " if ok else "✗ ") + message) if models else "  no models pulled")
    trained = client.model_context_length()
    if trained:
        marker = "✓" if cfg.num_ctx <= trained else "✗ too large"
        print(f"  context      num_ctx={cfg.num_ctx}, model supports {trained}  {marker}")
    return EXIT_OK if ok else EXIT_ERROR


def cmd_models(args) -> int:
    cfg = load_config(Path.cwd())
    client = OllamaClient(host=cfg.host)
    try:
        for name in client.list_models():
            marker = "*" if name == cfg.model else " "
            print(f"{marker} {name}")
    except OllamaUnavailable as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_ERROR
    return EXIT_OK


def cmd_init(args) -> int:
    target = Path.cwd() / ".bugwrap.toml"
    if target.exists() and not args.force:
        print(f"{target} already exists (use --force to overwrite)", file=sys.stderr)
        return EXIT_ERROR
    target.write_text(DEFAULT_TOML)
    print(f"wrote {target}")
    return EXIT_OK


def _root_for_paths(args) -> Path:
    try:
        return repo_root(Path.cwd())
    except GitError:
        return Path.cwd()


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bugwrap",
        description="Smart Context code review for Python, on local models via Ollama.",
    )
    parser.add_argument("--version", action="version", version=f"bugwrap {__version__}")
    sub = parser.add_subparsers(dest="command")

    review = sub.add_parser("review", help="review changes and report findings")
    add_selector_args(review)
    add_model_args(review)
    add_context_args(review)
    review.add_argument("--json", metavar="PATH", help="write JSON results ('-' for stdout)")
    review.add_argument("--post", action="store_true", help="post results to the PR (with --pr)")
    review.add_argument(
        "--fail-on",
        choices=["none", "info", "low", "medium", "high", "critical"],
        help="exit 1 at or above this severity (default: high)",
    )
    review.add_argument("--min-confidence", type=float, help="drop findings below this")
    review.set_defaults(func=cmd_review)

    check = sub.add_parser(
        "check", help="deterministic contract check — no model needed, instant, CI-safe"
    )
    add_selector_args(check)
    add_context_args(check)
    check.add_argument("--json", metavar="PATH", help="write JSON results ('-' for stdout)")
    check.add_argument(
        "--fail-on",
        choices=["none", "info", "low", "medium", "high", "critical"],
        help="exit 1 at or above this severity (default: high)",
    )
    check.add_argument("--min-confidence", type=float, help=argparse.SUPPRESS)
    check.set_defaults(func=cmd_check)

    context = sub.add_parser(
        "context", help="show the Smart Context packets without calling a model"
    )
    add_selector_args(context)
    add_context_args(context)
    context.add_argument("--num-ctx", type=int, help="budget to pack against")
    context.add_argument("--json", action="store_true", help="emit packets as JSON")
    context.add_argument("--prompt", action="store_true", help="print the full rendered prompt")
    context.set_defaults(func=cmd_context)

    index = sub.add_parser("index", help="build the symbol/call index and show stats")
    add_context_args(index)
    index.add_argument("--symbol", help="show call sites for symbols matching this")
    index.add_argument("--top", type=int, metavar="N", help="show the N most central symbols")
    index.add_argument("--verbose", "-v", action="store_true")
    index.set_defaults(func=cmd_index)

    doctor = sub.add_parser("doctor", help="check git, ollama, model and context settings")
    add_model_args(doctor)
    doctor.set_defaults(func=cmd_doctor)

    models = sub.add_parser("models", help="list models available on the ollama host")
    models.set_defaults(func=cmd_models)

    init = sub.add_parser("init", help="write a .bugwrap.toml")
    init.add_argument("--force", action="store_true")
    init.set_defaults(func=cmd_init)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return EXIT_OK
    try:
        return args.func(args)
    except GitError as exc:
        print(f"bugwrap: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except OllamaUnavailable as exc:
        print(f"bugwrap: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
