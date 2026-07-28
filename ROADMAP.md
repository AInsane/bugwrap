# bugwrap roadmap

Living document. Items move ⏳ → 🔨 → ✅; new ideas land in **Backlog** with a
value/effort guess. Every resolver-touching change must pass both gates before
its box gets checked:

- **Gate A** — `python -m bench.run` (contract-bench): precision must stay 100%;
  recall must not drop.
- **Gate B** — stdlib sanity: index CPython's stdlib, eyeball the top-callers
  list (must stay `re.compile` / `argparse.add_argument`-shaped, no protocol-method
  garbage), `too_ambiguous` visible.

## Done ✅

- ✅ **Core pipeline** — diff → ast symbols → call graph → contract delta →
  budget-packed impact packet → Ollama (JSON-schema constrained) → findings
- ✅ **Evidence-rule resolver** — name-fallback only with import evidence; builtins
  and protocol-method calls on untyped receivers dropped, not guessed
- ✅ **Local type binding** — `cart = Cart()`, `cart: Cart`, annotated params →
  `typed` edges (conf 0.9); fixed the method-call bench miss
- ✅ **Static call-site re-binder** (`bugwrap check`, merged into `review`) —
  CPython-faithful arg binding at every call site; 95%-confidence deterministic
  findings; silent under `*args`/`**kwargs`. contract-bench: 100% P / 100% R
- ✅ **contract-bench** (`bench/`) — seeded contract bugs + silence controls,
  Martian-style precision/recall scoring
- ✅ **Behaviour contract deltas** — raise-set changes, return-shape changes,
  function↔generator toggle (breaking), surfaced in the packet
- ✅ **Class hierarchy** — base-class resolution with import evidence, inherited
  method binding (`Derived().method()` → `Base.method` when not overridden),
  override divergence check when a parent signature changes, overrides listed
  in the packet
- ✅ **Co-change signal** — `git log` mining; frequently-co-changed-but-untouched
  files surfaced as packet notes on contract changes
- ✅ **PageRank centrality** — power iteration over the call graph; orders
  whole-file audits so central code is reviewed first; `bugwrap index --top`
- ✅ **Two-hop impact** — for breaking changes, callers-of-callers through
  `*args`/`**kwargs` wrapper functions (the packet shows where arguments
  actually originate)

- ✅ **Attribute-read index + field contracts** — `obj.field` reads indexed on
  proven receivers (self/typed); removed dataclass/`__init__` fields find their
  readers hierarchy-wide (inherited fields too); a field surviving as a
  `@property`/method is correctly not "removed". Static AttributeError findings
- ✅ **Layered LLM integration** — static findings injected into the prompt
  ("already flagged — do not repeat"), line-collision merge suppression, and an
  adversarial verify pass over every LLM finding (skeptic prompt must fail to
  refute it). Took static+3B from 54% → 100% precision on contract-bench
- ✅ **LLM-layer eval** — see benchmark log; the ablation shows the context
  architecture, not model size, carries the result

- ✅ **Architecture documentation** — `docs/ARCHITECTURE.md`: full pipeline +
  module-dependency mermaid diagrams, all ten layers explained (data structures,
  precision rules, non-goals), packet anatomy, resolution-quality ladder,
  findings-flow sequence. Written by a dedicated audit agent, verified against
  source
- ✅ **Clarity/bug audit applied** — 14 of the agent's 15 findings fixed, incl.
  real bugs: known-findings cross-injection between same-named symbols (now keyed
  by fqname), `--no-tests` sites eating packet slots (filter before slice),
  deleted-file `dropped_callers` always 0, ambiguous-fanout edge aliasing
  (per-candidate copies), string-matched `went_async` (now a structured flag),
  verify-confidence 0.0 silently ignored (now caps the finding), dead code
  removed, private cross-module access made public accessors
- ✅ **Prompt efficiency pass** — diff context-lines stripped (they duplicated
  CURRENT SOURCE), minimal-width gutters, PREVIOUS VERSION gated on behaviour
  deltas, callee list dropped from contract packets, compact closing instruction,
  verify prompt pruned from full-packet to claim-relevant context (~40-70% of
  verify tokens), token estimator recalibrated 3.0→3.3 chars/tok. Net: **−16%
  per demo packet (604→510), −23% on scale packets, all quality gates unchanged**
- ✅ **context-bench** (`bench/context_bench.py`) — Smart Context vs naive
  full-context baseline; see benchmark log

## In progress 🔨

- 🔨 **Martian benchmark adapter** (`bench/martian.py`) — adapter script ready;
  full 50-PR head-to-head vs CodeRabbit/Greptile pending a GitHub org + judge
  API key. Python repo in the set is Sentry.

## Planned ⏳

- ⏳ **`bugwrap review --pr --post` inline comments** (S) — post per-line review
  comments instead of one summary comment (gh api), matching how CodeRabbit
  comments land
- ⏳ **Import-cycle / import-time side-effect checks** (S) — module-unit static
  checks: new import cycles, top-level I/O
- ⏳ **Optional `--lsp` extra (Jedi)** (L) — only if evidence-rule precision
  proves insufficient on the Martian benchmark; benchmark first

## Backlog / ideas 💡

- 💡 **Logic-bug bench scenarios** — seeded off-by-one, inverted condition,
  None-path bugs where the static layer *can't* fire; measures the LLM layer's
  real value-add (contract-bench's golden bugs are all statically provable)
- 💡 Exception-flow impact: when raise-set widens, find callers with `try/except`
  that don't catch the new exception
- 💡 `bugwrap bench --repo <url>` — replay historical bug-fix commits of a real
  repo as benchmark cases (reverse the fix = seeded bug with golden finding)
- 💡 Test-gap findings: contract change with zero test-file callers → "untested
  contract change" info finding
- 💡 SARIF output for GitHub code scanning integration
- 💡 Watch mode (`bugwrap review --watch`) reviewing on save
- 💡 CodeFuse-CR-Bench / SWR-Bench adapters (both Python, repo-level)

## Benchmark log

| Date | Change | contract-bench | stdlib sanity |
|---|---|---|---|
| 2026-07-28 | static re-binder + typed edges | 12 scenarios, P 100% / R 100% | clean (top: re.compile, argparse.add_argument) |
| 2026-07-28 | hierarchy + behaviour deltas + 2-hop + co-change + pagerank | **15 scenarios, P 100% / R 100%** (adds inherited_method_call, override_divergence, generator_toggle) | clean — 2.9s cold, 1874 hierarchy edges, top: re.compile / argparse.add_argument / io.Writer.write; pagerank 0.2s over 12k nodes, top: re._compile |
| 2026-07-28 | attribute-read index + field contracts | **17 scenarios, P 100% / R 100%** (adds field_removed + property-shim control) | clean — 3.0s, 26k proven reads indexed, top callers unchanged |

### LLM-layer eval (17 scenarios, num_ctx 8192)

| Mode | Model | Precision | Recall | F1 | Reading |
|---|---|---|---|---|---|
| static-only | — | **100%** | **100%** | 1.00 | the deterministic floor |
| llm-only (packets, no static layer) | 3b | 50% | 8% | 0.13 | the ablation |
| llm-only (packets, no static layer) | 7b | 100% | 8% | 0.14 | cleaner, still can't localize call-site breakage |
| static+llm, naive merge | 3b | 54% | 100% | 0.70 | model re-reports static findings in different words |
| static+llm, layered (known-findings prompt + line merge) | 3b | 87% | 100% | 0.93 | duplicates gone; 3B hallucinates on controls ~50% of runs |
| static+llm, layered + adversarial verify | 3b | **100%** | **100%** | 1.00 | verify pass kills hallucinations; stable across re-runs |
| static+llm, layered + adversarial verify | 7b | **100%** | **100%** | 1.00 | |

Takeaways: (1) the context architecture, not model size, carries recall — llm-only
drops to 8% on both models (they comment on the definition, not the breaking call
sites; the static layer owns localization); (2) precision on small models requires
the verify pass — on by default (`[review] verify = true`); (3) contract-bench's
golden bugs are all statically provable, so the LLM's value-add must be measured
on logic-bug scenarios next (see Backlog). Also verified live: `bugwrap review`
against a real Ollama (7B) produces exactly the static findings with zero LLM
duplicates — the prompt-level layering works outside the bench too.

### context-bench: Smart Context vs full-context baseline (2026-07-29)

Baseline = what diff-plus-context tools do: diff + full changed file + full text
of every file importing it, one prompt per changed file. Same model, schema,
scoring. After the prompt-efficiency pass:

**Tokens (synthetic repos, one breaking change, estimator):**

| repo size | full-context | smart | ratio |
|---|---|---|---|
| 10 modules | 1,345 tok | 426 tok | 3.2× |
| 50 modules | 7,219 tok | 643 tok | 11.2× |
| 150 modules | 22,462 tok | 649 tok | **34.6×** |

Full-context grows linearly with repo size (and blows any local num_ctx well
before 1k files); Smart Context stays flat because packet size tracks the
change's blast radius, not the repo.

**Quality (17 scenarios, real model, prompt tokens actually billed):**

| approach | model | precision | recall | prompt tok |
|---|---|---|---|---|
| smart (static + layered LLM + verify) | 3b | **100%** | **100%** | 10,701 |
| full-context | 3b | 0–100%* | **0%** | 6,824 |
| full-context | 7b | 47% | 62% | 6,824 |

*3B on full context returns nothing or noise depending on the run.

At toy scale the baseline is cheaper per run — and finds nothing (3B) or
hallucinates on 4 of 5 controls (7B). At real repo scale it is both blind AND
34× more expensive. That is the whole thesis of the tool, now measured.
