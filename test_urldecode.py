"""Tests for urldecode: unwrapping redirector links and stripping tracking params."""

import urllib.parse as ul

import pytest

from urldecode import strip_tracking, unredirect, unwrap

# A real l.facebook.com wrapper, with the click-identifying tokens (fbclid, h,
# c[]) replaced by synthetic values of the same shape and character set.
FACEBOOK_LINK = (
    "https://l.facebook.com/l.php"
    "?u=https%3A%2F%2Fmrf.lu%2F2sW97"
    "%3Ffbclid%3DIwZXh0bgNhZW0ExampleClickIdForTestsOnly0123456789_aem_ExampleAemSuffix"
    "&h=AT0ExampleRedirectSignatureForTestsOnly-0123456789_abcdefgh"
    "&c[0]=AUDExampleClickContextZeroForTestsOnly-0123456789_abcdefgh"
    "&c[1]=AUDExampleClickContextOneForTestsOnly-0123456789_abcdefgh"
)
FACEBOOK_TARGET = "https://mrf.lu/2sW97"


def test_facebook_link_unwraps_to_its_target():
    assert unwrap(FACEBOOK_LINK) == FACEBOOK_TARGET


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/watch?v=abc123",  # used to crash: no second "http"
        "https://example.com",  # no query at all
        "https://example.com/a?ref=nothttp",  # "http" as a substring, not a target
        "https://example.com/httpd-docs?page=2",  # "http" inside the path
    ],
)
def test_non_redirect_urls_pass_through_unchanged(url):
    assert unredirect(url) == url


def test_meaningful_query_params_are_preserved():
    url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ&fbclid=XYZ"
    assert unwrap(url) == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def test_only_tracking_params_are_removed():
    url = "https://example.com/p?id=7&utm_source=fb&utm_medium=x&gclid=Y&page=2"
    assert unwrap(url) == "https://example.com/p?id=7&page=2"


def test_url_without_query_is_left_alone():
    assert strip_tracking("https://example.com/a") == "https://example.com/a"


def test_stripping_every_param_leaves_no_dangling_question_mark():
    assert strip_tracking("https://example.com/a?fbclid=X") == "https://example.com/a"


def test_fragment_is_preserved():
    assert unwrap("https://example.com/doc?fbclid=X#section-3") == "https://example.com/doc#section-3"


def test_encoded_plus_in_target_survives_as_a_plus():
    wrapped = "https://l.facebook.com/l.php?u=" + ul.quote("https://example.com/s?q=a+b", safe="") + "&h=AT0"
    assert unwrap(wrapped) == "https://example.com/s?q=a+b"


def test_raw_plus_in_wrapper_is_not_turned_into_a_space():
    wrapped = "https://l.facebook.com/l.php?u=https%3A%2F%2Fexample.com%2Fs%3Fq%3Da+b"
    assert " " not in unwrap(wrapped)


def test_nested_redirectors_are_unwrapped():
    inner = "https://l.facebook.com/l.php?u=" + ul.quote("https://target.example/x", safe="")
    outer = "https://out.example/away?url=" + ul.quote(inner, safe="")
    assert unwrap(outer) == "https://target.example/x"


def test_google_style_redirect_is_unwrapped():
    assert unredirect("https://www.google.com/url?q=https%3A%2F%2Fexample.com%2Fa&sa=D") == "https://example.com/a"


def test_output_is_a_parseable_absolute_url():
    parts = ul.urlparse(unwrap(FACEBOOK_LINK))
    assert parts.scheme == "https"
    assert parts.netloc == "mrf.lu"
    assert parts.query == ""
