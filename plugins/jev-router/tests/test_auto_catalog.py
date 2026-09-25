from jev_router.auto_catalog import build_catalog


def test_catalog_retains_native_models_and_adds_virtual_model():
    native = {"models": [{"slug": "gpt-5.6-terra", "display_name": "Terra"}, {"slug": "gpt-6-astra", "display_name": "Astra"}]}
    official = {"models": [
        {"slug": "gpt-6-sol", "display_name": "Sol", "supported_reasoning_levels": [{"effort": "medium"}, {"effort": "high"}]},
        {"slug": "gpt-6-luna", "display_name": "Luna"},
    ]}
    catalog = build_catalog(native, official)
    rows = {row["slug"]: row for row in catalog["models"]}
    assert set(rows) == {"gpt-5.6-terra", "gpt-6-astra", "gpt-6-sol", "gpt-6-luna", "jev-auto"}
    assert rows["jev-auto"]["display_name"] == "Jev Auto"
    assert rows["jev-auto"]["supported_reasoning_levels"] == [{"effort": "medium"}, {"effort": "high"}]
    assert native["models"][0]["display_name"] == "Terra"
