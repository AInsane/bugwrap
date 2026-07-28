"""Core data structures shared by every stage of the pipeline.

The pipeline is:  diff -> symbols -> call graph -> ChangeUnit -> prompt -> Finding
"""

from __future__ import annotations

from dataclasses import dataclass, field

SEVERITY_ORDER = ["info", "low", "medium", "high", "critical"]


def severity_rank(sev: str) -> int:
    try:
        return SEVERITY_ORDER.index(sev.lower())
    except ValueError:
        return 0


# --------------------------------------------------------------------------
# diff
# --------------------------------------------------------------------------


@dataclass
class Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    header: str
    lines: list[str] = field(default_factory=list)  # raw, with +/-/' ' prefix

    def text(self) -> str:
        return "\n".join([self.header, *self.lines])


@dataclass
class FileDiff:
    path: str
    old_path: str | None = None
    status: str = "M"  # A added, M modified, D deleted, R renamed, F full-file review
    binary: bool = False
    hunks: list[Hunk] = field(default_factory=list)
    new_lines: set[int] = field(default_factory=set)  # touched lines, new-side
    old_lines: set[int] = field(default_factory=set)  # touched lines, old-side

    @property
    def is_python(self) -> bool:
        return self.path.endswith(".py")

    def text(self) -> str:
        head = f"--- a/{self.old_path or self.path}\n+++ b/{self.path}"
        return "\n".join([head, *(h.text() for h in self.hunks)])


# --------------------------------------------------------------------------
# symbols
# --------------------------------------------------------------------------


@dataclass
class Symbol:
    module: str
    qualname: str  # "Class.method" within the module
    kind: str  # function | method | class
    path: str  # repo-relative
    lineno: int  # 'def' line
    end_lineno: int
    start_lineno: int  # first decorator line (source slice starts here)
    signature: str  # normalized, e.g. "def f(a, b=1) -> int"
    is_async: bool = False
    decorators: tuple[str, ...] = ()
    parent: str | None = None  # enclosing qualname
    bases: tuple[str, ...] = ()  # raw base-class names, classes only

    @property
    def fqname(self) -> str:
        return f"{self.module}.{self.qualname}"

    @property
    def short_name(self) -> str:
        return self.qualname.rsplit(".", 1)[-1]

    def contains(self, line: int) -> bool:
        return self.start_lineno <= line <= self.end_lineno

    @property
    def span(self) -> int:
        return self.end_lineno - self.start_lineno + 1


# --------------------------------------------------------------------------
# call graph
# --------------------------------------------------------------------------


@dataclass
class CallSite:
    path: str
    line: int
    caller: str  # fqname of enclosing function, or "<module>"
    target: str  # best-effort fqname of callee
    raw: str  # dotted expression as written, e.g. "utils.calc"
    resolution: str  # import | module | self | typed | local | name | attr | ambiguous
    text: str = ""  # the source line

    @property
    def confidence(self) -> float:
        return {
            "import": 1.0,
            "module": 0.95,
            "self": 0.9,
            "typed": 0.9,  # receiver's class proven by a local binding/annotation
            "local": 0.85,
            "name": 0.6,
            "attr": 0.55,  # unique method match on an untyped receiver
            "ambiguous": 0.35,
        }.get(self.resolution, 0.3)


# --------------------------------------------------------------------------
# signature / contract analysis
# --------------------------------------------------------------------------


@dataclass
class Param:
    name: str
    kind: str  # posonly | arg | vararg | kwonly | kwarg
    annotation: str | None
    has_default: bool
    default: str | None = None


@dataclass
class SignatureDelta:
    kind: str  # added | removed | signature_changed | body_only | full_review
    breaking: bool = False
    details: list[str] = field(default_factory=list)
    old_signature: str | None = None
    new_signature: str | None = None
    # Behaviour contract: changes callers feel even though the signature text
    # didn't move — raise set, return shape, function<->generator.
    behavior: list[str] = field(default_factory=list)
    went_async: bool = False  # sync def became async def (structured, not string-matched)

    @property
    def is_contract_change(self) -> bool:
        return (
            self.kind in ("signature_changed", "removed")
            or (self.kind == "added" and self.breaking)
            or bool(self.behavior)
        )


# --------------------------------------------------------------------------
# the review packet
# --------------------------------------------------------------------------


@dataclass
class ImpactSite:
    """A place that calls the changed symbol, with just enough source to judge it."""

    site: CallSite
    snippet: str
    enclosing_signature: str | None = None
    is_test: bool = False
    hop: int = 1  # 2 = caller-of-caller, reached through a *args/**kwargs wrapper
    via: str | None = None  # the wrapper fqname a 2-hop site was reached through


@dataclass
class ChangeUnit:
    """One self-contained review packet: the change plus its blast radius."""

    path: str
    symbol: Symbol | None  # None for module-level changes
    delta: SignatureDelta
    diff_text: str
    new_source: str
    old_source: str | None = None
    callers: list[ImpactSite] = field(default_factory=list)
    callees: list[Symbol] = field(default_factory=list)
    overrides: list[Symbol] = field(default_factory=list)  # subclass overrides
    removed_fields: list[str] = field(default_factory=list)  # field-contract units
    dropped_callers: int = 0
    total_callers: int = 0
    notes: list[str] = field(default_factory=list)
    tokens: int = 0

    @property
    def title(self) -> str:
        if self.symbol:
            return f"{self.path}:{self.symbol.qualname}"
        return f"{self.path} (module level)"


# --------------------------------------------------------------------------
# findings
# --------------------------------------------------------------------------


@dataclass
class Finding:
    file: str
    line: int | None
    symbol: str | None
    severity: str
    category: str
    title: str
    detail: str
    confidence: float = 0.5
    suggestion: str | None = None

    def key(self) -> tuple:
        return (self.file, self.line, self.title.strip().lower())

    def to_dict(self) -> dict:
        return {
            "file": self.file,
            "line": self.line,
            "symbol": self.symbol,
            "severity": self.severity,
            "category": self.category,
            "title": self.title,
            "detail": self.detail,
            "confidence": self.confidence,
            "suggestion": self.suggestion,
        }
