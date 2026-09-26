"""Check scope review at the boundary between a task plan and Git changes."""

from pathlib import Path
import subprocess

import pytest

from jev_router.quality import inspect_quality


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def test_quality_check_flags_only_changes_outside_planned_paths(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "src").mkdir()
    (repo / "src/feature.py").write_text("value = 1\n")
    (repo / "README.md").write_text("original\n")
    _git(repo, "add", ".")
    _git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "init")
    (repo / "src/feature.py").write_text("value = 2\n")
    (repo / "README.md").write_text("changed\n")
    (repo / "src/new.py").write_text("other = True\n")

    monkeypatch.setattr("jev_router.quality_backend.inspect_backend", lambda *_args, **_kwargs: {"findings": []})
    monkeypatch.setattr("jev_router.quality_frontend.inspect_frontend", lambda *_args, **_kwargs: {"findings": [], "review_required": False})
    report = inspect_quality(repo, request="Change feature", review=True, allowed_paths=["src/"])

    assert report["changed_paths"] == ["README.md", "src/feature.py", "src/new.py"]
    assert [finding["evidence"] for finding in report["findings"]] == ["README.md"]
    assert report["requires_review"] is True


def test_quality_context_does_not_label_existing_changes_as_new_work(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "file.py").write_text("old = 1\n")
    _git(repo, "add", ".")
    _git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "init")
    (repo / "file.py").write_text("old = 2\n")

    monkeypatch.setattr("jev_router.quality_backend.inspect_backend", lambda *_args, **_kwargs: {"findings": []})
    monkeypatch.setattr("jev_router.quality_frontend.inspect_frontend", lambda *_args, **_kwargs: {"findings": [], "review_required": False})
    report = inspect_quality(repo, request="Plan a change")

    assert report["phase"] == "context"
    assert report["changed_paths"] == []
    assert report["requires_review"] is False


def test_quality_review_catches_untracked_jdbc_and_ui_file(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "build.gradle.kts").write_text(
        'implementation("org.springframework.boot:spring-boot-starter-data-jpa")\n'
        'implementation("com.querydsl:querydsl-jpa")\n'
    )
    (repo / "package.json").write_text('{"dependencies":{"react":"*"}}')
    _git(repo, "add", ".")
    _git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "init")
    (repo / "src").mkdir()
    (repo / "src/New.java").write_text("import java.sql.Connection;\n")
    (repo / "src/Page.tsx").write_text("export const Page = () => <main>Hello</main>;\n")

    report = inspect_quality(repo, request="Add a page and query", review=True, allowed_paths=["src/"])

    assert report["backend"]["status"] == "jpa_querydsl"
    assert any("New.java" in finding["evidence"].get("path", "") for finding in report["backend"]["findings"])
    assert report["frontend"]["review_required"] is True
    assert report["requires_review"] is True

    with pytest.raises(ValueError, match="requires a reason"):
        inspect_quality(repo, request="Add a page and query", review=True, allow_jdbc=True)


def test_quality_review_integrates_compose_visual_and_code_findings(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    source = repo / "shared/src/commonMain/kotlin/app"
    source.mkdir(parents=True)
    screen = source / "HomeScreen.kt"
    screen.write_text("@Composable fun HomeScreen() { Text(\"Hi\") }\n")
    _git(repo, "add", ".")
    _git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "init")
    screen.write_text("@Composable fun HomeScreen() { Thread.sleep(100); Text(\"Hi\") }\n")

    report = inspect_quality(repo, request="Improve shared screen", review=True, allowed_paths=["shared/"])

    assert report["compose"]["framework"] == "Compose Multiplatform"
    assert report["compose"]["review_required"] is True
    assert any("Blocking call" in finding["message"] for finding in report["findings"])
    assert report["requires_review"] is True
    assert report["acceptance"]["visual"].startswith("Requires rendered-screen")
