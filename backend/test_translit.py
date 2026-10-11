"""Offline checks for Latin name matching. No LED or hardware imports."""

import translit


def test_cyrillic_name_becomes_latin():
    first, last, changed = translit.latinize_parts("Іван", "Петренко")
    assert changed
    assert first == "Ivan"
    assert last == "Petrenko"
    assert translit.is_latin_name(first)
    assert translit.is_latin_name(last)


def test_latin_name_is_unchanged():
    first, last, changed = translit.latinize_parts("Ivan", "Petrenko")
    assert not changed
    assert (first, last) == ("Ivan", "Petrenko")


def test_pass1_match_payload_is_latin_before_lookup():
    prepared = translit.prepare_for_match(
        {
            "found": True,
            "first_name": "Марія",
            "last_name": "Шевченко",
            "full_name": "Марія Шевченко",
            "first_name_en": "",
            "last_name_en": "",
        }
    )
    assert prepared["first_name_en"] == "Mariya"
    assert prepared["last_name_en"] == "Shevchenko"
    assert prepared["display_name"] == "Shevchenko Mariya"
    assert not translit.has_cyrillic(prepared["display_name"])


def test_existing_ai_latin_is_kept():
    prepared = translit.prepare_for_match(
        {
            "first_name": "Іван",
            "last_name": "Петренко",
            "first_name_en": "Ivan",
            "last_name_en": "Petrenko",
        }
    )
    assert prepared["display_name"] == "Petrenko Ivan"


if __name__ == "__main__":
    test_cyrillic_name_becomes_latin()
    test_latin_name_is_unchanged()
    test_pass1_match_payload_is_latin_before_lookup()
    test_existing_ai_latin_is_kept()
    print("translit tests ok")
