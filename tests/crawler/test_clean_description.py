"""Tests for clean_description (crawler/normalizer.py)."""
from crawler.normalizer import clean_description


def test_none_and_blank_give_none():
    assert clean_description(None) is None
    assert clean_description("") is None
    assert clean_description("   \n  ") is None


def test_plain_text_is_unchanged():
    assert clean_description("Two girls fall in love.") == "Two girls fall in love."


def test_br_tags_become_line_breaks():
    assert clean_description("First line.<br>Second line.") == "First line.\nSecond line."
    assert clean_description("One<br/>Two<BR />Three") == "One\nTwo\nThree"


def test_other_html_tags_are_removed_and_entities_decoded():
    assert clean_description("A <i>quiet</i> story &amp; a <b>sweet</b> one") == "A quiet story & a sweet one"


def test_spoiler_blocks_are_removed():
    assert clean_description("Safe text. ~!Big spoiler!~ More safe text.") == "Safe text.  More safe text."


def test_trailing_source_note_is_removed():
    assert clean_description("A nice story.\n\n(Source: Studio Example)") == "A nice story."
    assert clean_description("A nice story. [Written by MAL Rewrite]") == "A nice story."


def test_a_source_word_inside_the_text_is_kept():
    text = "The source of her trouble is a letter."
    assert clean_description(text) == text


def test_long_runs_of_blank_lines_are_squeezed():
    assert clean_description("One.\n\n\n\n\nTwo.") == "One.\n\nTwo."


def test_text_that_is_only_markup_gives_none():
    assert clean_description("<br><br>") is None
    assert clean_description("~!only a spoiler!~") is None
