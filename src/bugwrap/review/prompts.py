"""Prompt construction and the findings schema.

Written for local 7B–32B coder models: short instructions, hard constraints,
an explicit escape hatch for "nothing wrong here", and a JSON schema so the
output is parseable without regex archaeology.
"""

from __future__ import annotations

from ..models import ChangeUnit

SYSTEM = """You are a precise Python code reviewer. You are given ONE changed \
symbol plus the exact call sites that could be affected by it.

Report only defects you can point at in the provided code:
- broken call contracts (a caller passes arguments the new signature rejects)
- logic errors, off-by-one, inverted conditions, wrong operator
- None/KeyError/IndexError/ZeroDivision risks on reachable paths
- resource leaks, unawaited coroutines, mutable default arguments
- silent behaviour changes for existing callers
- security: injection, path traversal, unsafe deserialization, hardcoded secrets

Do NOT report:
- formatting, naming, style, typing nits, missing docstrings
- speculation about code you were not shown ("if X is None somewhere else...")
- praise, summaries, or restatements of the diff

Rules:
- Every finding must cite a file and a line number that appears in the context.
- If the change is correct, return an empty findings list. That is a good answer.
- Prefer 0 findings over 1 guess. Set confidence below 0.5 when unsure."""

FINDINGS_SCHEMA = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "file": {"type": "string"},
                    "line": {"type": "integer"},
                    "severity": {
                        "type": "string",
                        "enum": ["info", "low", "medium", "high", "critical"],
                    },
                    "category": {
                        "type": "string",
                        "enum": [
                            "contract",
                            "logic",
                            "exception",
                            "security",
                            "concurrency",
                            "performance",
                            "resource",
                            "test-gap",
                        ],
                    },
                    "title": {"type": "string"},
                    "detail": {"type": "string"},
                    "confidence": {"type": "number"},
                    "suggestion": {"type": "string"},
                },
                "required": ["file", "line", "severity", "category", "title", "detail", "confidence"],
            },
        }
    },
    "required": ["findings"],
}


VERIFY_SYSTEM = """You are a skeptical senior reviewer double-checking a reported \
bug. Your default position is that the report is WRONG. Confirm it only if you can \
point at the exact code that proves it. A report that describes correct code as \
buggy, invents behaviour not visible in the context, or hedges ("might", "could") \
without evidence is refuted."""

VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "real": {"type": "boolean"},
        "confidence": {"type": "number"},
        "reason": {"type": "string"},
    },
    "required": ["real", "confidence", "reason"],
}


def render_verification(unit: ChangeUnit, finding) -> str:
    """Compact context for the skeptic: the contract block, the code the claim
    points at, and nothing else. Re-sending the whole packet per finding was the
    single largest token line item in a chatty run (~N full packets per unit)."""
    parts = [
        "A reviewer reported this issue:",
        "",
        f"  file: {finding.file}:{finding.line}",
        f"  claim: {finding.title}",
        f"  detail: {finding.detail}",
        "",
    ]
    delta = unit.delta
    if delta.old_signature or delta.new_signature or delta.details:
        parts.append("The change under review:")
        if delta.old_signature:
            parts.append(f"  before: {delta.old_signature}")
        if delta.new_signature:
            parts.append(f"  after:  {delta.new_signature}")
        parts.extend(f"  - {d}" for d in [*delta.details, *delta.behavior])
        parts.append("")

    # the code the claim points at: matching call site, else the changed source
    shown = False
    for impact in unit.callers:
        if impact.site.path == finding.file and finding.line is not None and abs(
            impact.site.line - finding.line
        ) <= 4:
            parts += [f"Code at {finding.file}:{finding.line}:", "```python", impact.snippet, "```", ""]
            shown = True
            break
    if not shown:
        parts += ["The changed code:", "```python", unit.new_source.strip("\n"), "```", ""]

    parts.append(
        "Is the claim a real, demonstrable defect in THIS code? "
        "Refute it unless the code proves it."
    )
    return "\n".join(parts)


def _compact_diff(diff_text: str) -> str:
    """Hunk headers plus +/- lines only. Context lines cost tokens twice: they
    are already present, line-numbered, in the CURRENT SOURCE section."""
    kept = [
        line
        for line in diff_text.strip("\n").splitlines()
        if line.startswith(("@@", "+", "-", "---", "+++"))
    ]
    return "\n".join(kept)


def render_unit(unit: ChangeUnit, known=()) -> str:
    """The 'impact snippet': diff + changed symbol + affected call sites.

    `known` — findings the deterministic layer already produced for this unit.
    Shown to the model so it spends its effort elsewhere instead of re-reporting
    them in different words (which double-comments the same line).
    """
    out: list[str] = []
    symbol = unit.symbol

    out.append(f"# FILE: {unit.path}")
    if symbol:
        out.append(f"# SYMBOL: {symbol.qualname}  ({symbol.kind}, lines {symbol.start_lineno}-{symbol.end_lineno})")
    out.append("")

    delta = unit.delta
    if delta.kind == "signature_changed":
        out.append("## CONTRACT CHANGE — the public signature changed")
        out.append(f"before: {delta.old_signature}")
        out.append(f"after:  {delta.new_signature}")
        for item in delta.details:
            out.append(f"  - {item}")
        if delta.breaking:
            out.append("  !! This is source-incompatible. Existing callers may now be wrong.")
    elif delta.kind == "added":
        out.append("## NEW DEFINITION")
    elif delta.kind == "removed":
        out.append("## REMOVED — callers below now reference something that no longer exists")
    elif delta.kind == "full_review":
        out.append("## FULL REVIEW (no diff — judge the code as written)")
    else:
        out.append("## BODY-ONLY CHANGE — signature is unchanged, so callers still type-check")
        if delta.new_signature:
            out.append(f"signature: {delta.new_signature}")
    if delta.behavior:
        out.append("")
        out.append("## BEHAVIOUR CONTRACT CHANGED")
        for item in delta.behavior:
            out.append(f"  - {item}")
    out.append("")

    out.append("## DIFF (context lines omitted — see CURRENT SOURCE)")
    out.append("```diff")
    out.append(_compact_diff(unit.diff_text))
    out.append("```")
    out.append("")

    if unit.old_source:
        out.append("## PREVIOUS VERSION")
        out.append("```python")
        out.append(unit.old_source.strip("\n"))
        out.append("```")
        out.append("")

    out.append("## CURRENT SOURCE (line-numbered)")
    out.append("```python")
    out.append(unit.new_source.strip("\n"))
    out.append("```")
    out.append("")

    if unit.callers:
        header = f"## AFFECTED CALL SITES ({len(unit.callers)}"
        if unit.dropped_callers:
            header += f" of {unit.total_callers}, {unit.dropped_callers} omitted"
        out.append(header + ")")
        for impact in unit.callers:
            site = impact.site
            tag = " [test]" if impact.is_test else ""
            if impact.hop == 2 and impact.via:
                tag += f" [2 hops — reaches the change through `{impact.via}`]"
            enclosing = f" inside {impact.enclosing_signature}" if impact.enclosing_signature else ""
            out.append(f"### {site.path}:{site.line}{tag}{enclosing}")
            out.append("```python")
            out.append(impact.snippet)
            out.append("```")
        out.append("")
    elif symbol:
        out.append("## AFFECTED CALL SITES")
        out.append("(none found in this repository — it may be a public entry point, "
                   "called dynamically, or dead code)")
        out.append("")

    if unit.overrides:
        out.append("## OVERRIDDEN BY (subclasses defining the same method)")
        for override in unit.overrides:
            out.append(f"- {override.path}:{override.lineno}  {override.fqname}: {override.signature}")
        out.append("")

    if unit.callees:
        out.append("## CALLS OUT TO")
        for callee in unit.callees:
            out.append(f"- {callee.path}:{callee.lineno}  {callee.signature}")
        out.append("")

    if unit.notes:
        out.append("## NOTES")
        for note in unit.notes:
            out.append(f"- {note}")
        out.append("")

    if known:
        out.append("## ALREADY FLAGGED BY STATIC ANALYSIS — do NOT repeat these")
        for finding in known:
            loc = f"{finding.file}:{finding.line}" if finding.line else finding.file
            out.append(f"- {loc}  {finding.title}")
        out.append("Report only issues NOT in the list above. If nothing else is "
                   "wrong, return an empty findings list.")
        out.append("")

    out.append("Review the change. Empty findings list if it is sound.")
    return "\n".join(out)
