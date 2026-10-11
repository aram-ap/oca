"""Parse unified diffs: paths, line counts, and deterministic risk flags."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from oca.fakeco.runner import FORBIDDEN_PATCH_FILES, PATCHABLE_ROOTS
from oca.schemas import RiskFlags

MAX_FILES = 20
MAX_LINES = 2000
_HEADER = re.compile(r"^(?:---|\+\+\+) (?:(?:a|b)/)?(\S+)", re.M)
_AUTH = re.compile(r"auth|token|password|secret|credential", re.I)
_ROUTE = re.compile(r"^[+-]\s*@app\.(get|post|put|delete|patch)\(", re.M)


@dataclass
class DiffInfo:
    files: list[str] = field(default_factory=list)
    added: int = 0
    removed: int = 0
    new_files: list[str] = field(default_factory=list)
    config_changes: dict[str, str] = field(default_factory=dict)  # KEY -> new value from *.env hunks
    migration_sql: dict[str, str] = field(default_factory=dict)   # path -> added SQL


FORMAT_HINT = ("Unified diff format: '--- a/<path>' and '+++ b/<path>' headers (or '--- /dev/null' for a new file), "
               "then one or more '@@ -start,count +start,count @@' hunks whose context lines (prefixed by a space) match "
               "the current file exactly; counts are recomputed server-side, so keep hunks small. Read the file with "
               "get_code first and copy the context lines verbatim. No markdown fences.")


def config_diff(path: str, content: str, key: str, value: int | str) -> str:
    """A one-hunk unified diff that sets KEY=value in an env file (replacing the line or appending it)."""
    lines = content.split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]
    new_line = f"{key}={value}"
    for index, line in enumerate(lines):
        if re.match(rf"^\s*{re.escape(key)}\s*=", line):
            if line == new_line:
                raise ValueError(f"{key} is already {value}")
            before = lines[max(0, index - 2):index]
            after = lines[index + 1:index + 3]
            start = index + 1 - len(before)
            body = [" " + l for l in before] + ["-" + line, "+" + new_line] + [" " + l for l in after]
            count = len(before) + 1 + len(after)
            return (f"--- a/{path}\n+++ b/{path}\n@@ -{start},{count} +{start},{count} @@\n" + "\n".join(body) + "\n")
    before = lines[-2:]
    start = len(lines) + 1 - len(before)
    body = [" " + l for l in before] + ["+" + new_line]
    return (f"--- a/{path}\n+++ b/{path}\n@@ -{start},{len(before)} +{start},{len(before) + 1} @@\n" + "\n".join(body) + "\n")


def new_file_diff(path: str, content: str) -> str:
    lines = content.rstrip("\n").split("\n")
    return f"--- /dev/null\n+++ b/{path}\n@@ -0,0 +1,{len(lines)} @@\n" + "\n".join("+" + l for l in lines) + "\n"


def normalize(diff: str) -> str:
    """Strip markdown fences and stray prose around a diff; make sure it ends with a newline."""
    text = diff.replace("\r\n", "\n").strip("\n")
    lines = text.split("\n")
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    # drop leading prose before the first diff header
    for i, line in enumerate(lines):
        if line.startswith(("diff --git", "--- ", "+++ ", "Index: ")):
            lines = lines[i:]
            break
    return "\n".join(lines).rstrip("\n") + "\n"


def parse(diff: str) -> DiffInfo:
    info = DiffInfo()
    current: str | None = None
    old_is_devnull = False
    for line in diff.splitlines():
        if line.startswith("--- "):
            old_is_devnull = line.strip() == "--- /dev/null"
            continue
        if line.startswith("+++ "):
            match = _HEADER.match(line)
            current = match.group(1) if match else None
            if current and current != "/dev/null":
                if current not in info.files:
                    info.files.append(current)
                if old_is_devnull:
                    info.new_files.append(current)
            continue
        if line.startswith("+") and not line.startswith("+++"):
            info.added += 1
            if current and current.endswith(".env"):
                key, sep, value = line[1:].partition("=")
                if sep and re.fullmatch(r"[A-Z0-9_]+", key.strip()):
                    info.config_changes[key.strip()] = value.strip()
            if current and "/migrations/" in current and current.endswith(".sql"):
                info.migration_sql[current] = info.migration_sql.get(current, "") + line[1:] + "\n"
        elif line.startswith("-") and not line.startswith("---"):
            info.removed += 1
    return info


def validate(diff: str) -> DiffInfo:
    """Raise ValueError if the diff is malformed or touches forbidden paths."""
    if "@@" not in diff or "+++ " not in diff or "--- " not in diff:
        raise ValueError("Not a unified diff (expected ---/+++ headers and @@ hunks)")
    info = parse(diff)
    if not info.files:
        raise ValueError("Diff does not name any target file")
    if len(info.files) > MAX_FILES:
        raise ValueError(f"Diff touches {len(info.files)} files; limit is {MAX_FILES}")
    if info.added + info.removed > MAX_LINES:
        raise ValueError(f"Diff changes {info.added + info.removed} lines; limit is {MAX_LINES}")
    for path in info.files:
        if path.startswith("/") or ".." in path.split("/"):
            raise ValueError(f"Illegal path in diff: {path}")
        if not path.startswith(PATCHABLE_ROOTS):
            raise ValueError(f"{path} is outside the patchable roots {PATCHABLE_ROOTS}")
        if path.rsplit("/", 1)[-1] in FORBIDDEN_PATCH_FILES:
            raise ValueError(f"{path} may not be changed (offline builds depend on it)")
    return info


def risk_flags(diff: str) -> RiskFlags:
    info = parse(diff)
    files = info.files
    return RiskFlags(
        files_changed=len(files),
        lines_changed=info.added + info.removed,
        db_migration=any("/migrations/" in f or "/db/" in f for f in files),
        auth_affected=bool(_AUTH.search("\n".join(l for l in diff.splitlines() if l[:1] in "+-" and l[:3] not in ("+++", "---")))),
        public_api_affected=bool(_ROUTE.search(diff)),
        deps_changed=any(f.endswith(("requirements.txt", "Dockerfile")) for f in files),
        config_only=bool(files) and all(f.endswith(".env") for f in files),
    )
