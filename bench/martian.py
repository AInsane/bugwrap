"""Adapter for the Martian code-review-benchmark (withmartian/code-review-benchmark).

That benchmark scores CodeRabbit, Greptile, Copilot, Cursor Bugbot, Qodo et al.
on 50 real PRs with human-verified golden comments (the Python repo is Sentry).
Their pipeline expects each tool's review as PR comments; this adapter produces
the same shape from a local bugwrap run, so the head-to-head needs no GitHub bot
installation — just checkouts.

Usage (one PR):
    python -m bench.martian --repo /path/to/sentry --base <base_sha> --head <head_sha> \
        --out comments/sentry_12345.json [--llm]

Output: [{"path": ..., "line": ..., "body": ..., "severity": ...}, ...]
— feed these to the benchmark's judge step (step3), or to any LLM judge with the
prompt "do these describe the same underlying issue as the golden comment?".
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from bugwrap.analysis import build_units  # noqa: E402
from bugwrap.api import static_check  # noqa: E402
from bugwrap.config import load_config  # noqa: E402
from bugwrap.gitio import diff_range, parse_unified_diff  # noqa: E402
from bugwrap.index import build_index  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True, help="local checkout of the PR's repo")
    parser.add_argument("--base", required=True, help="PR base sha/ref")
    parser.add_argument("--head", required=True, help="PR head sha/ref")
    parser.add_argument("--out", required=True, help="output comments JSON")
    parser.add_argument("--llm", action="store_true", help="include the Ollama review pass")
    args = parser.parse_args()

    root = Path(args.repo).resolve()
    subprocess.run(["git", "checkout", "-q", args.head], cwd=root, check=True)

    cfg = load_config(root)
    diff_text, base = diff_range(f"{args.base}..{args.head}", root)
    diffs = [d for d in parse_unified_diff(diff_text) if d.is_python]
    index = build_index(root, cfg)
    units = build_units(diffs, index, cfg, root, base)

    findings = static_check(units, index)
    if args.llm and units:
        from bugwrap.review import review_units

        findings.extend(review_units(units, cfg).findings)

    comments = [
        {
            "path": f.file,
            "line": f.line,
            "severity": f.severity,
            "body": f"**{f.title}**\n\n{f.detail}"
            + (f"\n\nSuggestion: {f.suggestion}" if f.suggestion else ""),
        }
        for f in findings
    ]
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(comments, indent=2))
    print(f"{len(comments)} comment(s) -> {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
