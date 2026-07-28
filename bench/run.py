"""contract-bench: measure bugwrap's precision/recall on seeded contract bugs.

Scoring follows the Martian code-review-benchmark model: golden findings per PR,
a matcher that asks "same underlying issue?", then

    precision = matched tool findings / all tool findings
    recall    = matched golden issues / all golden issues

Usage:
    python -m bench.run             # static layer only (no model, instant)
    python -m bench.run --llm       # static + Ollama review
    python -m bench.run --only name # single scenario
"""

from __future__ import annotations

import argparse
import subprocess
from dataclasses import replace
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from bugwrap.analysis import build_units  # noqa: E402
from bugwrap.api import static_check  # noqa: E402
from bugwrap.config import Config  # noqa: E402
from bugwrap.gitio import diff_working, parse_unified_diff  # noqa: E402
from bugwrap.index import build_index  # noqa: E402
from bugwrap.models import Finding  # noqa: E402

from .scenarios import SCENARIOS  # noqa: E402

LINE_TOLERANCE = 2


def git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def materialize(scn: dict, root: Path) -> None:
    for rel, text in scn["files"].items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    git(["init", "-q", "-b", "main"], root)
    git(["config", "user.email", "bench@bugwrap"], root)
    git(["config", "user.name", "bench"], root)
    git(["add", "-A"], root)
    git(["commit", "-qm", "base"], root)
    for rel, text in scn["change"].items():
        path = root / rel
        if text is None:
            path.unlink()
        else:
            path.write_text(text)


def run_bugwrap(root: Path, use_llm: bool, cfg: Config, static_on: bool = True):
    diff_text, base = diff_working(root)
    diffs = [d for d in parse_unified_diff(diff_text) if d.is_python]
    index = build_index(root, cfg)
    units = build_units(diffs, index, cfg, root, base)

    findings = static_check(units, index) if static_on else []
    if use_llm and units:
        from bugwrap.review import review_units
        from bugwrap.review.runner import merge_findings

        result = review_units(units, cfg, known=findings)
        findings = merge_findings(findings, result.findings)
    return findings, units


def score(findings: list[Finding], golden: list[tuple]) -> dict:
    remaining = list(golden)
    matched_findings = 0
    for finding in findings:
        hit = None
        for g in remaining:
            g_file, g_line, keyword = g
            if finding.file != g_file:
                continue
            if finding.line is None or abs(finding.line - g_line) > LINE_TOLERANCE:
                continue
            text = f"{finding.title} {finding.detail}".lower()
            if keyword and keyword.lower() not in text:
                continue
            hit = g
            break
        if hit is not None:
            remaining.remove(hit)
            matched_findings += 1
    return {
        "tp": matched_findings,
        "fp": len(findings) - matched_findings,
        "fn": len(remaining),
        "missed": remaining,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--llm", action="store_true", help="also run the Ollama review pass")
    parser.add_argument("--model", help="ollama model tag for --llm")
    parser.add_argument("--num-ctx", type=int, default=8192)
    parser.add_argument("--static-off", action="store_true",
                        help="with --llm: model only, no static findings (isolates the LLM layer)")
    parser.add_argument("--only", help="run a single scenario")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    cfg = Config(use_cache=False, min_confidence=0.45, num_ctx=args.num_ctx, workers=1)
    if args.model:
        cfg = replace(cfg, model=args.model)
    names = [args.only] if args.only else list(SCENARIOS)

    total = {"tp": 0, "fp": 0, "fn": 0}
    rows = []
    for name in names:
        scn = SCENARIOS[name]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            materialize(scn, root)
            findings, units = run_bugwrap(root, args.llm, cfg, static_on=not args.static_off)
        result = score(findings, scn["golden"])
        # Some contracts (e.g. function->generator) have no provable call-site
        # finding; the deliverable is a breaking delta in the packet instead.
        if scn.get("expect_breaking_delta") and not any(u.delta.breaking for u in units):
            result["fn"] += 1
            result["missed"].append(("<breaking delta expected>", 0, ""))
        for key in ("tp", "fp", "fn"):
            total[key] += result[key]
        status = "ok " if result["fp"] == 0 and result["fn"] == 0 else "MISS" if result["fn"] else "FP  "
        rows.append((name, len(scn["golden"]), result))
        print(
            f"  [{status}] {name:<28} golden={len(scn['golden'])} "
            f"tp={result['tp']} fp={result['fp']} fn={result['fn']}"
        )
        if args.verbose:
            for f in findings:
                print(f"        - {f.file}:{f.line}  [{f.severity}] {f.title}")
            for g in result["missed"]:
                print(f"        ! missed golden: {g}")

    tp, fp, fn = total["tp"], total["fp"], total["fn"]
    precision = tp / (tp + fp) if tp + fp else 1.0
    recall = tp / (tp + fn) if tp + fn else 1.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    if args.llm:
        mode = f"{'llm-only' if args.static_off else 'static+llm'}:{cfg.model}"
    else:
        mode = "static-only"
    print(
        f"\ncontract-bench ({mode}): {len(names)} scenarios · "
        f"precision {precision:.0%} · recall {recall:.0%} · F1 {f1:.2f} "
        f"(tp={tp} fp={fp} fn={fn})"
    )
    return 0 if fn == 0 and fp == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
