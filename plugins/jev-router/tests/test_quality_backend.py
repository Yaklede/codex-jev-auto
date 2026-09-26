from pathlib import Path

from jev_router.quality_backend import inspect_backend


def _diff(*lines: str) -> str:
    return "\n".join((
        "diff --git a/src/main/java/Example.java b/src/main/java/Example.java",
        "--- a/src/main/java/Example.java",
        "+++ b/src/main/java/Example.java",
        "@@ -10,2 +10,3 @@",
        *lines,
    ))


def _profile(root: Path) -> None:
    (root / "build.gradle.kts").write_text(
        'implementation("org.springframework.boot:spring-boot-starter-data-jpa")\n'
        'implementation("com.querydsl:querydsl-jpa")\n',
        encoding="utf-8",
    )


def test_gradle_profile_guidance_and_added_jdbc_line(tmp_path: Path):
    _profile(tmp_path)
    result = inspect_backend(tmp_path, _diff(
        " import org.springframework.data.jpa.repository.JpaRepository;",
        "-    old();",
        "+    JdbcTemplate jdbcTemplate = makeTemplate();",
        "     retained();",
        "+    return jdbcTemplate.queryForList(sql);",
    ))

    assert result["status"] == "jpa_querydsl"
    assert result["evidence"] == {"jpa": ["build.gradle.kts"], "querydsl": ["build.gradle.kts"]}
    assert len(result["guidance"]) == 2
    assert len(result["findings"]) == 1
    assert result["findings"][0]["severity"] == "warning"
    assert result["findings"][0]["evidence"]["line"] == 11


def test_maven_and_source_evidence_can_establish_profile(tmp_path: Path):
    (tmp_path / "pom.xml").write_text("<artifactId>spring-boot-starter-data-jpa</artifactId>", encoding="utf-8")
    source = tmp_path / "src/main/java"
    source.mkdir(parents=True)
    (source / "SearchRepository.java").write_text("import com.querydsl.jpa.impl.JPAQueryFactory;", encoding="utf-8")

    result = inspect_backend(tmp_path, _diff("+import java.sql.Connection;"))

    assert result["status"] == "jpa_querydsl"
    assert result["evidence"]["querydsl"] == ["src/main/java/SearchRepository.java"]
    assert result["findings"][0]["evidence"]["line"] == 10


def test_unconfirmed_profile_never_flags_jdbc(tmp_path: Path):
    (tmp_path / "build.gradle").write_text("implementation 'org.springframework.boot:spring-boot-starter-data-jpa'", encoding="utf-8")
    result = inspect_backend(tmp_path, _diff("+import java.sql.Connection;"))
    assert result["status"] == "not_applicable"
    assert result["findings"] == []
    assert result["guidance"] == []


def test_only_added_source_code_is_flagged_and_exemption_works(tmp_path: Path):
    _profile(tmp_path)
    diff = _diff(
        " import java.sql.Connection;",
        "-    DriverManager.getConnection(url);",
        "+    // JdbcTemplate is intentionally mentioned in a comment",
        "+    /* DataSource is discussed here */",
        "+    DataSource source = lookup();",
    )
    assert [finding["evidence"]["line"] for finding in inspect_backend(tmp_path, diff)["findings"]] == [13]
    assert inspect_backend(tmp_path, diff, allow_jdbc=True)["findings"] == []


def test_non_source_changes_do_not_trigger(tmp_path: Path):
    _profile(tmp_path)
    diff = "\n".join((
        "diff --git a/docs/notes.md b/docs/notes.md",
        "--- a/docs/notes.md",
        "+++ b/docs/notes.md",
        "@@ -0,0 +1 @@",
        "+JdbcTemplate",
    ))
    assert inspect_backend(tmp_path, diff)["findings"] == []


def test_synthetic_untracked_file_diff_has_line_numbers(tmp_path: Path):
    _profile(tmp_path)
    diff = "\n".join((
        "diff --git a/src/New.java b/src/New.java",
        "--- /dev/null",
        "+++ b/src/New.java",
        "+package example;",
        "+import javax.sql.DataSource;",
    ))
    assert inspect_backend(tmp_path, diff)["findings"][0]["evidence"] == {
        "path": "src/New.java", "line": 2, "text": "import javax.sql.DataSource;",
    }
