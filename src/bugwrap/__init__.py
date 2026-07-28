"""bugwrap — Smart Context code review for Python repositories.

Pipeline:
    git diff  ->  ast symbols  ->  call graph  ->  contract delta
              ->  impact packet (budget-packed)  ->  local model  ->  findings

Programmatic use:

    from pathlib import Path
    from bugwrap import review_changes

    result = review_changes(Path("."), base="main")
    for finding in result.findings:
        print(finding.severity, finding.file, finding.title)
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = [
    "__version__",
    "review_changes",
    "context_for",
    "Config",
    "Finding",
    "ChangeUnit",
]


def __getattr__(name: str):  # lazy re-export, keeps `import bugwrap` cheap
    if name in ("review_changes", "context_for"):
        from .api import context_for, review_changes

        return {"review_changes": review_changes, "context_for": context_for}[name]
    if name == "Config":
        from .config import Config

        return Config
    if name in ("Finding", "ChangeUnit"):
        from . import models

        return getattr(models, name)
    raise AttributeError(name)
