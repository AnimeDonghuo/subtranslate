import string

from subtranslate.languages import (
    LanguageCatalog,
    group_keyboard,
    letters_keyboard,
    popular_keyboard,
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


def test_every_language_can_be_reached_and_callback_fits_telegram_limit():
    catalog = LanguageCatalog()
    seen = set()
    keyboards = [popular_keyboard(catalog), letters_keyboard(catalog)]
    for letter in string.ascii_uppercase + "#":
        keyboard, _, pages = group_keyboard(catalog, letter, 0)
        keyboards.append(keyboard)
        for page in range(1, pages):
            keyboards.append(group_keyboard(catalog, letter, page)[0])
    for keyboard in keyboards:
        for row in keyboard.inline_keyboard:
            for button in row:
                assert len(button.callback_data.encode()) <= 64
                if button.callback_data.startswith("lang:"):
                    seen.add(button.callback_data.removeprefix("lang:"))
    assert seen == {code for code, _ in catalog.items()}


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
