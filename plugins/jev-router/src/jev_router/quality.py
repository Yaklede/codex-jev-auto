"""Repository-aware context and bounded change review for Jev Auto tasks."""

from __future__ import annotations

from fnmatch import fnmatchcase
import hashlib
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess
from typing import Any


def _git(workspace: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(workspace), *args],
        capture_output=True, check=True, timeout=20,
    ).stdout


def _relative_paths(raw: bytes) -> list[str]:
    return [name.decode("utf-8", errors="surrogateescape") for name in raw.split(b"\0") if name]


def changed_paths(workspace: Path) -> list[str]:
    """Report tracked edits and untracked files without traversing ignored directories."""
    tracked = _relative_paths(_git(workspace, "diff", "--name-only", "-z", "HEAD", "--"))
    untracked = _relative_paths(_git(workspace, "ls-files", "--others", "--exclude-standard", "-z"))
    return sorted(set(tracked + untracked))


def change_diff(workspace: Path, paths: list[str] | None = None) -> str:
    """Provide unified diff plus bounded synthetic additions for untracked text files."""
    selected = set(paths) if paths is not None else None
    if selected is not None and not selected:
        return ""
    patch = _git(workspace, "diff", "--no-ext-diff", "--no-color", "HEAD", "--", *(paths or [])).decode("utf-8", errors="replace")
    for name in _relative_paths(_git(workspace, "ls-files", "--others", "--exclude-standard", "-z")):
        if selected is not None and name not in selected:
            continue
        path = workspace / name
        if not path.is_file() or path.is_symlink() or path.stat().st_size > 256_000:
            continue
        content = path.read_bytes()
        if b"\0" in content:
            continue
        lines = content.decode("utf-8", errors="replace").splitlines()
        patch += f"\ndiff --git a/{name} b/{name}\n--- /dev/null\n+++ b/{name}\n@@ -0,0 +1,{len(lines)} @@\n"
        patch += "\n".join("+" + line for line in lines) + "\n"
    return patch


def _signature(path: Path) -> str:
    """Fingerprint a path without retaining its content in the baseline record."""
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return "missing"
    digest = hashlib.sha256()
    digest.update(str(stat.S_IFMT(metadata.st_mode)).encode())
    digest.update(str(stat.S_IMODE(metadata.st_mode)).encode())
    if stat.S_ISLNK(metadata.st_mode):
        digest.update(os.readlink(path).encode("utf-8", errors="surrogateescape"))
    elif stat.S_ISREG(metadata.st_mode):
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    else:
        digest.update(b"non-regular")
    return digest.hexdigest()


def capture_baseline(workspace: Path) -> dict[str, Any]:
    """Capture the pre-task dirty state as paths and hashes, without source text."""
    root = Path(_git(workspace, "rev-parse", "--show-toplevel").decode().strip()).resolve()
    return {
        "workspace": str(root),
        "head": _git(root, "rev-parse", "HEAD").decode().strip(),
        "signatures": {name: _signature(root / name) for name in changed_paths(root)},
    }


def _since_baseline(root: Path, baseline: dict[str, Any]) -> tuple[list[str], list[str]]:
    if baseline.get("workspace") != str(root):
        raise ValueError("Quality baseline belongs to a different workspace")
    if baseline.get("head") != _git(root, "rev-parse", "HEAD").decode().strip():
        raise ValueError("Git HEAD changed since quality baseline; capture a new baseline")
    signatures = baseline.get("signatures")
    if not isinstance(signatures, dict) or any(
        not isinstance(k, str) or not isinstance(v, str) or not k
        or PurePosixPath(k).is_absolute() or ".." in PurePosixPath(k).parts
        for k, v in signatures.items()
    ):
        raise ValueError("Invalid quality baseline")
    current = set(changed_paths(root))
    paths = sorted(name for name in current | signatures.keys()
                   if _signature(root / name) != signatures.get(name))
    preexisting_modified = [name for name in paths if name in signatures]
    return paths, preexisting_modified


def _within_scope(path: str, patterns: list[str]) -> bool:
    for pattern in patterns:
        normalized = pattern.strip().removeprefix("./")
        if not normalized or normalized.startswith("/") or ".." in Path(normalized).parts:
            continue
        if fnmatchcase(path, normalized) or path == normalized.rstrip("/") or path.startswith(normalized.rstrip("/") + "/"):
            return True
    return False


def inspect_quality(
    workspace: Path,
    *,
    request: str,
    review: bool = False,
    allowed_paths: list[str] | None = None,
    focus_paths: list[str] | None = None,
    allow_jdbc: bool = False,
    jdbc_reason: str | None = None,
    baseline: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Collect evidence; never claim semantic or visual acceptance from static checks."""
    if not workspace.is_dir():
        raise ValueError(f"Workspace does not exist: {workspace}")
    if allow_jdbc and not (jdbc_reason and jdbc_reason.strip()):
        raise ValueError("Direct JDBC exception requires a reason")
    root = Path(_git(workspace, "rev-parse", "--show-toplevel").decode().strip()).resolve()
    from .quality_backend import inspect_backend
    from .quality_frontend import inspect_frontend
    from .quality_compose import inspect_compose

    if review and baseline is not None:
        paths, preexisting_modified = _since_baseline(root, baseline)
    else:
        paths = changed_paths(root) if review else []
        preexisting_modified = []
    diff = change_diff(root, paths if baseline is not None else None) if review else ""
    backend = inspect_backend(root, diff, allow_jdbc=allow_jdbc)
    frontend = inspect_frontend(root, diff, focus_paths=focus_paths)
    compose = inspect_compose(root, diff, focus_paths=focus_paths)
    findings: list[dict[str, str]] = []
    for path in preexisting_modified:
        findings.append({
            "severity": "review",
            "message": "File had pre-task edits and changed again; inspect this file against its baseline",
            "evidence": path,
        })
    if review and paths and not allowed_paths:
        findings.append({
            "severity": "review",
            "message": "Planned changed paths were not supplied; scope cannot be checked",
            "evidence": "Pass --allowed-path for each planned path or glob",
        })
    if review and allowed_paths:
        for path in paths:
            if not _within_scope(path, allowed_paths):
                findings.append({
                    "severity": "review",
                    "message": "Changed path is outside the planned scope",
                    "evidence": path,
                })
    for section in (backend, frontend, compose):
        findings.extend(section.get("findings", []))
    visual_review_required = review and bool(frontend.get("review_required") or compose.get("review_required"))
    return {
        "workspace": str(root),
        "request": request,
        "phase": "review" if review else "context",
        "planned_scope": allowed_paths or [],
        "focus_paths": focus_paths or [],
        "jdbc_exception_reason": jdbc_reason if allow_jdbc else None,
        "changed_paths": paths,
        "preexisting_modified_paths": preexisting_modified,
        "backend": backend,
        "frontend": frontend,
        "compose": compose,
        "findings": findings,
        "requires_review": bool(findings) or visual_review_required,
        "acceptance": {
            "requirements": "Requires agent review against the user's request and acceptance criteria",
            "visual": "Requires rendered-screen inspection when UI changed" if visual_review_required else "Not asserted",
        },
    }
