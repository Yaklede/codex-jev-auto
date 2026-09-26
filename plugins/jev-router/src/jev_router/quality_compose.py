"""Bounded, advisory review hints for Compose Multiplatform screen changes.

Static source inspection cannot establish rendered quality or runtime performance.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
from typing import Any


_SKIP = {".git", ".gradle", "build", "out", "node_modules", "generated"}
_SOURCE_SETS = {"commonMain", "androidMain", "iosMain", "desktopMain", "jvmMain"}
_COMPOSE = re.compile(r"@Composable\b|\b(?:Scaffold|Column|Row|Box|LazyColumn|LazyRow|Text|Button)\s*\(")
_THEME = re.compile(r"\b(?:MaterialTheme|AppTheme|\w+Theme|\w+Tokens|\w+Colors|\w+Spacing)\.(?:colorScheme|typography|shapes|colors|spacing|\w+)")
_COLOR = re.compile(r"\bColor\s*\(\s*(?:0x[0-9a-fA-F]+|\d+\s*,)|\bColor\.(?:Red|Blue|Green|Black|White|Gray|Yellow|Magenta|Cyan)\b")
_DIMEN = re.compile(r"\b(?:padding|size|width|height|defaultMinSize|offset|spacedBy)\s*\([^\n]*?\b\d+(?:\.\d+)?\.dp\b")
_BLOCKING = re.compile(r"\b(?:Thread\.sleep|runBlocking)\s*\(")
_ANDROID_IMPORT = re.compile(r"^\s*import\s+(?:android\.|androidx\.activity\.|androidx\.fragment\.|androidx\.lifecycle\.)")
_SCREEN = re.compile(r"(?:Screen|Page|Route|View)\.kt$|/(?:screens?|pages?|routes?|ui)/", re.I)
_ROUNDED_SHAPE = re.compile(r"\bRoundedCornerShape\s*\(\s*(?!0(?:\.0+)?\.dp\b)[^)]+\)")
_SQUARE_SHAPE = re.compile(r"\b(?:RectangleShape\b|RoundedCornerShape\s*\(\s*0(?:\.0+)?\.dp\s*\))")
_BUTTON_CALL = re.compile(r"\bButton\s*\(")


def _safe(workspace: Path, name: str) -> Path | None:
    if not name or name == "/dev/null":
        return None
    path = (workspace / name).resolve()
    try:
        path.relative_to(workspace)
    except ValueError:
        return None
    return path


def _diff_changes(diff_text: str) -> dict[str, dict[str, Any]]:
    """Collect added lines and touched Kotlin paths, including deletion-only hunks."""
    changes: dict[str, dict[str, Any]] = {}
    name: str | None = None
    number = 0
    in_hunk = False
    for line in diff_text.splitlines()[:20_000]:
        if line.startswith("diff --git "):
            name, in_hunk = None, False
        elif line.startswith("+++ "):
            raw = line[4:].strip()
            name = raw[2:] if raw.startswith("b/") else None
            if name:
                changes.setdefault(name, {"added": [], "touched": True})
            in_hunk = False
        elif line.startswith("@@ "):
            match = re.search(r"\+(\d+)(?:,\d+)?\s@@", line)
            in_hunk = bool(name and match)
            if match:
                number = int(match.group(1))
        elif in_hunk and name:
            if line.startswith("+") and not line.startswith("+++"):
                changes[name]["added"].append((number, line[1:]))
                number += 1
            elif line.startswith(" "):
                number += 1
            elif not (line.startswith("-") or line.startswith("\\ No newline")):
                in_hunk = False
    return changes


def _read_small(path: Path, limit: int = 64_000) -> str:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
            return ""
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _focused_kotlin_files(workspace: Path, name: str) -> list[Path]:
    path = _safe(workspace, name)
    if path is None or any(part in _SKIP for part in path.relative_to(workspace).parts):
        return []
    if path.suffix == ".kt" and path.is_file():
        return [path]
    if not path.is_dir():
        return []
    try:
        entries = sorted(os.scandir(path), key=lambda entry: entry.name)[:100]
    except OSError:
        return []
    return [
        Path(entry.path) for entry in entries
        if entry.is_file(follow_symlinks=False) and entry.name.endswith(".kt")
    ][:20]


def _samples(workspace: Path, paths: list[Path]) -> list[Path]:
    """Inspect bounded siblings and convention directories near changed files."""
    directories: dict[Path, None] = {}
    for path in paths[:30]:
        parent = path.parent
        for _ in range(3):
            if parent == workspace or workspace in parent.parents:
                directories[parent] = None
            if parent == workspace:
                break
            parent = parent.parent
    found: dict[Path, None] = {}
    for directory in list(directories)[:60]:
        if directory.name in _SKIP or not directory.is_dir():
            continue
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)[:100]
        except OSError:
            continue
        for entry in entries:
            if len(found) >= 120:
                break
            if entry.is_file(follow_symlinks=False) and Path(entry.name).suffix == ".kt":
                found[Path(entry.path)] = None
            elif entry.name.lower() in {"theme", "designsystem", "components", "ui"} and entry.is_dir(follow_symlinks=False):
                try:
                    children = sorted(os.scandir(entry.path), key=lambda child: child.name)[:30]
                except OSError:
                    continue
                for child in children:
                    if child.is_file(follow_symlinks=False) and Path(child.name).suffix == ".kt":
                        found[Path(child.path)] = None
    return list(found)[:120]


def inspect_compose(workspace: Path, diff_text: str, focus_paths: list[str] | None = None) -> dict[str, Any]:
    """Inspect a Compose diff and return JSON-compatible, evidence-backed advice."""
    workspace = Path(workspace).resolve()
    changes = _diff_changes(diff_text)
    kotlin_changes = {
        name: item for name, item in changes.items()
        if name.endswith(".kt") and _safe(workspace, name) is not None
    }
    paths = [_safe(workspace, name) for name in kotlin_changes]
    focused_paths: list[Path] = []
    for name in (focus_paths or [])[:30]:
        for path in _focused_kotlin_files(workspace, name):
            if path not in focused_paths:
                focused_paths.append(path)
            if path not in paths:
                paths.append(path)
    paths = [path for path in paths if path is not None]
    samples = _samples(workspace, paths)
    sample_sources = [(path, _read_small(path)) for path in samples]
    changed_sources = {name: _read_small(_safe(workspace, name)) for name in kotlin_changes}
    compose_files = [
        name for name, item in kotlin_changes.items()
        if _COMPOSE.search(changed_sources[name])
        or any(_COMPOSE.search(line) for _, line in item["added"])
        or (_SCREEN.search(name) and any(part in name for part in _SOURCE_SETS))
    ]
    focus_compose = [
        path.relative_to(workspace).as_posix() for path in focused_paths
        if path.relative_to(workspace).as_posix() not in compose_files
        and _COMPOSE.search(_read_small(path))
    ]
    ui_files = (compose_files + focus_compose)[:30]
    source_sets = sorted({part for path in paths for part in path.parts if part in _SOURCE_SETS})
    ui_source_sets = {part for name in ui_files for part in Path(name).parts if part in _SOURCE_SETS}
    configured_sets: set[str] = set()
    for path in paths[:30]:
        parts = path.parts
        if "src" not in parts:
            continue
        index = parts.index("src")
        source_root = Path(*parts[:index + 1])
        if not source_root.is_dir():
            continue
        try:
            configured_sets.update(
                entry.name for entry in os.scandir(source_root)
                if entry.is_dir(follow_symlinks=False) and entry.name in _SOURCE_SETS
            )
        except OSError:
            pass
    evidence: list[str] = []
    if focus_compose:
        evidence.append("Focused Compose files: " + ", ".join(focus_compose[:4]))
    if source_sets:
        evidence.append("Changed source sets: " + ", ".join(source_sets))
    token_examples = [
        path.relative_to(workspace).as_posix() for path, source in sample_sources
        if source and _THEME.search(source)
    ][:3]
    component_examples = [
        path.relative_to(workspace).as_posix() for path, source in sample_sources
        if source and "@Composable" in source and any(p.lower() in {"components", "designsystem"} for p in path.parts)
    ][:3]
    button_examples = [
        path.relative_to(workspace).as_posix() for path, source in sample_sources
        if source and path not in paths and "button" in path.stem.lower()
        and any(part.lower() in {"components", "designsystem"} for part in path.parts)
        and _ROUNDED_SHAPE.search(source)
    ][:3]
    shape_token_examples = [
        path.relative_to(workspace).as_posix() for path, source in sample_sources
        if source and any(part.lower() in {"theme", "tokens"} for part in path.parts)
        and re.search(r"\b(?:shapes|RoundedCornerShape|CornerSize)\b", source)
    ][:3]
    if token_examples:
        evidence.append("Existing theme/token usage: " + ", ".join(token_examples))
    if component_examples:
        evidence.append("Nearby composable components: " + ", ".join(component_examples))
    if button_examples:
        evidence.append("Nearby reusable rounded button: " + ", ".join(button_examples))
    if shape_token_examples:
        evidence.append("Nearby shape tokens: " + ", ".join(shape_token_examples))
    multiplatform = bool(ui_source_sets & {"commonMain", "iosMain", "desktopMain"} or configured_sets & {"iosMain", "desktopMain"})
    framework = "Compose Multiplatform" if ui_files and multiplatform else ("Jetpack Compose" if ui_files else None)
    guidance: list[str] = []
    if ui_files:
        guidance.append("Follow nearby screen state and event callback conventions; keep rendering components focused on UI state.")
        if token_examples:
            guidance.append("Reuse existing theme and design tokens for new colors and spacing where equivalent tokens exist.")
        if component_examples:
            guidance.append("Check nearby reusable components before introducing a new control or styling variant.")
        if button_examples:
            guidance.append("Inspect the nearby rounded button before adding a directly shaped Button.")
        if shape_token_examples:
            guidance.append("Inspect nearby theme shapes before setting a control shape directly.")
        if framework == "Compose Multiplatform":
            guidance.append("Keep shared UI portable across configured targets and validate platform-specific layout and behavior.")
    findings: list[dict[str, str]] = []
    for name in ui_files:
        for number, line in kotlin_changes.get(name, {}).get("added", []):
            stripped = line.strip()
            if stripped.startswith("//") or stripped.startswith("*"):
                continue
            if button_examples and _BUTTON_CALL.search(line) and _SQUARE_SHAPE.search(line):
                findings.append({
                    "severity": "review",
                    "message": "Direct square Button shape differs from a nearby reusable rounded button; inspect the rendered controls.",
                    "evidence": f"{name}:{number}: {stripped[:100]} | nearby: {button_examples[0]}",
                })
            message: str | None = None
            if "commonMain" in Path(name).parts and _ANDROID_IMPORT.search(line):
                message = "Android-only import in commonMain may break other targets."
            elif _BLOCKING.search(line):
                message = "Blocking call in UI code may stall rendering; verify its execution context."
            elif token_examples and (_COLOR.search(line) or _DIMEN.search(line)) and not _THEME.search(line):
                message = "New literal styling may bypass an existing theme or design token; check for an equivalent token."
            if message:
                findings.append({"severity": "review", "message": message, "evidence": f"{name}:{number}: {stripped[:120]}"})
    findings = findings[:20]
    checklist: list[str] = []
    if compose_files:
        targets = ", ".join(ui_files[:4])
        checklist = [
            f"Render changed screen/component ({targets}) and compare with the active design and nearby screens.",
            "Inspect default, loading, empty, error, and success states where applicable.",
            "Check small and large layouts, text scaling, long text, accessibility labels, focus, and touch targets.",
            "Exercise interaction and scrolling; use runtime profiling for suspected jank or recomposition issues.",
            "Compare button shapes and control styling with nearby reusable composables in the rendered screen.",
        ]
        if framework == "Compose Multiplatform":
            if "iosMain" in configured_sets or "iosMain" in source_sets or "commonMain" in ui_source_sets:
                checklist.append("Review Android and iOS rendering and interaction for shared UI; include safe areas and keyboard behavior where relevant.")
            else:
                checklist.append("Review Android rendering and every configured Compose target for shared UI behavior.")
            if "desktopMain" in configured_sets or "desktopMain" in source_sets:
                checklist.append("Review desktop window resizing, pointer behavior, and keyboard navigation.")
        else:
            checklist.append("Review Android rendering on representative device sizes and accessibility settings.")
    return {
        "framework": framework,
        "evidence": evidence,
        "guidance": guidance,
        "findings": findings,
        "review_required": bool(compose_files),
        "visual_review_checklist": checklist,
    }
