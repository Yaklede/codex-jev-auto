"""Repository-aware context and bounded change review for Jev Auto tasks."""

from __future__ import annotations

from fnmatch import fnmatchcase
from pathlib import Path
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


def change_diff(workspace: Path) -> str:
    """Provide unified diff plus bounded synthetic additions for untracked text files."""
    patch = _git(workspace, "diff", "--no-ext-diff", "--no-color", "HEAD", "--").decode("utf-8", errors="replace")
    for name in _relative_paths(_git(workspace, "ls-files", "--others", "--exclude-standard", "-z")):
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

    diff = change_diff(root) if review else ""
    paths = changed_paths(root) if review else []
    backend = inspect_backend(root, diff, allow_jdbc=allow_jdbc)
    frontend = inspect_frontend(root, diff, focus_paths=focus_paths)
    compose = inspect_compose(root, diff, focus_paths=focus_paths)
    findings: list[dict[str, str]] = []
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
