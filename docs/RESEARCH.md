# Smart Context for Python: research notes & roadmap

*2026-07-28. State of the art survey + gap analysis of bugwrap's context engine.*

## Where bugwrap sits in the landscape

There are four families of "give the model the right code" approaches in the wild:

| Approach | Who | How | Trade-off |
|---|---|---|---|
| **Repo map + PageRank** | Aider | tree-sitter extracts defs/refs → directed graph → personalized PageRank ranks symbols, binary-search fills token budget | Great for *editing* ("what should I know about this repo"), weak for *review* — centrality ≠ blast radius |
| **Semantic code graph** | Greptile | Pre-indexes functions/classes/call relationships; on PR, multi-hop traversal: diff → affected dependencies → git history → cross-file impact | Closest to what we built; it's the approach that "catches the caller three files away" |
| **Diff + linter stack** | CodeRabbit | Diff-centric, 40+ linters/SAST layered on, PR history for context | Low false positives, but by design can't see cross-file breakage |
| **RAG / embeddings** | academic (Context-Aware Code Review Automation etc.) | retrieve "similar" code/comments by embedding similarity | Retrieval by *similarity* is the wrong metric for review; you want retrieval by *dependency* |

bugwrap is a deterministic, local version of the Greptile shape: dependency-driven retrieval, not similarity-driven. The AACR-Bench findings back this design: repository context substantially beats diff-only, **selective** context beats indiscriminate inclusion, and directly-related code beats distant dependencies. That's precisely our packet model.

Two more results worth internalizing:

- **Static call graphs beat LLMs at being call graphs.** An empirical study (EMSE 2025) found traditional static analysis (PyCG) consistently outperforms LLMs at call-graph construction. Don't ask the model to find the callers; hand them to it. (We do.)
- **Python type checkers are not type inferrers.** Jedi/Pyright/Mypy do well on annotated code and badly on unannotated code with dynamic patterns (TypeEvalPy benchmark). An LSP dependency would buy less precision than it appears to, at a large operational cost. Our zero-dep choice holds up.

## Honest gap analysis of the current engine

What we have: line→symbol ownership (ast), import-aware call graph with an evidence
rule, signature-contract diffing, deleted-symbol name-index lookup, budget packing.

What we're missing, ordered by how often it will bite:

### 1. No class hierarchy — the inheritance blind spot (biggest gap)
- `Base.process()` changes → calls through `Derived` instances aren't linked.
- Worse, the **override contract**: parent signature changes, subclass overrides keep the
  old signature. This is a classic real-world breakage and today we say nothing. PyCG's
  accuracy edge comes largely from modeling inheritance/MRO.

### 2. No local type binding for receivers
We drop `cart.recalculate()` unless the file imports `shop.cart`. But
`cart = Cart(...)` two lines above is proof of the receiver's type. A PyCG-style
"assignment graph lite" — track `var = ClassName(...)` within a scope, bind
`var.method` through it — converts a big slice of `attr`-resolution discards into
high-confidence edges. Flow-insensitive, one extra AST walk, still zero deps.

### 3. Signature ≠ the whole contract
Body-only changes that are actually contract changes:
- **Raise set changed** — `raise ValueError` added/removed changes what callers must catch. Statically visible (`ast.Raise`), cheap to diff.
- **Return shape changed** — `return x` → `return x, err`. Diffing the set of `ast.Return` value shapes catches the crude cases.
- **Class attribute contract** — dataclass fields / `__init__` assignments added or removed break `obj.field` readers. We index attribute *calls* but not attribute *reads*.

### 4. No git-history signal
AACR-Bench lists historical context as a top context category; Greptile checks git
history during its multi-hop pass. Cheapest useful version: co-change mining
(`git log --name-only`, count files committed together with the changed file) as a
ranking boost for impact sites and as "you usually also change X" notes in the packet.

### 5. One-hop impact only
For a breaking change, the caller's caller may hold the actual bug (a wrapper
forwards `**kwargs` and the real arguments come from two levels up). `**kwargs`
passthrough wrappers specifically defeat signature checking. Two-hop expansion,
gated on `delta.breaking` and capped hard, is the Greptile "multi-hop" move.

### 6. Whole-file mode has no ranking
`bugwrap review src/` reviews files in walk order. Aider's insight applies here:
PageRank over our existing call graph ranks symbols by centrality, so audits spend
tokens on load-bearing code first. We already have the graph; this is ~40 lines
(power iteration, no numpy).

## Benchmarks: how the field measures this (and how we do)

Public benchmarks that matter for us, most actionable first:

| Benchmark | What it is | Why it matters to bugwrap |
|---|---|---|
| [Martian code-review-benchmark](https://github.com/withmartian/code-review-benchmark) | 50 real PRs (Sentry for Python) with human-verified golden comments; LLM judge computes precision/recall; public leaderboard covers CodeRabbit, Greptile, Copilot, Cursor Bugbot, Qodo, … | The direct head-to-head route. Adapter: fork/checkout each PR locally, `bugwrap review --json`, map findings→comments, reuse their step3 judge |
| [CodeFuse-CR-Bench](https://arxiv.org/pdf/2509.14856) | 70 **Python** projects, end-to-end review with full repo context | The most on-target academic set — Python-only, repo-level |
| [SWR-Bench](https://arxiv.org/pdf/2509.01494) | Review comment generation derived from SWE-Bench's 12 Python projects | Real-world comment quality on repos we can index |
| [AACR-Bench](https://arxiv.org/pdf/2601.19494) | Repo-level context taxonomy + diff-only vs full-context ablations | Validates the packet design; useful for context-ablation studies |
| CodeReviewer (Li et al. 2022) / CRScore | The legacy datasets — diff-level fragments, no repo context | Avoid: diff-only evaluation is precisely the failure mode we exist to fix |

One caveat from the vendor-benchmark wars (every vendor's benchmark ranks
themselves first; [DeepSource's analysis](https://deepsource.com/blog/ai-code-review-benchmarks)
and the Augment-vs-Greptile 45%-vs-82% discrepancy on identical repos): repo
selection, "what counts as a bug", and partial-match scoring swing results
wildly. So we hold ourselves to the two things that can't be gamed: **controls
where silence is correct** (precision) and **golden findings a runtime error
would prove** (recall).

### contract-bench (ours, in `bench/`)

Seeded contract bugs in synthetic git repos + negative controls, scored
Martian-style. The result that matters for the CodeRabbit comparison: their
precision edge comes from a deterministic linter layer under the LLM. Our
equivalent — the static call-site re-binder (`analysis/callcheck.py`,
`bugwrap check`) — scores **100% precision / 100% recall** on the suite,
including staying silent on `**kwargs` forwarders and `*args` call sites where
breakage is unprovable. The LLM layer only has to add findings on top of a
floor that is already competitive.

The `method_signature_changed` scenario is what forced local type binding
(`cart = Cart(); cart.add(sku)` → `typed` edges, confidence 0.9) — roadmap
item #2, now shipped. Benchmark-driven development works.

## Roadmap (value ÷ effort, descending)

| # | Feature | Effort | Status / why |
|---|---|---|---|
| 1 | Class hierarchy index + override-contract check | M | **next** — biggest un-modeled bug class |
| 2 | Local type binding (`var = ClassName()` / annotations → `typed` edges) | S | ✅ shipped — fixed the method-call benchmark miss |
| 3 | Raise-set / return-shape contract deltas | S | contract changes invisible to signature diffing |
| 4 | Co-change ranking boost from git log | S | history signal, ~zero cost, no new deps |
| 5 | Two-hop impact for breaking changes (capped) | M | kwargs-forwarding wrappers, indirection layers |
| 6 | PageRank ordering for whole-file/audit mode | S | token budget goes to central code first |
| 7 | Attribute-read index (field contract) | M | dataclass/`__init__` field changes |
| 8 | Static call-site re-binding (deterministic findings) | M | ✅ shipped — `bugwrap check` + merged into `review` |
| 9 | Martian-benchmark adapter (run against CodeRabbit's actual scores) | M | the head-to-head number |
| 10 | Optional `--lsp` extra (Jedi) behind the SymbolTable interface | L | only if evidence-rule precision proves insufficient |

Suggested order: **3 → 1 → 9 → 4 → 6 → 5 → 7**. Re-run both benchmarks
(`python -m bench.run` and the stdlib top-callers sanity check) after every
resolver-touching change — the typed-binding work regressed nothing precisely
because both were re-run at each step.

## Sources

- [Aider repository mapping (tree-sitter + personalized PageRank)](https://deepwiki.com/Aider-AI/aider/4.1-repository-mapping-system), [freebird issue describing the algorithm](https://github.com/JoshCap20/freebird/issues/136), [RepoMapper reimplementation](https://github.com/pdavis68/RepoMapper)
- [Greptile — semantic code graph vs CodeRabbit's diff-centric approach](https://www.greptile.com/greptile-vs-coderabbit), [Panto AI comparison](https://www.getpanto.ai/blog/coderabbit-vs-greptile-ai-code-review-tools-compared)
- [AACR-Bench: Automatic Code Review with Holistic Repository-Level Context](https://arxiv.org/pdf/2601.19494)
- [LLMs vs static analysis for type & call graph construction (EMSE)](https://link.springer.com/article/10.1007/s10664-025-10704-3)
- [Scalable and Precise Application-Centered Call Graph Construction for Python (PyCG lineage)](https://arxiv.org/abs/2305.05949)
- [TypeEvalPy: micro-benchmarking Python type inference tools](https://arxiv.org/html/2312.16882v2)
- [Context-Aware Code Review Automation: A Retrieval-Augmented Approach](https://www.mdpi.com/2076-3417/16/4/1875)
- [Survey of code review benchmarks, pre-LLM and LLM era](https://arxiv.org/html/2602.13377v1)
