import pytest

from skyctl.richtext import (
    PostTooLongError,
    detect_facets,
    grapheme_len,
    split_thread,
    validate_length,
)


def test_grapheme_len_counts_emoji_sequences_as_one():
    assert grapheme_len("hello") == 5
    assert grapheme_len("👨‍👩‍👧") == 1      # family joined with ZWJ
    assert grapheme_len("é") == 1     # e + combining accent
    assert grapheme_len("🇨🇦") == 1         # flag


def test_validate_length_accepts_300_and_rejects_301():
    validate_length("a" * 300)
    with pytest.raises(PostTooLongError, match="301"):
        validate_length("a" * 301)


def test_detect_facets_finds_link_mention_and_tag():
    facets = detect_facets("hi @bsky.app see https://example.com/a?b=1. #test")
    assert [(f.kind, f.value) for f in facets] == [
        ("mention", "bsky.app"),
        ("link", "https://example.com/a?b=1"),   # trailing full stop dropped
        ("tag", "test"),
    ]


def test_facet_offsets_are_utf8_bytes():
    text = "café ☕ https://example.com"
    (facet,) = detect_facets(text)
    raw = text.encode("utf-8")
    assert raw[facet.byte_start:facet.byte_end].decode("utf-8") == "https://example.com"


def test_detect_facets_ignores_non_facets():
    assert detect_facets("email me@example.com, item #1, a # alone") == []


def test_fragment_in_url_is_not_a_hashtag():
    facets = detect_facets("https://example.com/page#section")
    assert [f.kind for f in facets] == ["link"]


def test_split_thread_respects_limit_and_keeps_all_words():
    text = " ".join(f"word{i}" for i in range(200))
    parts = split_thread(text)
    assert len(parts) > 1
    assert all(grapheme_len(p) <= 300 for p in parts)
    assert " ".join(parts) == text


def test_split_thread_hard_splits_a_very_long_word():
    parts = split_thread("x" * 650)
    assert [len(p) for p in parts] == [300, 300, 50]


def test_short_text_is_a_single_part():
    assert split_thread("just one post") == ["just one post"]
