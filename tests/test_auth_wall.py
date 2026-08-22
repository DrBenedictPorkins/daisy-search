"""Auth-wall detection decides 'seen' vs 'blocked' — the core of the fetch audit."""

import pytest

from daisy.mcp_server import _detect_auth_wall, _slugify


@pytest.mark.parametrize(
    "url",
    [
        "https://www.linkedin.com/in/someone",
        "https://facebook.com/someone",
        "https://x.com/someone",
        "https://accounts.google.com/signin",
    ],
)
def test_known_auth_walled_domains_are_permanent(url):
    result = _detect_auth_wall(200, url, "")
    assert result is not None
    _reason, tool, permanent = result
    assert tool == "Claude-in-Chrome"
    assert permanent is True


def test_rate_limit_is_temporary_and_retryable():
    reason, tool, permanent = _detect_auth_wall(429, "https://example.com", "")
    assert "429" in reason
    assert "retry" in tool
    assert permanent is False


def test_cloudflare_challenge_is_temporary():
    reason, _tool, permanent = _detect_auth_wall(
        403, "https://example.com", "<title>Just a moment...</title>"
    )
    assert "Cloudflare" in reason
    assert permanent is False


def test_plain_403_is_permanent():
    reason, tool, permanent = _detect_auth_wall(403, "https://example.com", "denied")
    assert "403" in reason
    assert tool == "Claude-in-Chrome"
    assert permanent is True


def test_401_requires_authentication():
    reason, _tool, permanent = _detect_auth_wall(401, "https://example.com", "")
    assert "401" in reason
    assert permanent is True


def test_ordinary_public_page_is_not_an_auth_wall():
    result = _detect_auth_wall(200, "https://example.gov/records/1", "<html>ok</html>")
    assert result is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Gina Talia-Faker", "gina-talia-faker"),
        ("  Mixed   CASE  ", "mixed-case"),
        ("punctuation!?", "punctuation"),
        ("under_score", "under-score"),
    ],
)
def test_slugify(raw, expected):
    assert _slugify(raw) == expected


def test_slugify_is_length_bounded_and_never_trailing_dash():
    slug = _slugify("x" * 200)
    assert len(slug) <= 50
    assert not slug.endswith("-")
