# tests/test_tts_catalog_matching.py
from app.api.routers.breeze_buddy.tts_catalog.matching import language_matches


def test_exact_match():
    assert language_matches("en-IN", ["en-IN", "hi"])


def test_bare_filter_matches_regional_voice():
    assert language_matches("en", ["en-IN"])


def test_regional_filter_matches_bare_voice():
    assert language_matches("en-IN", ["en"])


def test_no_match_across_languages():
    assert not language_matches("hi", ["en-IN", "ta-IN"])


def test_empty_languages_is_language_agnostic():  # Gemini voices
    assert language_matches("hi", [])
    assert language_matches("en-IN", [])


def test_case_insensitive():
    assert language_matches("EN-in", ["en-IN"])


def test_no_prefix_false_positive():
    # "en" must not match "enx" style codes
    assert not language_matches("en", ["enx-XX"])
