"""Offline contracts for sweep page specs. No browser, no paid APIs."""

import tempfile
from pathlib import Path

import pytest

from jev_ultrafast.reporting import parse_page_spec, parse_pages_file


def test_spec_parses_url_only():
    assert parse_page_spec("https://x.test/a") == ("https://x.test/a", None, None)


def test_spec_parses_both_expectations():
    spec = parse_page_spec("https://x.test/pricing expect-url=/pricing expect-text=Pricing")
    assert spec == ("https://x.test/pricing", "/pricing", "Pricing")


def test_spec_flag_order_does_not_matter():
    spec = parse_page_spec("https://x.test/a expect-text=Welcome expect-url=/a")
    assert spec == ("https://x.test/a", "/a", "Welcome")


def test_spec_value_may_contain_equals():
    url, expect_url, text = parse_page_spec("https://x.test/a expect-text=sign?next=/home")
    assert (url, expect_url, text) == ("https://x.test/a", None, "sign?next=/home")


@pytest.mark.parametrize("line", [
    "https://x.test/a expect-text=",   # empty value
    "https://x.test/a expect-url",     # key without '='
    "https://x.test/a bogus=1",        # unknown key
    "https://x.test/a free text",      # bare words are not specs
])
def test_spec_rejects_unknown_or_empty_tokens(line):
    with pytest.raises(ValueError, match="Bad page-spec token"):
        parse_page_spec(line)


def test_file_skips_blanks_and_comments():
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as handle:
        handle.write(
            "# header comment\n"
            "https://x.test/a\n"
            "\n"
            "https://x.test/b expect-text=Signed  # why: authed landing\n"
            "  # indented comment\n"
        )
        path = handle.name
    specs = parse_pages_file(path)
    Path(path).unlink()
    assert specs == [
        ("https://x.test/a", None, None),
        ("https://x.test/b", None, "Signed"),
    ]


def test_file_preserves_urls_with_fragments():
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as handle:
        handle.write("https://x.test/a#section\n")
        path = handle.name
    specs = parse_pages_file(path)
    Path(path).unlink()
    assert specs[0][0] == "https://x.test/a#section"
