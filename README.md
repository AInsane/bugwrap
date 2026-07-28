<div align="center">

# bugwrap

**Smart Context code review for Python — local models, zero dependencies, provable findings.**

[![CI](https://github.com/AInsane/bugwrap/actions/workflows/ci.yml/badge.svg)](https://github.com/AInsane/bugwrap/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Dependencies: 0](https://img.shields.io/badge/dependencies-0-brightgreen.svg)](pyproject.toml)
[![Ollama](https://img.shields.io/badge/runs%20on-Ollama-white.svg)](https://ollama.com)

</div>

---

Most AI review tools hand the model a raw `git diff` and hope. A diff hunk is the worst
possible unit of review: three lines of context, none of the blast radius.
`calculate_discount(price, pct)` gaining a required parameter looks harmless in isolation —
the bug is in `order.py:13`, which the model never saw.

**bugwrap builds the packet the reviewer actually needs**, and proves what it can before
a model ever runs:

```console
$ bugwrap review

shop/order.py
    HIGH  contract  `calculate_discount()` call no longer binds: is missing
          required argument `currency` (95%)
          shop/order.py:13
          The signature changed to `def calculate_discount(price, pct, currency)`
          (was `def calculate_discount(price, pct)`). This call —
          `net = calculate_discount(self.subtotal(), self.discount_pct)` —
          raises TypeError at runtime.
          → update the call to match the new signature

1 packet(s) reviewed · 3 high · 875 prompt tok · 18.7s
```

## Why

|  | naive full-context | **bugwrap Smart Context** |
|---|---|---|
| tokens, 150-module repo, 1 change | 22,462 | **649 (34× less)** |
| growth with repo size | linear — overflows local context | **flat — tracks blast radius** |
| seeded-bug recall (7B model) | 62%, hallucinates on controls | **100% / 100% precision** |
| finds the caller three files away | no | **yes — that's the point** |

*Measured, not asserted: `python -m bench.context_bench`. Full numbers in [ROADMAP.md](ROADMAP.md#benchmark-log).*

## How it works

```
git diff → AST symbols → call graph → contract delta → impact packet → static proof + local LLM
```

1. **Own the lines** — map every changed line to the function/class that owns it (`ast`, line-precise).
2. **Know the callers** — an import-aware, type-aware, hierarchy-aware call graph of the whole repo. Every edge carries a confidence; nothing is guessed without evidence.
3. **Detect the contract change** — signature diffs, raise-set/return-shape/generator changes, removed dataclass fields, subclass overrides that diverge.
4. **Assemble the packet** — the diff, the changed body, and the exact call sites that break, budget-packed to your model's context window.
5. **Prove first, prompt second** — a deterministic re-binder checks every call site the way CPython would (zero tokens, ~always right); the model only reviews what isn't provable, and every model finding must survive an adversarial verify pass.

Deep dive with diagrams: **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** · research notes: [docs/RESEARCH.md](docs/RESEARCH.md)

## Quickstart

```bash
pip install -e .                  # zero runtime dependencies
ollama pull qwen2.5-coder:14b     # or 7b / 3b — smaller works, layering carries it
bugwrap doctor                    # checks git, ollama, model, context sizing
```

```bash
bugwrap review                    # review uncommitted work
bugwrap review --base main        # everything on this branch
bugwrap review --pr 482 --post    # review a GitHub PR, post results back
bugwrap review src/auth/          # whole-file audit, ordered by PageRank centrality

bugwrap check                     # static layer only: no model, no tokens, instant.
                                  # exit 1 on provable breakage — made for CI
```

Debug the context, not the model:

```bash
bugwrap context --base main       # exactly what would be sent, and why
bugwrap context --prompt          # the full rendered packet
bugwrap index --symbol calc       # who calls this?
bugwrap index --top 10            # the repo's most load-bearing symbols
```

## The two layers

**Static layer** — when a signature changes, re-bind every known call site exactly the way
CPython would: missing required argument, extra positional, renamed keyword, positional-only
violation, deleted symbol still called, newly-async function not awaited, override diverging
from its parent. Emitted only when provable from the AST; silent whenever `*args`/`**kwargs`
makes it unprovable. **95% confidence, zero tokens.**

**Model layer** — reviews the packet for what isn't provable: logic, behaviour, None-paths,
security. It is told what's already flagged (no duplicate comments), its findings on
statically-owned lines are suppressed, and each one must survive a skeptic prompt whose
default position is *"this report is wrong."* On our bench this layering takes a 3B model
from 54% → 100% precision.

## Benchmarks

```bash
python -m bench.run                        # contract-bench: seeded bugs + silence controls
python -m bench.run --llm                  # with the model layer
python -m bench.context_bench --scale-only # smart vs full-context token table
```

contract-bench seeds real contract bugs (required param added, kwarg renamed, sync→async,
field removed, override divergence, …) plus **controls where the correct review is
silence** — flagging anything on them is a false positive. Current: **17 scenarios,
100% precision / 100% recall**, static layer alone. Scoring follows the
[Martian code-review-benchmark](https://github.com/withmartian/code-review-benchmark)
methodology; a [`bench/martian.py`](bench/martian.py) adapter is ready for the head-to-head.

## Configuration

`bugwrap init` writes a `.bugwrap.toml`:

```toml
[model]
name = "qwen2.5-coder:14b"
num_ctx = 16384          # must match what the model can actually hold

[review]
fail_on = "high"         # exit 1 at or above this severity — for CI
verify = true            # adversarial second pass over model findings

[index]
cache = true             # mtime-keyed parse cache in .bugwrap/
```

Env overrides: `BUGWRAP_HOST` / `OLLAMA_HOST`, `BUGWRAP_MODEL`, `BUGWRAP_NUM_CTX`.
Ollama silently truncates oversized prompts — bugwrap packs to ~65% of `num_ctx`, warns
when `num_ctx` exceeds the model's trained window, and reports any packet that hit the ceiling.

## As a library

```python
from bugwrap import review_changes, context_for

units, index = context_for(".", base="main")   # packets, zero tokens
result = review_changes(".", base="main")      # full review
for f in result.findings:
    print(f.severity, f"{f.file}:{f.line}", f.title)
```

## CI

```yaml
- run: bugwrap check --base ${{ github.base_ref }} --json findings.json   # no model needed
```

Exit codes: `0` clean · `1` findings at or above `fail_on` · `2` tool error.

## Contributing

Two gates protect every resolver change: `python -m bench.run` (precision must stay 100%)
and the stdlib sanity check. See [CONTRIBUTING.md](CONTRIBUTING.md) and
[ROADMAP.md](ROADMAP.md) for where the project is heading.

## License

[MIT](LICENSE)
