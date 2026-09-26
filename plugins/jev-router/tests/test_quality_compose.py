import json

from jev_router.quality_compose import inspect_compose


def _diff(path: str, added: str) -> str:
    lines = added.splitlines()
    return (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n+++ b/{path}\n"
        f"@@ -0,0 +1,{len(lines)} @@\n"
        + "".join(f"+{line}\n" for line in lines)
    )


def test_shared_screen_uses_bounded_conventions_and_requires_visual_review(tmp_path):
    source = tmp_path / "shared/src/commonMain/kotlin/app"
    theme = source / "theme"
    theme.mkdir(parents=True)
    (theme / "AppTheme.kt").write_text("val action = AppTheme.colors.primary\n")
    components = source / "components"
    components.mkdir()
    (components / "AppButton.kt").write_text("@Composable fun AppButton() {}")
    screen = source / "HomeScreen.kt"
    screen.write_text("@Composable fun HomeScreen() { Text(\"Hi\") }")
    name = "shared/src/commonMain/kotlin/app/HomeScreen.kt"
    result = inspect_compose(tmp_path, _diff(name, screen.read_text()))

    assert result["framework"] == "Compose Multiplatform"
    assert result["review_required"] is True
    assert any("theme" in item.lower() for item in result["guidance"])
    assert any("components" in item.lower() for item in result["evidence"])
    assert any("Android and iOS" in item for item in result["visual_review_checklist"])
    assert json.loads(json.dumps(result)) == result


def test_advisory_findings_are_changed_line_only(tmp_path):
    source = tmp_path / "shared/src/commonMain/kotlin/app"
    theme = source / "theme"
    theme.mkdir(parents=True)
    (theme / "Theme.kt").write_text("val foreground = AppTheme.colors.foreground")
    name = "shared/src/commonMain/kotlin/app/ProfileScreen.kt"
    added = (
        "import android.content.Context\n"
        "@Composable fun ProfileScreen() {\n"
        "  Thread.sleep(100)\n"
        "  Box(Modifier.padding(12.dp).background(Color(0xFF123456)))\n"
        "}"
    )
    result = inspect_compose(tmp_path, _diff(name, added))

    assert len(result["findings"]) == 3
    assert [f["evidence"].split(":")[1] for f in result["findings"]] == ["1", "3", "4"]
    assert all(f["severity"] == "review" for f in result["findings"])


def test_no_token_convention_does_not_flag_literal_and_non_ui_is_ignored(tmp_path):
    result = inspect_compose(
        tmp_path,
        _diff("app/src/androidMain/kotlin/HomeScreen.kt", "@Composable fun HomeScreen() { Box(Modifier.padding(8.dp)) }")
        + _diff("app/src/commonMain/kotlin/Repository.kt", "class Repository"),
    )
    assert result["framework"] == "Jetpack Compose"
    assert result["findings"] == []
    assert result["review_required"] is True


def test_deleted_screen_still_requires_review_and_path_escape_is_ignored(tmp_path):
    deletion = (
        "diff --git a/shared/src/commonMain/kotlin/HomeScreen.kt b/shared/src/commonMain/kotlin/HomeScreen.kt\n"
        "--- a/shared/src/commonMain/kotlin/HomeScreen.kt\n"
        "+++ b/shared/src/commonMain/kotlin/HomeScreen.kt\n"
        "@@ -1 +0,0 @@\n"
        "-@Composable fun HomeScreen() {}\n"
    )
    result = inspect_compose(tmp_path, deletion + _diff("../outside/src/commonMain/kotlin/EvilScreen.kt", "@Composable fun EvilScreen() {}"))
    assert result["review_required"] is True
    assert "HomeScreen.kt" in result["visual_review_checklist"][0]
    assert "EvilScreen" not in str(result)


def test_plain_kotlin_diff_needs_no_visual_review(tmp_path):
    result = inspect_compose(tmp_path, _diff("shared/src/commonMain/kotlin/Repository.kt", "class Repository"))
    assert result["framework"] is None
    assert result["review_required"] is False
    assert result["visual_review_checklist"] == []


def test_focused_ui_directory_informs_prework_without_diff_review(tmp_path):
    ui = tmp_path / "shared/src/commonMain/kotlin/app/ui"
    ui.mkdir(parents=True)
    (ui / "HomeScreen.kt").write_text("@Composable fun HomeScreen() { Text(AppTheme.colors.text) }")
    (ui / "Repository.kt").write_text("class Repository")
    (ui / "Nested").mkdir()
    (ui / "Nested" / "HiddenScreen.kt").write_text("@Composable fun HiddenScreen() {}")
    result = inspect_compose(tmp_path, "", ["shared/src/commonMain/kotlin/app/ui"])

    assert result["framework"] == "Compose Multiplatform"
    assert any("HomeScreen.kt" in item for item in result["evidence"])
    assert "HiddenScreen.kt" not in str(result)
    assert result["guidance"]
    assert result["review_required"] is False
    assert result["visual_review_checklist"] == []


def test_rounded_shared_button_and_square_direct_button_need_review(tmp_path):
    app = tmp_path / "shared/src/commonMain/kotlin/app"
    components = app / "components"
    components.mkdir(parents=True)
    (components / "AppButton.kt").write_text(
        "@Composable fun AppButton() { Button(shape = RoundedCornerShape(12.dp)) {} }"
    )
    name = "shared/src/commonMain/kotlin/app/ProfileScreen.kt"
    result = inspect_compose(tmp_path, _diff(name,
        "@Composable fun ProfileScreen() { Button(shape = RectangleShape) { Text(\"Save\") } }"
    ))

    assert any("AppButton.kt" in item and "rounded button" in item for item in result["evidence"])
    assert any("Direct square Button shape" in item["message"] for item in result["findings"])
    assert any("button shapes" in item.lower() for item in result["visual_review_checklist"])


def test_unrelated_shape_or_no_shared_baseline_does_not_flag_button(tmp_path):
    app = tmp_path / "shared/src/commonMain/kotlin/app"
    components = app / "components"
    components.mkdir(parents=True)
    (components / "Card.kt").write_text(
        "@Composable fun Card() { Box(Modifier.clip(RoundedCornerShape(12.dp))) }"
    )
    name = "shared/src/commonMain/kotlin/app/ProfileScreen.kt"
    result = inspect_compose(tmp_path, _diff(name,
        "@Composable fun ProfileScreen() { Button(shape = RectangleShape) {} }"
    ))
    assert not any("square Button" in item["message"] for item in result["findings"])
