def language_matches(filter_code: str, voice_languages: list[str]) -> bool:
    """Bare-code <-> regional matching. Empty voice_languages = language-agnostic."""
    if not voice_languages:
        return True
    f = filter_code.strip().lower()
    for lang in voice_languages:
        lv = lang.strip().lower()
        if lv == f or lv.startswith(f + "-") or f.startswith(lv + "-"):
            return True
    return False
