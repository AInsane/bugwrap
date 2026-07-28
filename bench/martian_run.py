"""Path A: run bugwrap over the Martian benchmark's Sentry PRs and score
against their human-verified golden comments.

Dataset: withmartian/code-review-benchmark, offline/golden_comments/sentry.json
(10 PRs; 6 on getsentry/sentry, 4 on the ai-code-review-evaluation fork).

Judging follows their methodology — "do these describe the same underlying
issue?" — with one honest deviation, flagged in the output: the official
leaderboard judges with frontier models; by default this script judges with a
local Ollama model. Treat local-judge numbers as provisional until re-judged
with the official setup (--judge-model with an API is the roadmap step).

Usage:
    # 1. one-time: partial-clone the repo pair
    git clone --filter=blob:none --no-checkout \
        https://github.com/ai-code-review-evaluation/sentry-greptile.git sentry
    cd sentry && git remote add upstream https://github.com/getsentry/sentry.git

    # 2. build the PR manifest (needs gh)
    python -m bench.martian_run manifest --golden path/to/sentry.json --out prs.json

    # 3. review + judge
    python -m bench.martian_run review --repo path/to/sentry --manifest prs.json \
        --out results/ [--llm] [--model qwen2.5-coder:7b]
    python -m bench.martian_run judge --manifest prs.json --results results/ \
        --judge-model qwen2.5-coder:7b
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
from bugwrap.config import Config  # noqa: E402
from bugwrap.gitio import diff_range, parse_unified_diff  # noqa: E402
from bugwrap.index import build_index  # noqa: E402
from bugwrap.llm import OllamaClient  # noqa: E402


def sh(args, cwd=None, check=True) -> str:
    proc = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise RuntimeError(f"{' '.join(args)}: {proc.stderr.strip()[:300]}")
    return proc.stdout


# --------------------------------------------------------------------------
# manifest
# --------------------------------------------------------------------------


def cmd_manifest(args) -> int:
    goldens = json.loads(Path(args.golden).read_text())
    out = []
    for pr in goldens:
        parts = pr["url"].split("/")
        owner_repo, num = "/".join(parts[3:5]), parts[6]
        meta = json.loads(
            sh(["gh", "api", f"repos/{owner_repo}/pulls/{num}",
                "--jq", '{"base": .base.sha, "head": .head.sha, "title": .title}'])
        )
        out.append({"url": pr["url"], "repo": owner_repo, "num": int(num),
                    **meta, "goldens": pr["comments"]})
    Path(args.out).write_text(json.dumps(out, indent=1))
    print(f"{len(out)} PRs -> {args.out}")
    return 0


# --------------------------------------------------------------------------
# review
# --------------------------------------------------------------------------


def ensure_sha(repo: Path, sha: str) -> None:
    if subprocess.run(["git", "cat-file", "-e", f"{sha}^{{commit}}"],
                      cwd=repo, capture_output=True).returncode == 0:
        return
    for remote in ("origin", "upstream"):
        if subprocess.run(["git", "fetch", "-q", remote, sha],
                          cwd=repo, capture_output=True).returncode == 0:
            return
    raise RuntimeError(f"cannot fetch {sha}")


def cmd_review(args) -> int:
    repo = Path(args.repo).resolve()
    manifest = json.loads(Path(args.manifest).read_text())
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    cfg = Config(model=args.model, num_ctx=args.num_ctx, workers=1)

    for pr in manifest:
        tag = f"pr{pr['num']}"
        out_file = out_dir / f"{tag}.json"
        if out_file.exists() and not args.force:
            print(f"[skip] {tag} (exists)")
            continue
        print(f"[{tag}] {pr['title'][:60]}")
        ensure_sha(repo, pr["base"])
        ensure_sha(repo, pr["head"])
        sh(["git", "checkout", "-q", "-f", pr["head"]], cwd=repo)

        # Three-dot: diff against the merge base, exactly like the GitHub PR view.
        # Two-dot on diverged branches drags the whole repo into the diff.
        diff_text, base = diff_range(f"{pr['base']}...{pr['head']}", repo)
        diffs = [d for d in parse_unified_diff(diff_text) if d.is_python]
        print(f"    {len(diffs)} python file(s) changed")
        index = build_index(repo, cfg)
        units = build_units(diffs, index, cfg, repo, base)
        print(f"    {len(units)} packet(s), index: {index.stats['symbols']} symbols")

        findings = static_check(units, index)
        mode = "static"
        if args.llm and units:
            from bugwrap.review.runner import merge_findings, review_units

            result = review_units(units, cfg, known=findings)
            findings = merge_findings(findings, result.findings)
            mode = f"static+llm:{cfg.model}"

        comments = [
            {"path": f.file, "line": f.line, "severity": f.severity,
             "title": f.title, "body": f"{f.title}. {f.detail}"}
            for f in findings
        ]
        out_file.write_text(json.dumps(
            {"pr": pr["num"], "mode": mode, "packets": len(units),
             "comments": comments}, indent=1))
        print(f"    -> {len(comments)} comment(s)")
    return 0


# --------------------------------------------------------------------------
# judge
# --------------------------------------------------------------------------

JUDGE_SYSTEM = """You judge code-review comments. Given one GOLDEN issue (ground
truth, verified by humans) and a numbered list of CANDIDATE comments from a review
tool, decide whether any candidate describes the same underlying issue as the
golden. Different wording is fine — only the substance matters: same root cause,
same code path. A candidate that mentions the same file but a different problem
is NOT a match."""

JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "match_index": {"type": "integer"},
        "reason": {"type": "string"},
    },
    "required": ["match_index", "reason"],
}


def cmd_judge(args) -> int:
    manifest = json.loads(Path(args.manifest).read_text())
    results_dir = Path(args.results)
    client = OllamaClient(host=args.host, model=args.judge_model, num_ctx=8192)

    total = {"golden": 0, "matched": 0, "comments": 0, "matched_comments": 0}
    rows = []
    for pr in manifest:
        result_file = results_dir / f"pr{pr['num']}.json"
        if not result_file.exists():
            print(f"[miss] pr{pr['num']}: no results file")
            continue
        data = json.loads(result_file.read_text())
        comments = data["comments"]
        matched_golden = 0
        matched_comment_idx: set[int] = set()

        for golden in pr["goldens"]:
            if not comments:
                break
            listing = "\n".join(
                f"[{i}] ({c['path']}:{c['line']}) {c['body'][:400]}"
                for i, c in enumerate(comments)
            )
            prompt = (
                f"GOLDEN issue (severity {golden.get('severity', '?')}):\n"
                f"{golden['comment']}\n\nCANDIDATES:\n{listing}\n\n"
                'Return {"match_index": <index or -1>, "reason": "..."}.'
            )
            try:
                chat = client.chat(JUDGE_SYSTEM, prompt, schema=JUDGE_SCHEMA)
                verdict = json.loads(chat.content)
            except Exception as exc:  # noqa: BLE001
                print(f"    judge error: {exc}")
                continue
            idx = verdict.get("match_index", -1)
            if isinstance(idx, int) and 0 <= idx < len(comments):
                matched_golden += 1
                matched_comment_idx.add(idx)

        n_golden = len(pr["goldens"])
        total["golden"] += n_golden
        total["matched"] += matched_golden
        total["comments"] += len(comments)
        total["matched_comments"] += len(matched_comment_idx)
        rows.append((pr["num"], matched_golden, n_golden, len(matched_comment_idx), len(comments)))
        print(f"pr{pr['num']:<6} recall {matched_golden}/{n_golden} · "
              f"precision {len(matched_comment_idx)}/{len(comments)}")

    recall = total["matched"] / total["golden"] if total["golden"] else 0
    precision = (
        total["matched_comments"] / total["comments"] if total["comments"] else 1.0
    )
    print(
        f"\nSentry subset ({len(rows)} PRs, judge={args.judge_model} [LOCAL — "
        f"provisional, official leaderboard judges with frontier models]):\n"
        f"  recall    {recall:.0%}  ({total['matched']}/{total['golden']} goldens found)\n"
        f"  precision {precision:.0%}  ({total['matched_comments']}/{total['comments']} comments matched a golden)"
    )
    return 0


# --------------------------------------------------------------------------
# compare: same judge, same goldens — us and the published competitor reviews
# --------------------------------------------------------------------------

_STRIP = [
    (r"\[!\[[^\]]*\]\([^)]*\)\]\([^)]*\)", ""),  # badge links
    (r"!\[[^\]]*\]\([^)]*\)", ""),  # images
    (r"<details[^>]*>.*?</details>", "", ),  # collapsed reasoning blocks
    (r"<[^>]+>", ""),  # html tags
    (r"\[([^\]]*)\]\([^)]*\)", r"\1"),  # links -> text
]


def clean_body(text: str, limit: int = 450) -> str:
    import re

    for pattern, repl in _STRIP:
        text = re.sub(pattern, repl, text, flags=re.DOTALL)
    return " ".join(text.split())[:limit]


def judge_comments(client, goldens, comments) -> tuple[int, int]:
    """(#goldens matched, #distinct comments that matched)."""
    matched_golden = 0
    matched_idx: set[int] = set()
    for golden in goldens:
        if not comments:
            break
        listing = "\n".join(f"[{i}] {c}" for i, c in enumerate(comments))
        prompt = (
            f"GOLDEN issue (severity {golden.get('severity', '?')}):\n"
            f"{golden['comment']}\n\nCANDIDATES:\n{listing}\n\n"
            'Return {"match_index": <index or -1>, "reason": "..."}.'
        )
        try:
            chat = client.chat(JUDGE_SYSTEM, prompt, schema=JUDGE_SCHEMA)
            verdict = json.loads(chat.content)
        except Exception:  # noqa: BLE001
            continue
        idx = verdict.get("match_index", -1)
        if isinstance(idx, int) and 0 <= idx < len(comments):
            matched_golden += 1
            matched_idx.add(idx)
    return matched_golden, len(matched_idx)


def cmd_compare(args) -> int:
    data = json.loads(Path(args.benchmark_data).read_text())
    sentry = {u: e for u, e in data.items()
              if e.get("source_repo") == args.source_repo and e.get("reviews")}
    manifest = json.loads(Path(args.manifest).read_text())
    url_to_num = {pr["url"]: pr["num"] for pr in manifest}
    results_dir = Path(args.results) if args.results else None
    client = OllamaClient(host=args.host, model=args.judge_model, num_ctx=8192)
    tools = args.tools.split(",")

    scores: dict[str, dict] = {
        t: {"golden": 0, "matched": 0, "comments": 0, "matched_c": 0} for t in tools
    }
    for url, entry in sentry.items():
        goldens = entry["golden_comments"]
        reviews_by_tool = {r["tool"]: r for r in entry["reviews"]}
        for tool in tools:
            if tool == "bugwrap":
                num = url_to_num.get(url)
                f = results_dir / f"pr{num}.json" if (results_dir and num) else None
                if not (f and f.exists()):
                    continue
                raw = json.loads(f.read_text())["comments"]
                comments = [
                    clean_body(f"({c['path']}:{c['line']}) {c['body']}") for c in raw
                ]
            else:
                review = reviews_by_tool.get(tool)
                if review is None:
                    continue
                comments = [
                    clean_body(f"({c.get('path')}:{c.get('line')}) {c.get('body', '')}")
                    for c in review.get("review_comments", [])[:25]
                ]
            got, matched_c = judge_comments(client, goldens, comments)
            s = scores[tool]
            s["golden"] += len(goldens)
            s["matched"] += got
            s["comments"] += len(comments)
            s["matched_c"] += matched_c
            print(f"  {tool:<14} {url.rsplit('/', 1)[-1]:>6}: "
                  f"recall {got}/{len(goldens)} precision {matched_c}/{len(comments)}",
                  flush=True)

    print(f"\n== {args.source_repo} subset · same local judge ({args.judge_model}) — "
          "provisional vs official frontier-model judging ==")
    print(f"{'tool':<16} {'recall':>8} {'precision':>10} {'comments':>9}")
    rows = []
    for tool, s in scores.items():
        if s["golden"] == 0:
            continue
        recall = s["matched"] / s["golden"]
        precision = s["matched_c"] / s["comments"] if s["comments"] else 1.0
        rows.append((recall, precision, tool, s))
    for recall, precision, tool, s in sorted(rows, reverse=True):
        print(f"{tool:<16} {recall:>7.0%} {precision:>9.0%} {s['comments']:>9}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    m = sub.add_parser("manifest")
    m.add_argument("--golden", required=True)
    m.add_argument("--out", required=True)
    m.set_defaults(func=cmd_manifest)

    r = sub.add_parser("review")
    r.add_argument("--repo", required=True)
    r.add_argument("--manifest", required=True)
    r.add_argument("--out", required=True)
    r.add_argument("--llm", action="store_true")
    r.add_argument("--model", default="qwen2.5-coder:7b")
    r.add_argument("--num-ctx", type=int, default=16384)
    r.add_argument("--force", action="store_true")
    r.set_defaults(func=cmd_review)

    c = sub.add_parser("compare")
    c.add_argument("--benchmark-data", required=True)
    c.add_argument("--manifest", required=True)
    c.add_argument("--results", help="bugwrap results dir (adds 'bugwrap' row)")
    c.add_argument("--tools", default="bugwrap,coderabbit,greptile,copilot,bugbot,devin,claude-code,qodo,gemini,augment")
    c.add_argument("--source-repo", default="sentry")
    c.add_argument("--judge-model", default="qwen2.5-coder:7b")
    c.add_argument("--host", default="http://localhost:11434")
    c.set_defaults(func=cmd_compare)

    j = sub.add_parser("judge")
    j.add_argument("--manifest", required=True)
    j.add_argument("--results", required=True)
    j.add_argument("--judge-model", default="qwen2.5-coder:7b")
    j.add_argument("--host", default="http://localhost:11434")
    j.set_defaults(func=cmd_judge)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
