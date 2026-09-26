import json

from jev_router.quality_frontend import inspect_frontend


def _diff(path: str, added: str) -> str:
    return (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n+++ b/{path}\n"
        f"@@ -0,0 +1,{len(added.splitlines())} @@\n"
        + "".join(f"+{line}\n" for line in added.splitlines())
    )


def test_repo_evidence_and_visual_review_are_distinct(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"dependencies": {"next": "*", "react": "*"}}))
    components = tmp_path / "app" / "components"
    components.mkdir(parents=True)
    (components / "Existing.tsx").write_text("export const Existing = () => <div style={{color: theme.colors.text}} />")
    diff = _diff("app/page.tsx", "export default function Page() { return <main>Hello</main> }")

    result = inspect_frontend(tmp_path, diff)

    assert result["framework"] == "Next.js"
    assert result["review_required"] is True
    assert result["findings"] == []
    assert any("token" in item.lower() for item in result["guidance"])
    assert any("loading" in item for item in result["visual_review_checklist"])
    assert json.loads(json.dumps(result)) == result


def test_added_literals_and_dom_access_are_advisory_when_convention_exists(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"dependencies": {"react": "*"}}))
    components = tmp_path / "src" / "components"
    components.mkdir(parents=True)
    (components / "Button.tsx").write_text("export const Button = () => <b style={{color: theme.colors.text}} />")
    diff = _diff(
        "src/components/Card.tsx",
        "const node = document.querySelector('#root');\n"
        "export const Card = () => <div style={{ color: '#ffffff', padding: '12px' }} />",
    )

    result = inspect_frontend(tmp_path, diff)

    assert len(result["findings"]) == 2
    assert {finding["severity"] for finding in result["findings"]} == {"review"}
    assert any("Card.tsx:1" in finding["evidence"] for finding in result["findings"])
    assert any("Card.tsx:2" in finding["evidence"] for finding in result["findings"])


def test_no_token_convention_does_not_flag_new_style_literal(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"dependencies": {"vue": "*"}}))
    result = inspect_frontend(tmp_path, _diff("src/App.vue", "<style>.card { color: #fff; }</style>"))
    assert result["framework"] == "Vue"
    assert result["findings"] == []
    assert result["review_required"] is True


def test_backend_change_has_no_visual_review_and_ignores_path_escape(tmp_path):
    diff = _diff("service.py", "print('hello')") + _diff("../outside.tsx", "const x = document.querySelector('body')")
    result = inspect_frontend(tmp_path, diff)
    assert result["review_required"] is False
    assert result["visual_review_checklist"] == []
    assert result["findings"] == []


def test_deleted_screen_content_still_needs_visual_review(tmp_path):
    diff = (
        "diff --git a/src/screens/Home.js b/src/screens/Home.js\n"
        "--- a/src/screens/Home.js\n+++ b/src/screens/Home.js\n"
        "@@ -1 +0,0 @@\n-old content\n"
    )
    result = inspect_frontend(tmp_path, diff)
    assert result["review_required"] is True
    assert "Home.js" in result["visual_review_checklist"][0]


def test_prework_uses_nearby_next_screen_and_installed_design_system(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({
        "dependencies": {"next": "*", "@radix-ui/themes": "*"},
    }))
    route = tmp_path / "app" / "settings"
    route.mkdir(parents=True)
    (route / "loading.tsx").write_text("export default function Loading() { return <div>Loading</div> }")
    (route / "old.tsx").write_text("const isEmpty = items.length === 0; export default function Old() { return <main /> }")
    (route / "page.tsx").write_text("export default function Page() { return <main /> }")

    result = inspect_frontend(tmp_path, "", focus_paths=["app/settings/page.tsx"])

    assert result["framework"] == "Next.js"
    assert result["review_required"] is False
    assert result["visual_review_checklist"]
    assert result["findings"] == []
    assert any("Radix Themes" in item for item in result["evidence"])
    assert any("old.tsx" in item for item in result["evidence"])
    assert any("empty" in item for item in result["evidence"])
    assert any("installed UI library" in item for item in result["guidance"])


def test_expo_screen_review_targets_devices_lists_and_media(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({
        "dependencies": {"expo": "*", "react-native-paper": "*"},
    }))
    added = (
        "import { ScrollView, Image, Text } from 'react-native';\n"
        "export default function Feed() { return <ScrollView>{items.map(item => "
        "<Text key={item.id}>{item.title}</Text>)}<Image source={hero} /></ScrollView> }"
    )

    result = inspect_frontend(tmp_path, _diff("app/feed.tsx", added))

    assert result["framework"] == "Expo / React Native"
    assert result["findings"] == []
    assert any("platform differences" in item for item in result["visual_review_checklist"])
    assert any("text scaling" in item for item in result["visual_review_checklist"])
    assert any("virtualization" in item for item in result["visual_review_checklist"])
    assert any("image sizing" in item for item in result["visual_review_checklist"])


def test_focus_path_escape_does_not_trigger_ui_prework(tmp_path):
    result = inspect_frontend(tmp_path, "", focus_paths=["../outside.tsx"])
    assert result["review_required"] is False
    assert result["visual_review_checklist"] == []


def test_focused_screen_directory_samples_immediate_ui_files_for_prework(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"dependencies": {"expo": "*"}}))
    screens = tmp_path / "src" / "screens"
    screens.mkdir(parents=True)
    (screens / "Home.tsx").write_text("export const Home = () => <Text style={{color: theme.colors.text}} />")
    (screens / "Settings.tsx").write_text("export const Settings = () => <Text>Settings</Text>")
    nested = screens / "nested"
    nested.mkdir()
    (nested / "Hidden.tsx").write_text("export const Hidden = () => <Text>Hidden</Text>")

    result = inspect_frontend(tmp_path, "", focus_paths=["src/screens"])

    assert result["framework"] == "Expo / React Native"
    assert result["review_required"] is False
    assert any("Focused UI files: src/screens/Home.tsx" in item for item in result["evidence"])
    assert any("Settings.tsx" in item for item in result["evidence"])
    assert all("Hidden.tsx" not in item for item in result["evidence"])
    assert any("theme" in item.lower() or "token" in item.lower() for item in result["guidance"])
    assert any("small and large devices" in item for item in result["visual_review_checklist"])
