"""Git plumbing and a unified-diff parser.

Everything the tool knows about "what changed" enters through this module.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from .models import FileDiff, Hunk

HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")


class GitError(RuntimeError):
    pass


def run_git(args: list[str], cwd: Path | str = ".", check: bool = True) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        errors="replace",
    )
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def repo_root(start: Path | str = ".") -> Path:
    try:
        out = run_git(["rev-parse", "--show-toplevel"], cwd=start)
    except (GitError, FileNotFoundError) as exc:
        raise GitError(f"not a git repository (or git is unavailable): {exc}") from exc
    return Path(out.strip())


def rev_exists(rev: str, cwd: Path) -> bool:
    proc = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}"],
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0


def default_base(cwd: Path) -> str:
    """Best guess at the integration branch."""
    out = run_git(["symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"], cwd, check=False).strip()
    if out:
        return out.rsplit("/", 1)[-1]
    for candidate in ("main", "master", "develop"):
        if rev_exists(candidate, cwd):
            return candidate
    return "HEAD"


def merge_base(base: str, head: str, cwd: Path) -> str:
    out = run_git(["merge-base", base, head], cwd, check=False).strip()
    return out or base


def show_file(rev: str, path: str, cwd: Path) -> str | None:
    """Content of `path` at `rev`, or None if it did not exist there."""
    proc = subprocess.run(
        ["git", "show", f"{rev}:{path}"],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        errors="replace",
    )
    if proc.returncode != 0:
        return None
    return proc.stdout


# --------------------------------------------------------------------------
# diff acquisition
# --------------------------------------------------------------------------

_DIFF_FLAGS = ["--no-color", "--no-ext-diff", "-U3", "--find-renames"]


def diff_working(cwd: Path, staged_only: bool = False) -> tuple[str, str]:
    """Uncommitted work. Returns (diff_text, base_rev)."""
    args = ["diff", *_DIFF_FLAGS]
    if staged_only:
        args.append("--cached")
    args.append("HEAD")
    return run_git(args, cwd), "HEAD"


def diff_base(base: str, cwd: Path, include_working: bool = True) -> tuple[str, str]:
    """Everything on this branch since it forked from `base`."""
    mb = merge_base(base, "HEAD", cwd)
    target = [] if include_working else ["HEAD"]
    return run_git(["diff", *_DIFF_FLAGS, mb, *target], cwd), mb


def diff_range(spec: str, cwd: Path) -> tuple[str, str]:
    if "..." in spec:
        left, right = spec.split("...", 1)
        base = merge_base(left, right or "HEAD", cwd)
    elif ".." in spec:
        base, right = spec.split("..", 1)
    else:
        base, right = f"{spec}^", spec
    return run_git(["diff", *_DIFF_FLAGS, base, right or "HEAD"], cwd), base


def diff_pr(number: int, cwd: Path) -> tuple[str, str]:
    """Fetch a GitHub PR diff via the gh CLI."""
    proc = subprocess.run(
        ["gh", "pr", "diff", str(number)],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        errors="replace",
    )
    if proc.returncode != 0:
        raise GitError(
            f"could not fetch PR #{number} via gh: {proc.stderr.strip() or 'gh not available'}"
        )
    base = _pr_base_rev(number, cwd)
    return proc.stdout, base


def _pr_base_rev(number: int, cwd: Path) -> str:
    proc = subprocess.run(
        ["gh", "pr", "view", str(number), "--json", "baseRefName,baseRefOid"],
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )
    if proc.returncode == 0:
        import json

        try:
            data = json.loads(proc.stdout)
            oid = data.get("baseRefOid")
            if oid and rev_exists(oid, cwd):
                return oid
            ref = data.get("baseRefName")
            if ref and rev_exists(ref, cwd):
                return ref
        except (json.JSONDecodeError, KeyError):
            pass
    return "HEAD"


def co_change_counts(cwd: Path, max_commits: int = 400) -> dict[str, dict[str, int]]:
    """path -> {other_path: times committed together}, mined from recent history.

    Files that historically change together are review context: if pricing.py
    usually ships with order.py and this diff only touches pricing.py, that
    absence is worth a note. One git call, built lazily, cheap to skip.
    """
    out = run_git(
        ["log", f"-n{max_commits}", "--name-only", "--pretty=format:%x00"],
        cwd,
        check=False,
    )
    counts: dict[str, dict[str, int]] = {}
    for block in out.split("\x00"):
        files = [f for f in block.strip().splitlines() if f.endswith(".py")]
        if not (2 <= len(files) <= 30):  # skip huge sweeping commits — no signal
            continue
        for a in files:
            row = counts.setdefault(a, {})
            for b in files:
                if a != b:
                    row[b] = row.get(b, 0) + 1
    return counts


def untracked_python_files(cwd: Path) -> list[str]:
    out = run_git(["ls-files", "--others", "--exclude-standard"], cwd, check=False)
    return [line for line in out.splitlines() if line.endswith(".py")]


def synthesize_full_review(paths: list[str], root: Path) -> list[FileDiff]:
    """Whole-file mode: no diff, review the code as it stands."""
    diffs: list[FileDiff] = []
    for rel in paths:
        abs_path = root / rel
        try:
            text = abs_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        n = len(text.splitlines())
        diffs.append(
            FileDiff(
                path=rel,
                old_path=None,
                status="F",
                new_lines=set(range(1, n + 1)),
            )
        )
    return diffs


# --------------------------------------------------------------------------
# unified diff parser
# --------------------------------------------------------------------------


def _strip_prefix(raw: str) -> str | None:
    raw = raw.strip()
    if raw.startswith(("a/", "b/")):
        raw = raw[2:]
    if raw in ("/dev/null", "dev/null"):
        return None
    # git quotes paths containing unusual characters
    if raw.startswith('"') and raw.endswith('"'):
        raw = raw[1:-1].encode().decode("unicode_escape")
    return raw


def parse_unified_diff(text: str) -> list[FileDiff]:
    files: list[FileDiff] = []
    current: FileDiff | None = None
    lines = text.splitlines()
    i = 0

    while i < len(lines):
        line = lines[i]

        if line.startswith("diff --git "):
            current = FileDiff(path="", status="M")
            files.append(current)
            i += 1
            continue

        if current is None:
            i += 1
            continue

        if line.startswith("new file mode"):
            current.status = "A"
        elif line.startswith("deleted file mode"):
            current.status = "D"
        elif line.startswith("rename from "):
            current.old_path = _strip_prefix(line[len("rename from ") :])
            current.status = "R"
        elif line.startswith("rename to "):
            current.path = _strip_prefix(line[len("rename to ") :]) or current.path
        elif line.startswith("Binary files") or line.startswith("GIT binary patch"):
            current.binary = True
        elif line.startswith("--- "):
            old = _strip_prefix(line[4:])
            if old:
                current.old_path = old
        elif line.startswith("+++ "):
            new = _strip_prefix(line[4:])
            if new:
                current.path = new
        elif (m := HUNK_RE.match(line)) is not None:
            hunk = Hunk(
                old_start=int(m.group(1)),
                old_count=int(m.group(2) or 1),
                new_start=int(m.group(3)),
                new_count=int(m.group(4) or 1),
                header=line,
            )
            i += 1
            old_no, new_no = hunk.old_start, hunk.new_start
            while i < len(lines):
                body = lines[i]
                if body.startswith(("diff --git ", "@@ ")):
                    break
                if body and body[0] not in " +-\\":
                    break
                hunk.lines.append(body)
                if body.startswith("+"):
                    current.new_lines.add(new_no)
                    new_no += 1
                elif body.startswith("-"):
                    current.old_lines.add(old_no)
                    old_no += 1
                elif body.startswith("\\"):
                    pass  # "\ No newline at end of file"
                else:
                    old_no += 1
                    new_no += 1
                i += 1
            current.hunks.append(hunk)
            continue

        i += 1

    # A pure deletion has no new-side path; fall back to the old one.
    for fd in files:
        if not fd.path and fd.old_path:
            fd.path = fd.old_path
    return [fd for fd in files if fd.path]
