"""Small, read-only backend convention check for a proposed git diff."""

from __future__ import annotations

import os
import re
from pathlib import Path


_SKIP_DIRS = {".git", ".gradle", ".idea", ".mvn", "build", "dist", "node_modules", "target", "vendor"}
_BUILD_NAMES = {"build.gradle", "build.gradle.kts", "pom.xml"}
_SOURCE_SUFFIXES = {".java", ".kt"}
_MAX_FILES = 800
_MAX_DIRS = 200
_MAX_DEPTH = 7
_MAX_BYTES = 128 * 1024
_MAX_EVIDENCE = 6
_MAX_DIFF_LINES = 20_000
_MAX_DIFF_CHARS = 2_000_000
_MAX_FINDINGS = 20

_JPA_BUILD = re.compile(r"spring-boot-starter-data-jpa|hibernate-(?:core|entitymanager)|jakarta\.persistence|javax\.persistence", re.I)
_QUERYDSL_BUILD = re.compile(r"querydsl-(?:jpa|apt|core)|com\.querydsl", re.I)
_JPA_SOURCE = re.compile(r"(?:\bimport\s+(?:jakarta|javax)\.persistence\.|\bimport\s+org\.springframework\.data\.jpa\.|@Entity\b|\bJpaRepository\b)")
_QUERYDSL_SOURCE = re.compile(r"\b(?:import\s+com\.querydsl\.|\bJPAQueryFactory\b|\bQuerydslPredicateExecutor\b)")
_DIRECT_JDBC = re.compile(
    r"\b(?:JdbcTemplate|NamedParameterJdbcTemplate|DriverManager|PreparedStatement|CallableStatement|"
    r"java\.sql\.|javax\.sql\.DataSource|\bDataSource\b|\.getConnection\s*\()"
)
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def _read_small_text(path: Path) -> str | None:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > _MAX_BYTES:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return None


def _evidence(workspace: Path) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {"jpa": [], "querydsl": []}
    scanned = 0
    visited_dirs = 0
    root = workspace.resolve()
    if not root.is_dir():
        return found

    for current, dirs, files in os.walk(root, followlinks=False):
        visited_dirs += 1
        if visited_dirs > _MAX_DIRS:
            return found
        relative_dir = Path(current).relative_to(root)
        depth = len(relative_dir.parts) if relative_dir != Path(".") else 0
        dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS and depth < _MAX_DEPTH and not (Path(current) / d).is_symlink())
        for name in sorted(files):
            path = Path(current) / name
            if name not in _BUILD_NAMES and path.suffix not in _SOURCE_SUFFIXES:
                continue
            scanned += 1
            if scanned > _MAX_FILES:
                return found
            body = _read_small_text(path)
            if body is None:
                continue
            relative = path.relative_to(root).as_posix()
            patterns = (_JPA_BUILD, _QUERYDSL_BUILD) if name in _BUILD_NAMES else (_JPA_SOURCE, _QUERYDSL_SOURCE)
            for key, pattern in zip(("jpa", "querydsl"), patterns):
                if len(found[key]) < _MAX_EVIDENCE and pattern.search(body):
                    found[key].append(relative)
    return found


def _added_jdbc_lines(diff_text: str) -> list[dict[str, object]]:
    findings: list[dict[str, object]] = []
    path: str | None = None
    new_line: int | None = None
    in_block_comment = False
    for raw in diff_text[:_MAX_DIFF_CHARS].splitlines()[:_MAX_DIFF_LINES]:
        if raw.startswith("diff --git "):
            path = None
            new_line = None
            in_block_comment = False
        elif raw.startswith("+++ "):
            candidate = raw[4:]
            path = candidate[2:] if candidate.startswith("b/") else None
            if path and Path(path).suffix not in _SOURCE_SUFFIXES:
                path = None
            # The quality collector represents untracked files without a hunk.
            new_line = 1 if path is not None else None
        elif match := _HUNK.match(raw):
            new_line = int(match.group(1))
        elif raw.startswith("+") and not raw.startswith("+++"):
            if path is not None and new_line is not None:
                code = raw[1:]
                # Comments are prose, not newly introduced JDBC use.
                if in_block_comment:
                    if "*/" in code:
                        code = code.split("*/", 1)[1]
                        in_block_comment = False
                    else:
                        code = ""
                if "/*" in code:
                    before, after = code.split("/*", 1)
                    code = before + (after.split("*/", 1)[1] if "*/" in after else "")
                    in_block_comment = "*/" not in after
                code = code.split("//", 1)[0]
                if _DIRECT_JDBC.search(code):
                    findings.append({
                        "severity": "warning",
                        "message": "새 JDBC 직접 접근 코드가 확인되었습니다. 기존 JPA/QueryDSL 사용 관례와 맞는지 확인하세요.",
                        "evidence": {"path": path, "line": new_line, "text": code.strip()[:160]},
                    })
                    if len(findings) >= _MAX_FINDINGS:
                        return findings
            if new_line is not None:
                new_line += 1
        elif raw.startswith(" ") and new_line is not None:
            new_line += 1
        # Removed lines do not advance the new-file line number.
    return findings


def inspect_backend(workspace: Path, diff_text: str, allow_jdbc: bool = False) -> dict[str, object]:
    """Inspect bounded repository evidence and added lines in a unified git diff.

    The convention is established only when both JPA and QueryDSL have evidence.
    ``allow_jdbc`` explicitly exempts the diff from the direct JDBC warning.
    """
    evidence = _evidence(Path(workspace))
    established = bool(evidence["jpa"] and evidence["querydsl"])
    return {
        "status": "jpa_querydsl" if established else "not_applicable",
        "evidence": evidence,
        "guidance": [
            "단순 CRUD는 JPA Repository를 우선 사용하세요.",
            "동적 조건과 복잡한 조회는 QueryDSL을 사용하세요.",
        ] if established else [],
        "findings": _added_jdbc_lines(diff_text) if established and not allow_jdbc else [],
    }
