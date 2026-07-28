"""Drive the model over the packets and normalize what comes back."""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from ..config import Config
from ..llm import OllamaClient, OllamaError
from ..models import ChangeUnit, Finding, severity_rank
from .prompts import (
    FINDINGS_SCHEMA,
    SYSTEM,
    VERDICT_SCHEMA,
    VERIFY_SYSTEM,
    render_unit,
    render_verification,
)

_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


@dataclass
class ReviewResult:
    findings: list[Finding] = field(default_factory=list)
    units: int = 0
    errors: list[str] = field(default_factory=list)
    prompt_tokens: int = 0
    eval_tokens: int = 0
    duration_ms: int = 0
    truncated_units: int = 0

    def worst_severity(self) -> str | None:
        if not self.findings:
            return None
        return max(self.findings, key=lambda f: severity_rank(f.severity)).severity


def merge_findings(static: list[Finding], llm: list[Finding]) -> list[Finding]:
    """Static findings own their lines. An LLM finding landing on (or next to) a
    line the deterministic layer already covered is the same issue reworded —
    drop it rather than double-commenting."""
    taken = {(f.file, f.line) for f in static if f.line is not None}
    out = list(static)
    for finding in llm:
        if finding.line is not None and any(
            (finding.file, finding.line + delta) in taken for delta in (-1, 0, 1)
        ):
            continue
        out.append(finding)
    return out


def review_units(
    units: list[ChangeUnit],
    cfg: Config,
    client: OllamaClient | None = None,
    on_progress=None,
    known: list[Finding] | None = None,
) -> ReviewResult:
    client = client or OllamaClient(
        host=cfg.host,
        model=cfg.model,
        num_ctx=cfg.num_ctx,
        temperature=cfg.temperature,
        think=cfg.think,
        timeout=cfg.timeout,
    )
    result = ReviewResult(units=len(units))

    # Keyed by fqname — a bare qualname collides across modules and would
    # cross-inject one unit's findings into another's prompt.
    known_by_symbol: dict[str | None, list[Finding]] = {}
    for finding in known or []:
        known_by_symbol.setdefault(finding.symbol, []).append(finding)

    def verify(unit: ChangeUnit, findings: list[Finding]) -> list[Finding]:
        """Adversarial second pass: the model must defend each of its own findings
        against a skeptic prompt. Kills the hallucinated ones; static findings
        never come through here (they're proof-backed)."""
        kept: list[Finding] = []
        for finding in findings:
            try:
                chat = client.chat(
                    VERIFY_SYSTEM, render_verification(unit, finding), schema=VERDICT_SCHEMA
                )
                verdict = _load_json(chat.content) or {}
            except OllamaError:
                verdict = {"real": True}  # verification unavailable — keep the finding
            if verdict.get("real"):
                try:
                    conf = float(verdict.get("confidence", finding.confidence))
                except (TypeError, ValueError):
                    conf = finding.confidence
                # The verifier's confidence CAPS the finding's own. A verifier
                # saying real=true, confidence=0.0 sinks it below the floor —
                # that is intended, not a bug.
                finding.confidence = min(finding.confidence, conf)
                kept.append(finding)
        return kept

    def work(unit: ChangeUnit):
        unit_known = known_by_symbol.get(unit.symbol.fqname if unit.symbol else None, [])
        prompt = render_unit(unit, known=unit_known)
        try:
            chat = client.chat(SYSTEM, prompt, schema=FINDINGS_SCHEMA)
        except OllamaError as exc:
            return unit, None, str(exc)
        return unit, chat, None

    workers = max(1, cfg.workers)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for unit, chat, error in pool.map(work, units):
            if on_progress:
                on_progress(unit, error)
            if error:
                result.errors.append(f"{unit.title}: {error}")
                continue
            result.prompt_tokens += chat.prompt_tokens
            result.eval_tokens += chat.eval_tokens
            result.duration_ms += chat.duration_ms
            if chat.truncated:
                result.truncated_units += 1
            found = parse_findings(chat.content, unit)
            if cfg.verify and found:
                found = verify(unit, found)
            result.findings.extend(found)

    result.findings = postprocess(result.findings, cfg)
    return result


def parse_findings(content: str, unit: ChangeUnit) -> list[Finding]:
    payload = _load_json(content)
    if not payload:
        return []
    raw = payload.get("findings")
    if isinstance(payload, list):
        raw = payload
    if not isinstance(raw, list):
        return []

    findings: list[Finding] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        try:
            line = int(item.get("line")) if item.get("line") is not None else None
        except (TypeError, ValueError):
            line = None
        try:
            confidence = float(item.get("confidence", 0.5))
        except (TypeError, ValueError):
            confidence = 0.5
        findings.append(
            Finding(
                file=str(item.get("file") or unit.path),
                line=line,
                symbol=unit.symbol.fqname if unit.symbol else None,
                severity=str(item.get("severity") or "medium").lower(),
                category=str(item.get("category") or "logic").lower(),
                title=title,
                detail=str(item.get("detail") or "").strip(),
                confidence=max(0.0, min(1.0, confidence)),
                suggestion=(str(item["suggestion"]).strip() or None)
                if item.get("suggestion")
                else None,
            )
        )
    return findings


def _load_json(content: str) -> dict | list | None:
    content = content.strip()
    if not content:
        return None
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass
    # thinking models sometimes wrap the object in prose or fences
    match = _JSON_BLOCK.search(content)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    return None


def postprocess(findings: list[Finding], cfg: Config) -> list[Finding]:
    seen: set[tuple] = set()
    kept: list[Finding] = []
    for finding in findings:
        if finding.confidence < cfg.min_confidence:
            continue
        if finding.key() in seen:
            continue
        seen.add(finding.key())
        kept.append(finding)
    kept.sort(key=lambda f: (-severity_rank(f.severity), -f.confidence, f.file, f.line or 0))
    return kept
