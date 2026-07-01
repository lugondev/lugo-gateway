from app.services.stt.glossary import (
    build_initial_prompt,
    load_glossary_terms,
    resolve_initial_prompt,
)


def test_load_glossary_skips_comments_and_blanks(tmp_path):
    f = tmp_path / "glossary.txt"
    f.write_text(
        "# domain terms\nHejito\n\n  bật đèn  \n# another comment\ntắt quạt\n",
        encoding="utf-8",
    )
    assert load_glossary_terms(str(f)) == ["Hejito", "bật đèn", "tắt quạt"]


def test_load_glossary_dedupes_preserving_order(tmp_path):
    f = tmp_path / "g.txt"
    f.write_text("Hejito\ntắt quạt\nHejito\n", encoding="utf-8")
    assert load_glossary_terms(str(f)) == ["Hejito", "tắt quạt"]


def test_load_glossary_missing_file_returns_empty():
    assert load_glossary_terms("/no/such/file.txt") == []
    assert load_glossary_terms("") == []


def test_build_initial_prompt_appends_terms_to_base():
    prompt = build_initial_prompt("Trợ lý ảo.", ["Hejito", "bật đèn"])
    assert "Trợ lý ảo." in prompt
    assert "Hejito" in prompt
    assert "bật đèn" in prompt


def test_build_initial_prompt_empty_inputs_returns_none():
    assert build_initial_prompt("", []) is None


def test_build_initial_prompt_only_base():
    assert build_initial_prompt("Trợ lý ảo.", []) == "Trợ lý ảo."


def test_build_initial_prompt_caps_length_to_avoid_hallucination():
    terms = [f"term{i}" for i in range(500)]
    prompt = build_initial_prompt("base.", terms, max_chars=100)
    assert len(prompt) <= 100
    # keeps the base and as many leading terms as fit
    assert prompt.startswith("base.")


def test_resolve_initial_prompt_merges_base_and_glossary_file(tmp_path):
    f = tmp_path / "g.txt"
    f.write_text("Hejito\ntắt quạt\n", encoding="utf-8")
    prompt = resolve_initial_prompt("Trợ lý ảo.", str(f))
    assert "Trợ lý ảo." in prompt
    assert "Hejito" in prompt
    assert "tắt quạt" in prompt


def test_resolve_initial_prompt_no_base_no_file_is_none():
    assert resolve_initial_prompt("", "") is None
