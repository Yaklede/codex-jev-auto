"""Bounded, repository-aware hints for reviewing frontend diffs.

This module deliberately does not claim to measure rendered or visual quality.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
from typing import Any


_SOURCE_SUFFIXES = {".js", ".jsx", ".ts", ".tsx", ".vue", ".svelte", ".css", ".scss"}
_UI_SUFFIXES = {".jsx", ".tsx", ".vue", ".svelte", ".css", ".scss", ".html"}
_SKIP_DIRS = {"node_modules", ".git", ".next", "dist", "build", "coverage", ".expo"}
_FRAMEWORK_DEPS = (
    ("next", "Next.js"), ("expo", "Expo / React Native"),
    ("react-native", "React Native"), ("react", "React"),
    ("vue", "Vue"), ("nuxt", "Nuxt"), ("svelte", "Svelte"),
    ("@angular/core", "Angular"),
)
_TOKEN_PATTERN = re.compile(
    r"(?:var\(--[\w-]+\)|\b(?:tokens?|theme|palette|colors|spacing)\.[A-Za-z][\w.]*)"
)
_STYLE_LITERAL = re.compile(
    r"(?:#[0-9a-fA-F]{3,8}\b|\b\d+(?:\.\d+)?(?:px|rem)\b)"
)
_CSS_DECLARATION = re.compile(
    r"^\s*(?:color|background(?:-color)?|border(?:-color)?|padding(?:-[a-z]+)?|margin(?:-[a-z]+)?|gap|font-size)\s*:"
)
_DIRECT_DOM = re.compile(r"\bdocument\.(?:querySelector(?:All)?|getElementById|getElementsByClassName)\s*\(|\.innerHTML\s*=")
_SCREEN_PART = re.compile(r"(?:^|/)(?:app|pages|screens|routes|views)(?:/|$)", re.I)
_DESIGN_DEPS = {
    "@mui/material": "MUI", "@chakra-ui/react": "Chakra UI",
    "@radix-ui/themes": "Radix Themes", "antd": "Ant Design",
    "tamagui": "Tamagui", "nativewind": "NativeWind",
    "react-native-paper": "React Native Paper",
    "@shopify/restyle": "Restyle",
}
_STATE_HINTS = {
    "loading": re.compile(r"\b(?:isLoading|loading|pending|isPending)\b", re.I),
    "empty": re.compile(r"\b(?:isEmpty|emptyState|emptyMessage)\b|\.length\s*===?\s*0", re.I),
    "error": re.compile(r"\b(?:isError|error|failed)\b", re.I),
}
_LIST_RENDER = re.compile(r"\.map\s*\(")
_LARGE_MEDIA = re.compile(r"<(?:Image|img|video)\b|\b(?:resizeMode|object-fit)\b")


def _is_ui_path(name: str, lines: list[tuple[int, str]]) -> bool:
    suffix = Path(name).suffix.lower()
    return suffix in _UI_SUFFIXES or (
        suffix in {".js", ".ts"}
        and (_SCREEN_PART.search(name) is not None
             or any(re.search(r"<\s*[A-Z][\w.]*[\s/>]", source) for _, source in lines))
    )


def _focused_ui_files(workspace: Path, focus_paths: list[str]) -> list[str]:
    """Resolve focused files or a small immediate sample from focused directories."""
    found: dict[str, None] = {}
    for name in focus_paths[:30]:
        path = _within(workspace, name)
        if path is None or any(part in _SKIP_DIRS for part in path.relative_to(workspace).parts):
            continue
        if path.is_file() and _is_ui_path(name, []):
            found[path.relative_to(workspace).as_posix()] = None
        elif path.is_dir():
            try:
                entries = sorted(os.scandir(path), key=lambda entry: entry.name)[:100]
            except OSError:
                continue
            for entry in entries:
                if len(found) >= 30:
                    break
                if not entry.is_file(follow_symlinks=False):
                    continue
                relative = (path / entry.name).relative_to(workspace).as_posix()
                if _is_ui_path(relative, []):
                    found[relative] = None
        if len(found) >= 30:
            break
    return list(found)


def _within(workspace: Path, relative: str) -> Path | None:
    if not relative or relative == "/dev/null":
        return None
    candidate = (workspace / relative).resolve()
    try:
        candidate.relative_to(workspace.resolve())
    except ValueError:
        return None
    return candidate


def _changed_lines(diff_text: str) -> dict[str, list[tuple[int, str]]]:
    """Read unified diff headers; inspect additions only, with new-file line numbers."""
    changed: dict[str, list[tuple[int, str]]] = {}
    current: str | None = None
    line_number = 0
    in_hunk = False
    for line in diff_text.splitlines()[:20000]:
        if line.startswith("diff --git "):
            current, in_hunk = None, False
        elif line.startswith("+++ "):
            raw = line[4:].strip()
            current = raw[2:] if raw.startswith("b/") else None
            if current:
                changed.setdefault(current, [])
            in_hunk = False
        elif line.startswith("@@ "):
            match = re.search(r"\+(\d+)(?:,\d+)?\s@@", line)
            in_hunk = bool(current and match)
            if match:
                line_number = int(match.group(1))
        elif in_hunk and current:
            if line.startswith("+") and not line.startswith("+++"):
                changed.setdefault(current, []).append((line_number, line[1:]))
                line_number += 1
            elif line.startswith(" "):
                line_number += 1
            elif line.startswith("-") or line.startswith("\\ No newline"):
                pass
            else:
                in_hunk = False
    return changed


def _nearby_files(workspace: Path, paths: list[Path]) -> list[Path]:
    """Sample a few siblings and convention files; never walk the repository."""
    found: dict[Path, None] = {}
    directories = {workspace}
    for path in paths[:30]:
        if path.is_dir():
            directories.add(path)
        directories.add(path.parent)
        if path.parent != workspace and (
            path.parent.parent == workspace or workspace in path.parent.parent.parents
        ):
            directories.add(path.parent.parent)
    for directory in sorted(directories, key=str)[:35]:
        if not directory.is_dir() or directory.name in _SKIP_DIRS:
            continue
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)[:100]
        except OSError:
            continue
        samples = list(entries)
        for entry in entries:
            if entry.name.lower() in {"components", "styles", "theme", "tokens"} and entry.is_dir(follow_symlinks=False):
                try:
                    samples.extend(sorted(os.scandir(entry.path), key=lambda child: child.name)[:30])
                except OSError:
                    pass
        selected = 0
        for entry in samples:
            if selected >= 12:
                break
            if not entry.is_file(follow_symlinks=False):
                continue
            path = Path(entry.path)
            name = path.name.lower()
            if path.suffix.lower() in _SOURCE_SUFFIXES or name in {"package.json", "tailwind.config.js", "tailwind.config.ts"}:
                found[path] = None
                selected += 1
    return list(found)[:120]


def _read_small(path: Path, limit: int = 64_000) -> str:
    try:
        if path.stat().st_size > limit:
            return ""
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def inspect_frontend(workspace: Path, diff_text: str, focus_paths: list[str] | None = None) -> dict[str, Any]:
    """Return JSON-compatible evidence, advice, and visual review work for a diff.

    All filesystem reads are local, size limited, and restricted to changed-file
    ancestors and nearby siblings. Findings are advisory, never a visual pass.
    """
    workspace = Path(workspace).resolve()
    additions = _changed_lines(diff_text)
    safe_changes = {name: lines for name, lines in additions.items() if _within(workspace, name)}
    changed_paths = [path for name in safe_changes if (path := _within(workspace, name))]
    for name in focus_paths or []:
        path = _within(workspace, name)
        if path and path not in changed_paths:
            changed_paths.append(path)
    ui_files = [name for name, lines in safe_changes.items() if _is_ui_path(name, lines)]
    focus_ui_files = [name for name in _focused_ui_files(workspace, focus_paths or []) if name not in ui_files]
    ui_targets = ui_files + focus_ui_files
    screen_files = [name for name in ui_files if _SCREEN_PART.search(name)]
    nearby = _nearby_files(workspace, changed_paths)

    package_paths: dict[Path, None] = {}
    for path in changed_paths[:30]:
        for parent in (path.parent, *path.parents):
            if parent == workspace or workspace in parent.parents:
                package_paths[parent / "package.json"] = None
            if parent == workspace:
                break
    package_paths[workspace / "package.json"] = None
    packages: list[tuple[Path, dict[str, Any]]] = []
    for path in list(package_paths)[:40]:
        raw = _read_small(path)
        if raw:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                packages.append((path, data))

    dependencies: set[str] = set()
    evidence: list[str] = []
    for path, package in packages:
        for key in ("dependencies", "devDependencies"):
            values = package.get(key, {})
            if isinstance(values, dict):
                dependencies.update(values)
        evidence.append(f"{path.relative_to(workspace)}: package.json inspected")
    framework = next((label for dependency, label in _FRAMEWORK_DEPS if dependency in dependencies), None)
    if framework:
        evidence.append(f"Framework: {framework} dependency found")
    elif any(Path(name).suffix.lower() in {".tsx", ".jsx"} for name in ui_targets):
        framework = "React-like JSX"
        evidence.append("JSX/TSX changed files")
    elif any(Path(name).suffix.lower() == ".vue" for name in ui_targets):
        framework = "Vue"
        evidence.append("Vue single-file component changed")
    elif any(Path(name).suffix.lower() == ".svelte" for name in ui_targets):
        framework = "Svelte"
        evidence.append("Svelte component changed")

    component_dirs: set[str] = set()
    token_examples: list[str] = []
    sibling_screens: list[str] = []
    nearby_states: set[str] = set()
    changed_set = set(changed_paths)
    target_parents = {path.parent for path in changed_paths if _SCREEN_PART.search(path.relative_to(workspace).as_posix())}
    for path in nearby[:40]:
        relative = path.relative_to(workspace).as_posix()
        if "components" in path.parts:
            component_dirs.add(relative.rsplit("/", 1)[0])
        if path in changed_set:
            continue
        if path.parent in target_parents and _is_ui_path(relative, []) and len(sibling_screens) < 3:
            sibling_screens.append(relative)
        source = _read_small(path, 16_000)
        if _TOKEN_PATTERN.search(source) and len(token_examples) < 3:
            token_examples.append(relative)
        for state, pattern in _STATE_HINTS.items():
            if pattern.search(source):
                nearby_states.add(state)
    if component_dirs:
        evidence.append("Nearby component convention: " + ", ".join(sorted(component_dirs)[:3]))
    if token_examples:
        evidence.append("Existing token/theme usage: " + ", ".join(token_examples))
    design_systems = sorted({label for dep, label in _DESIGN_DEPS.items() if dep in dependencies})
    if design_systems:
        evidence.append("Installed UI library: " + ", ".join(design_systems[:3]))
    if sibling_screens:
        evidence.append("Nearby screen examples: " + ", ".join(sibling_screens))
    if focus_ui_files:
        evidence.append("Focused UI files: " + ", ".join(focus_ui_files[:4]))
    if nearby_states:
        evidence.append("Nearby state handling examples: " + ", ".join(sorted(nearby_states)))

    guidance: list[str] = []
    if component_dirs:
        guidance.append("Follow nearby component boundaries and naming for new UI pieces.")
    if token_examples:
        guidance.append("Reuse the existing design tokens or theme variables for new style values.")
    if design_systems:
        guidance.append("Use the installed UI library's existing components and theme before adding custom controls.")
    if sibling_screens:
        guidance.append("Match nearby screen layout, navigation, responsive behavior, and state presentation where relevant.")
    if nearby_states:
        guidance.append("Follow nearby loading, empty, and error state patterns where the data flow needs them.")
    if framework:
        guidance.append(f"Check {framework} state and lifecycle conventions in nearby components.")
    if ui_targets and framework in {"React", "Next.js", "React Native", "Expo / React Native", "React-like JSX"}:
        guidance.append("Review component boundaries and data flow before adding memoization; measure rendering work when performance matters.")

    findings: list[dict[str, str]] = []
    for name in ui_files:
        suffix = Path(name).suffix.lower()
        for number, source in safe_changes[name]:
            if token_examples and _STYLE_LITERAL.search(source) and (
                (suffix in {".css", ".scss"} and _CSS_DECLARATION.search(source))
                or (suffix in {".jsx", ".tsx"} and re.search(r"\bstyle\s*=\s*\{\{", source))
            ) and "var(--" not in source:
                findings.append({
                    "severity": "review",
                    "message": "New style literal may bypass the repository's design tokens.",
                    "evidence": f"{name}:{number}: {source.strip()[:120]}",
                })
            if framework in {"React", "Next.js", "React Native", "Expo / React Native", "React-like JSX"} and suffix in {".jsx", ".tsx"} and _DIRECT_DOM.search(source):
                findings.append({
                    "severity": "review",
                    "message": "Direct DOM mutation or lookup may conflict with component state; review its necessity.",
                    "evidence": f"{name}:{number}: {source.strip()[:120]}",
                })
    findings = findings[:20]

    checklist: list[str] = []
    if ui_targets:
        targets = ", ".join(screen_files[:4] or ui_targets[:4])
        checklist = [
            f"Open changed screen/component ({targets}) at narrow and wide viewport sizes.",
            "Inspect default, loading, empty, error, and success states where applicable.",
            "Check text overflow, touch targets, keyboard focus, and screen-reader labels.",
            "Compare spacing, typography, and colors with nearby screens and existing tokens.",
        ]
        if framework in {"React Native", "Expo / React Native"}:
            checklist[0] = f"Open changed screen/component ({targets}) on small and large devices, including platform differences."
            checklist[2] = "Check text scaling, safe areas, touch targets, keyboard behavior, and accessibility labels."
        added_text = "\n".join(source for name in ui_files for _, source in safe_changes[name])
        if _LIST_RENDER.search(added_text):
            checklist.append("Check list size and scrolling on a representative data set; use virtualization when the list is large.")
        if _LARGE_MEDIA.search(added_text):
            checklist.append("Check image sizing, loading behavior, and layout stability on slow connections.")
    return {
        "framework": framework,
        "evidence": evidence,
        "guidance": guidance,
        "findings": findings,
        "review_required": bool(ui_files),
        "visual_review_checklist": checklist,
    }
