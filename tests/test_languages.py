from subtranslate.languages import (
    PAGE_SIZE,
    LanguageCatalog,
    confirm_all_keyboard,
    language_keyboard,
)


def test_catalog_has_full_current_widget_snapshot():
    catalog = LanguageCatalog()
    assert len(catalog.items()) == 249
    assert catalog.get("hi") == "Hindi"
    assert catalog.get("bn") == "Bengali"
    assert catalog.get("zh-CN") == "Chinese (Simplified)"
    assert catalog.get("zh-TW") == "Chinese (Traditional)"
    assert catalog.get("pt-PT") == "Portuguese (Portugal)"
    assert catalog.get("mni-Mtei") == "Meiteilon (Manipuri)"


def test_five_per_page_navigation_and_every_language_reachable():
    catalog = LanguageCatalog()
    seen = []
    keyboard, current, pages = language_keyboard(catalog)
    assert current == 1 and pages == 50
    for page in range(pages):
        keyboard, current, total = language_keyboard(catalog, page)
        assert current == page + 1 and total == pages
        buttons = [button for row in keyboard.inline_keyboard for button in row]
        languages = [button for button in buttons if button.callback_data.startswith("lang:")]
        assert len(languages) == min(PAGE_SIZE, len(catalog.items()) - page * PAGE_SIZE)
        assert all(len(row) == 1 for row in keyboard.inline_keyboard[:len(languages)])
        callbacks = {button.callback_data for button in buttons}
        assert (f"page:{page - 1}" in callbacks) == (page > 0)
        assert (f"page:{page + 1}" in callbacks) == (page < pages - 1)
        assert f"all:confirm:{page}" in callbacks
        seen.extend(button.callback_data.removeprefix("lang:") for button in languages)
        assert all(len(button.callback_data.encode()) <= 64 for button in buttons)
    assert seen == [code for code, _ in catalog.items()]
    assert any(button.callback_data == "all:start"
               for row in confirm_all_keyboard().inline_keyboard for button in row)


def test_remote_catalog_validation_and_merge():
    catalog = LanguageCatalog()
    assert not catalog.merge_remote({"tl": {"x": "Only one"}})
    data = {f"x{i}": f"Test {i}" for i in range(25)}
    data["a/b"] = "Bad code"
    data["evil"] = "Bad\nname"
    assert catalog.merge_remote({"tl": data})
    assert catalog.get("x3") == "Test 3"
    assert catalog.get("a/b") is None
    assert catalog.get("evil") is None
    assert catalog.get("hi") == "Hindi"
