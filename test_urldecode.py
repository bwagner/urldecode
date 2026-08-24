"""Tests for urldecode: unwrapping redirector links and stripping tracking params."""

import socket
import urllib.parse as ul

import pytest

from urldecode import (
    TorCheckFailed,
    confirm_tor,
    follow,
    socks_port_open,
    strip_tracking,
    unredirect,
    unwrap,
)

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


def test_publisher_click_id_is_removed_by_suffix():
    url = (
        "https://example.com/a-ld.123"
        "?utm_campaign=x-facebook-y&utm_source=facebook&utm_medium=social"
        "&mrfcid=20260101ExampleMrfClickIdForTestsOnly"
    )
    assert unwrap(url) == "https://example.com/a-ld.123"


@pytest.mark.parametrize("name", ["mrfcid", "fbclid", "gclid", "somevendorclid", "othercid"])
def test_click_id_suffixes_are_treated_as_tracking(name):
    assert unwrap(f"https://example.com/a?id=7&{name}=ABC123") == "https://example.com/a?id=7"


def test_a_bare_cid_is_stripped_too():
    # Deliberate trade-off of the suffix heuristic: "cid" is also a legitimate
    # abbreviation (category/content/customer id), and those are lost as well.
    assert unwrap("https://example.com/a?cid=7") == "https://example.com/a"


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


# --- following redirects over the network -----------------------------------
#
# follow() takes its resolver as a parameter: resolve(url) returns the Location
# of a single hop, or None when the URL does not redirect. That keeps every
# test below offline - no network, no mocks, just a dict.


def resolver_from(chain, calls=None):
    """Build a resolver over a {url: next_url} mapping, recording its calls."""

    def resolve(url):
        if calls is not None:
            calls.append(url)
        return chain.get(url)

    return resolve


def test_follow_returns_the_url_unchanged_when_nothing_redirects():
    assert follow("https://example.com/a", resolver_from({})) == "https://example.com/a"


def test_follow_strips_the_tracking_each_hop_adds():
    wrapper = (
        "https://l.facebook.com/l.php"
        "?u=https%3A%2F%2Fmrf.lu%2F2sW97%3Ffbclid%3DIwZXh0bgNhZW0ExampleClickId"
        "&h=AT0ExampleRedirectSignature"
    )
    chain = {
        "https://mrf.lu/2sW97": (
            "https://example.com/a-ld.123"
            "?utm_campaign=x-facebook-y&utm_source=facebook&utm_medium=social"
            "&mrfcid=20260101ExampleMrfClickIdForTestsOnly"
        )
    }
    assert follow(wrapper, resolver_from(chain)) == "https://example.com/a-ld.123"


def test_follow_never_sends_tracking_parameters_to_the_resolver():
    wrapper = (
        "https://l.facebook.com/l.php"
        "?u=https%3A%2F%2Fmrf.lu%2F2sW97%3Ffbclid%3DIwZXh0bgNhZW0ExampleClickId"
    )
    calls = []
    follow(wrapper, resolver_from({}, calls))
    assert calls == ["https://mrf.lu/2sW97"]
    assert not any("fbclid" in c for c in calls)


def test_follow_resolves_a_relative_location_header():
    chain = {"https://example.com/a": "/final?utm_source=x"}
    assert follow("https://example.com/a", resolver_from(chain)) == "https://example.com/final"


def test_follow_stops_after_max_hops():
    calls = []

    def endless(url):
        calls.append(url)
        return f"{url}x"

    follow("https://example.com/a", endless, max_hops=3)
    assert len(calls) == 3


def test_follow_terminates_on_a_redirect_cycle():
    chain = {
        "https://example.com/a": "https://example.com/b",
        "https://example.com/b": "https://example.com/a",
    }
    calls = []
    result = follow("https://example.com/a", resolver_from(chain, calls))
    assert result in ("https://example.com/a", "https://example.com/b")
    assert len(calls) < 4  # stopped on the repeat, not by exhausting max_hops


# --- detecting a running tor ------------------------------------------------


def test_socks_port_open_detects_a_listening_socket():
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        assert socks_port_open("127.0.0.1", server.getsockname()[1]) is True


def test_socks_port_open_is_false_when_nothing_listens():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    assert socks_port_open("127.0.0.1", port) is False


# --- refusing to use a proxy that is not confirmed tor -----------------------


def failing_check(_socks_port):
    raise TorCheckFailed("could not reach the check service")


def test_an_unconfirmed_preexisting_port_aborts():
    # We did not start it, so it could be any proxy: refusing is the whole point.
    with pytest.raises(SystemExit):
        confirm_tor(9050, we_started_it=False, report=lambda _: None, check=failing_check)


def test_an_instance_we_started_survives_a_failed_check():
    # We watched this one bootstrap, so a check that cannot complete is a
    # warning, not a reason to throw the run away.
    messages = []
    confirm_tor(9250, we_started_it=True, report=messages.append, check=failing_check)
    assert any("could not reach" in m for m in messages)


def test_a_confirmed_exit_node_is_reported():
    messages = []
    confirm_tor(9050, we_started_it=False, report=messages.append, check=lambda _: "204.8.96.158")
    assert any("204.8.96.158" in m for m in messages)


def test_follow_reports_each_cleaned_url_it_produces():
    chain = {"https://example.com/a": "https://example.com/b?fbclid=X"}
    reported = []
    follow("https://example.com/a", resolver_from(chain), report=reported.append)
    assert reported == ["  => https://example.com/b"]
