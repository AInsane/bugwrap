"""context-bench: Smart Context vs the naive full-context baseline.

The claim under test: dependency-driven packets find the same bugs as
"shove every related file into the prompt", while burning far fewer tokens.

Baseline ("full"): what diff-plus-context tools do — one prompt per changed
file containing the diff, the changed file in full, and the full text of every
file that imports it. Same model, same findings schema, same scoring.

Ours ("smart"): the full product — static re-binder + layered LLM on packets.

Two readouts:
  quality:  precision / recall on the contract-bench scenario suite (real model)
  tokens:   actual prompt tokens per scenario (ollama's prompt_eval_count),
            plus an estimator-based scaling table on synthetic repos of
            increasing size, where the full-context approach balloons.

Usage:
    python -m bench.context_bench --model qwen2.5-coder:3b          # both modes
    python -m bench.context_bench --scale-only                      # no model needed
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from bugwrap.analysis import build_units, estimate_tokens  # noqa: E402
from bugwrap.api import static_check  # noqa: E402
from bugwrap.config import Config  # noqa: E402
from bugwrap.gitio import diff_working, parse_unified_diff  # noqa: E402
from bugwrap.index import build_index  # noqa: E402
from bugwrap.llm import OllamaClient, OllamaError  # noqa: E402
from bugwrap.review.prompts import FINDINGS_SCHEMA, SYSTEM, render_unit  # noqa: E402
from bugwrap.review.runner import parse_findings  # noqa: E402

from .run import materialize, score  # noqa: E402
from .scenarios import SCENARIOS  # noqa: E402


class _FileUnit:
    """Shim so parse_findings can default the file for full-context responses."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.symbol = None


# --------------------------------------------------------------------------
# full-context baseline
# --------------------------------------------------------------------------


def full_context_prompts(root: Path, cfg: Config) -> list[tuple[str, str]]:
    """(changed_path, prompt) pairs the naive approach would send."""
    diff_text, _ = diff_working(root)
    diffs = [d for d in parse_unified_diff(diff_text) if d.is_python]
    index = build_index(root, cfg)

    prompts = []
    for fd in diffs:
        changed_module = fd.path[:-3].replace("/", ".").removesuffix(".__init__")
        dependents = sorted(
            path
            for path, imports in index.graph.imports_by_path.items()
            if any(t == changed_module or t.startswith(f"{changed_module}.") for t in imports)
        )
        parts = [f"# DIFF for {fd.path}", "```diff", fd.text(), "```", ""]
        for path in [fd.path, *dependents]:
            text = index.source_of(path)
            if text is None:
                continue
            parts += [f"# FILE: {path}", "```python", text.rstrip(), "```", ""]
        parts.append(
            'Review the change. Return JSON: {"findings": [...]}. '
            "Empty list if the change is sound."
        )
        prompts.append((fd.path, "\n".join(parts)))
    return prompts


def run_full(root: Path, cfg: Config, client: OllamaClient):
    findings, tokens, duration = [], 0, 0
    for path, prompt in full_context_prompts(root, cfg):
        try:
            chat = client.chat(SYSTEM, prompt, schema=FINDINGS_SCHEMA)
        except OllamaError as exc:
            print(f"    ! {exc}", file=sys.stderr)
            continue
        tokens += chat.prompt_tokens
        duration += chat.duration_ms
        findings.extend(parse_findings(chat.content, _FileUnit(path)))
    return findings, tokens, duration


def run_smart(root: Path, cfg: Config, client: OllamaClient):
    from bugwrap.review.runner import merge_findings, review_units

    diff_text, base = diff_working(root)
    diffs = [d for d in parse_unified_diff(diff_text) if d.is_python]
    index = build_index(root, cfg)
    units = build_units(diffs, index, cfg, root, base)
    static = static_check(units, index)
    result = review_units(units, cfg, client=client, known=static)
    return (
        merge_findings(static, result.findings),
        result.prompt_tokens,
        result.duration_ms,
    )


# --------------------------------------------------------------------------
# scale table: synthetic repos, estimator-based (no model required)
# --------------------------------------------------------------------------

_MODULE_TEMPLATE = '''\
"""Module {i}: generated workload."""
from shop.pricing import calculate_discount


def process_{i}(orders):
    """Chews through orders and applies business rules."""
    out = []
    for order in orders:
        value = order.subtotal * {i} % 97
        if value > 50:
            value = value - (value // 7)
        out.append(value)
    return out


def summarize_{i}(orders):
    rows = [(o.id, o.subtotal) for o in orders]
    return {{"count": len(rows), "total": sum(r[1] for r in rows)}}
'''

_CALLER_TEMPLATE = '''\
"""Module {i}: calls into pricing."""
from shop.pricing import calculate_discount


def checkout_{i}(order):
    net = calculate_discount(order.subtotal, 0.1)
    return net + {i}
'''


def synthetic_repo(root: Path, n_modules: int, n_callers: int) -> None:
    (root / "shop").mkdir(parents=True)
    (root / "shop" / "__init__.py").write_text("")
    (root / "shop" / "pricing.py").write_text(
        "def calculate_discount(price, pct):\n    return price * (1 - pct)\n"
    )
    for i in range(n_callers):
        (root / "shop" / f"caller_{i}.py").write_text(_CALLER_TEMPLATE.format(i=i))
    for i in range(n_modules - n_callers):
        (root / "shop" / f"worker_{i}.py").write_text(_MODULE_TEMPLATE.format(i=i))


def scale_row(n_modules: int, n_callers: int) -> dict:
    import subprocess

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "repo"
        synthetic_repo(root, n_modules, n_callers)
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.email", "b@b"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.name", "b"], cwd=root, check=True)
        subprocess.run(["git", "add", "-A"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=root, check=True)
        # the breaking change
        (root / "shop" / "pricing.py").write_text(
            "def calculate_discount(price, pct, currency):\n    return price * (1 - pct)\n"
        )
        cfg = Config(use_cache=False)

        full_tokens = sum(
            estimate_tokens(p) for _, p in full_context_prompts(root, cfg)
        )

        diff_text, base = diff_working(root)
        diffs = [d for d in parse_unified_diff(diff_text) if d.is_python]
        index = build_index(root, cfg)
        units = build_units(diffs, index, cfg, root, base)
        smart_tokens = sum(estimate_tokens(render_unit(u)) for u in units)

    return {
        "modules": n_modules,
        "callers": n_callers,
        "full": full_tokens,
        "smart": smart_tokens,
        "ratio": full_tokens / smart_tokens if smart_tokens else 0,
    }


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="qwen2.5-coder:3b")
    parser.add_argument("--num-ctx", type=int, default=16384)
    parser.add_argument("--scale-only", action="store_true")
    parser.add_argument("--only", help="single scenario")
    args = parser.parse_args()

    print("== scale table (estimator, no model) ==")
    print(f"{'modules':>8} {'callers':>8} {'full tok':>10} {'smart tok':>10} {'ratio':>7}")
    for n_modules, n_callers in [(10, 4), (50, 10), (150, 20)]:
        row = scale_row(n_modules, n_callers)
        print(
            f"{row['modules']:>8} {row['callers']:>8} {row['full']:>10} "
            f"{row['smart']:>10} {row['ratio']:>6.1f}x"
        )

    if args.scale_only:
        return 0

    cfg = Config(
        use_cache=False, model=args.model, num_ctx=args.num_ctx, workers=1
    )
    client = OllamaClient(
        host=cfg.host, model=cfg.model, num_ctx=cfg.num_ctx,
        temperature=cfg.temperature, timeout=cfg.timeout,
    )
    ok, message = client.health()
    if not ok:
        print(f"ollama: {message}", file=sys.stderr)
        return 2

    print(f"\n== quality + real tokens ({args.model}) ==")
    names = [args.only] if args.only else list(SCENARIOS)
    totals = {
        "smart": {"tp": 0, "fp": 0, "fn": 0, "tok": 0, "ms": 0},
        "full": {"tp": 0, "fp": 0, "fn": 0, "tok": 0, "ms": 0},
    }
    for name in names:
        scn = SCENARIOS[name]
        row = {}
        for mode, runner in (("smart", run_smart), ("full", run_full)):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp) / "repo"
                root.mkdir()
                materialize(scn, root)
                findings, tokens, ms = runner(root, cfg, client)
            result = score(findings, scn["golden"])
            for key in ("tp", "fp", "fn"):
                totals[mode][key] += result[key]
            totals[mode]["tok"] += tokens
            totals[mode]["ms"] += ms
            row[mode] = (result, tokens)
        s, f = row["smart"], row["full"]
        print(
            f"  {name:<30} smart: tp={s[0]['tp']} fp={s[0]['fp']} fn={s[0]['fn']} "
            f"{s[1]:>5} tok · full: tp={f[0]['tp']} fp={f[0]['fp']} fn={f[0]['fn']} "
            f"{f[1]:>6} tok"
        )

    print()
    for mode in ("smart", "full"):
        t = totals[mode]
        precision = t["tp"] / (t["tp"] + t["fp"]) if t["tp"] + t["fp"] else 1.0
        recall = t["tp"] / (t["tp"] + t["fn"]) if t["tp"] + t["fn"] else 1.0
        print(
            f"{mode:>6}: precision {precision:.0%} · recall {recall:.0%} · "
            f"{t['tok']} prompt tok · {t['ms'] / 1000:.0f}s"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
