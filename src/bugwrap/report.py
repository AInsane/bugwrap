"""Output: terminal, JSON, and GitHub PR comments."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from .models import ChangeUnit, Finding
from .review.runner import ReviewResult

_COLORS = {
    "reset": "\033[0m",
    "dim": "\033[2m",
    "bold": "\033[1m",
    "red": "\033[31m",
    "yellow": "\033[33m",
    "green": "\033[32m",
    "blue": "\033[34m",
    "magenta": "\033[35m",
    "cyan": "\033[36m",
}

SEVERITY_COLOR = {
    "critical": "magenta",
    "high": "red",
    "medium": "yellow",
    "low": "blue",
    "info": "dim",
}


def use_color(stream=sys.stdout) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    return hasattr(stream, "isatty") and stream.isatty()


class Painter:
    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def __call__(self, text: str, *styles: str) -> str:
        if not self.enabled or not styles:
            return text
        prefix = "".join(_COLORS.get(s, "") for s in styles)
        return f"{prefix}{text}{_COLORS['reset']}"


def print_units(units: list[ChangeUnit], paint: Painter, verbose: bool = False) -> None:
    """`bugwrap context` — show what the model would be told, and nothing more."""
    for unit in units:
        flag = ""
        if unit.delta.breaking:
            flag = paint(" BREAKING", "red", "bold")
        elif unit.delta.is_contract_change:
            flag = paint(" contract", "yellow")
        print(paint(f"\n■ {unit.title}", "bold") + flag)
        detail = f"  {unit.delta.kind} · ~{unit.tokens} tok · {len(unit.callers)} call site(s)"
        if unit.dropped_callers:
            detail += f" (+{unit.dropped_callers} omitted)"
        print(paint(detail, "dim"))
        for item in unit.delta.details[:6]:
            print(paint(f"    - {item}", "dim"))
        for impact in unit.callers:
            tag = paint(" [test]", "dim") if impact.is_test else ""
            print(f"    → {impact.site.path}:{impact.site.line}{tag}  {paint(impact.site.text[:80], 'dim')}")
        if verbose:
            from .review.prompts import render_unit

            print(paint("  " + "-" * 60, "dim"))
            print(render_unit(unit))


def print_findings(result: ReviewResult, paint: Painter, root: Path | None = None) -> None:
    if not result.findings:
        print(paint("\n✓ no findings", "green", "bold"))
    else:
        by_file: dict[str, list[Finding]] = {}
        for finding in result.findings:
            by_file.setdefault(finding.file, []).append(finding)

        for file, items in by_file.items():
            print(paint(f"\n{file}", "bold", "cyan"))
            for finding in items:
                color = SEVERITY_COLOR.get(finding.severity, "yellow")
                badge = paint(f"{finding.severity.upper():>8}", color, "bold")
                loc = f":{finding.line}" if finding.line else ""
                conf = paint(f"({finding.confidence:.0%})", "dim")
                print(f"  {badge}  {paint(finding.category, 'dim')}  {finding.title} {conf}")
                print(f"           {paint(f'{file}{loc}', 'dim')}")
                for line in _wrap(finding.detail, 88):
                    print(f"           {line}")
                if finding.suggestion:
                    for line in _wrap(f"→ {finding.suggestion}", 88):
                        print(paint(f"           {line}", "green"))

    counts: dict[str, int] = {}
    for finding in result.findings:
        counts[finding.severity] = counts.get(finding.severity, 0) + 1
    summary = ", ".join(f"{n} {sev}" for sev, n in sorted(counts.items())) or "clean"
    stats = (
        f"\n{result.units} packet(s) reviewed · {summary} · "
        f"{result.prompt_tokens} prompt tok · {result.duration_ms / 1000:.1f}s"
    )
    print(paint(stats, "dim"))

    if result.truncated_units:
        print(
            paint(
                f"warning: {result.truncated_units} packet(s) filled the context window — "
                "raise [model] num_ctx or lower max_callers",
                "yellow",
            )
        )
    for error in result.errors[:5]:
        print(paint(f"error: {error}", "red"))


def _wrap(text: str, width: int) -> list[str]:
    import textwrap

    out: list[str] = []
    for paragraph in text.splitlines():
        out.extend(textwrap.wrap(paragraph, width) or [""])
    return out


def write_json(result: ReviewResult, path: str, meta: dict | None = None) -> None:
    payload = {
        "meta": meta or {},
        "summary": {
            "units": result.units,
            "findings": len(result.findings),
            "worst_severity": result.worst_severity(),
            "prompt_tokens": result.prompt_tokens,
            "eval_tokens": result.eval_tokens,
            "duration_ms": result.duration_ms,
            "errors": result.errors,
        },
        "findings": [f.to_dict() for f in result.findings],
    }
    text = json.dumps(payload, indent=2)
    if path == "-":
        print(text)
    else:
        Path(path).write_text(text)


def units_json(units: list[ChangeUnit]) -> str:
    from .review.prompts import render_unit

    return json.dumps(
        [
            {
                "path": unit.path,
                "symbol": unit.symbol.qualname if unit.symbol else None,
                "fqname": unit.symbol.fqname if unit.symbol else None,
                "delta": {
                    "kind": unit.delta.kind,
                    "breaking": unit.delta.breaking,
                    "contract_change": unit.delta.is_contract_change,
                    "details": unit.delta.details,
                    "old_signature": unit.delta.old_signature,
                    "new_signature": unit.delta.new_signature,
                },
                "callers": [
                    {
                        "path": i.site.path,
                        "line": i.site.line,
                        "caller": i.site.caller,
                        "resolution": i.site.resolution,
                        "is_test": i.is_test,
                        "text": i.site.text,
                    }
                    for i in unit.callers
                ],
                "total_callers": unit.total_callers,
                "dropped_callers": unit.dropped_callers,
                "tokens": unit.tokens,
                "prompt": render_unit(unit),
            }
            for unit in units
        ],
        indent=2,
    )


def post_to_github(result: ReviewResult, pr: int, cwd: Path) -> tuple[bool, str]:
    """Post findings as a single PR review body via gh."""
    if not result.findings:
        body = "**bugwrap**: no findings."
    else:
        lines = ["## bugwrap review", ""]
        for finding in result.findings:
            loc = f"`{finding.file}:{finding.line}`" if finding.line else f"`{finding.file}`"
            lines.append(
                f"- **{finding.severity.upper()}** · {finding.category} · {loc} — "
                f"**{finding.title}** ({finding.confidence:.0%})"
            )
            if finding.detail:
                lines.append(f"  {finding.detail}")
            if finding.suggestion:
                lines.append(f"  _suggestion:_ {finding.suggestion}")
        body = "\n".join(lines)

    proc = subprocess.run(
        ["gh", "pr", "comment", str(pr), "--body-file", "-"],
        input=body,
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return False, proc.stderr.strip()
    return True, proc.stdout.strip()
