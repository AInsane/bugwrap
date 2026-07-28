# Contributing to bugwrap

Thanks for looking! The project is small on purpose — zero runtime dependencies,
one job done precisely. Contributions that fit that shape are very welcome.

## Setup

```bash
git clone https://github.com/AInsane/bugwrap && cd bugwrap
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest -q
```

## The two gates

Every change that touches the resolver, the packet builder, or the prompts must
pass both gates before review:

1. **contract-bench** — `python -m bench.run`. Precision must stay **100%**;
   recall must not drop. The control scenarios (where the correct review is
   silence) are the point: a false positive invites the model to invent a bug
   in unrelated code, which is worse than missing one.
2. **stdlib sanity** — index CPython's stdlib and eyeball the top-callers list:

   ```bash
   python - <<'EOF'
   import sysconfig
   from pathlib import Path
   from bugwrap.config import Config
   from bugwrap.index import build_index
   root = Path(sysconfig.get_paths()["stdlib"])
   idx = build_index(root, Config(use_cache=False, exclude=Config().exclude + ("test", "tests", "idlelib")))
   print(idx.stats)
   for n, s in sorted(((len(idx.graph.callers_of(x.fqname)), x.fqname) for x in idx.symbols.symbols), reverse=True)[:5]:
       print(n, s)
   EOF
   ```

   Healthy output is `re.compile` / `argparse.add_argument`-shaped. If a protocol
   method (`append`, `get`, `write` on an unrelated class) tops the list, the
   resolver has started guessing — that's a regression even if all tests pass.

If your change alters benchmark numbers, add a row to the benchmark log in
[ROADMAP.md](ROADMAP.md).

## Design rules of the house

- **Evidence over recall.** A call edge, base class, or attribute read only
  exists if the file gives evidence for it (import, local type binding, `self`).
  When unsure, drop it and count it in `too_ambiguous` — never guess silently.
- **Provable findings don't need a model.** If a breakage can be proven from the
  AST, it belongs in the static layer (`analysis/callcheck.py`) at high
  confidence, not in a prompt.
- **Zero runtime dependencies.** `ast`, `tomllib`, `urllib`, `subprocess`.
  A PR that adds a dependency needs an exceptional reason.
- **The packet is the product.** If review quality is bad, debug
  `bugwrap context --prompt` before touching prompts or models.

## Where to help

[ROADMAP.md](ROADMAP.md) tracks planned work and backlog ideas — the Martian
benchmark head-to-head, logic-bug scenarios, inline PR comments. Architecture
orientation: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
