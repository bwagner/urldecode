"""Tests for urldecode: unwrapping redirector links and stripping tracking params."""

import re
import socket
import sys
import urllib.parse as ul
from pathlib import Path
from types import SimpleNamespace

import pytest

from urldecode import (
    AMBIGUOUS_MESSAGE,
    BLOCKED_LABEL,
    BROWSER_HEADERS,
    BLOCKING_STATUSES,
    DEFAULT_SCHEME,
    DESTINATION_MESSAGE,
    INFERRED_LABEL,
    MAX_BLOCKED_ATTEMPTS,
    SOCKS_SCHEME,
    TOR_SOCKS_HOST,
    TOR_SOCKS_PORT,
    REQUEST_FAILED_MESSAGE,
    SCHEME_MESSAGE,
    NO_LINKS_MESSAGE,
    UNCORROBORATED_MESSAGE,
    UNREACHED_LABEL,
    UNREAD_MESSAGE,
    UNWRAPPED_LABEL,
    USER_AGENTS,
    USER_AGENT_HEADER,
    TorCheckFailed,
    _proxy_url,
    _forwarding_hop,
    _parser,
    _hop,
    bounded_text,
    chosen_result,
    confirm_tor,
    follow,
    forwarding_target,
    is_blocked,
    needs_get,
    request_headers,
    user_agent,
    meta_refresh_target,
    named_offsite_anchor,
    no_forward_message,
    offsite_targets,
    og_title,
    same_host,
    sole_offsite_anchor,
    worth_reading,
    socks_port_open,
    strip_tracking,
    unblocked_response,
    unredirect,
    unwrap,
    with_scheme,
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


# --- reporting why a URL settled --------------------------------------------


def response(status_code, **headers):
    """A stand-in for an httpx response: _hop only reads status and headers."""
    return SimpleNamespace(status_code=status_code, headers=headers)


def test_a_redirect_yields_its_location_and_reports_nothing():
    messages = []
    location = _hop(response(301, location="https://example.com/b"), messages.append)
    assert location == "https://example.com/b"
    assert messages == []


def test_a_settled_url_reports_its_status_and_type():
    messages = []
    assert _hop(response(200, **{"content-type": "text/html; charset=utf-8"}), messages.append) is None
    assert messages == ["  .. 200 text/html, no redirect"]


def test_a_settled_url_without_a_content_type_still_reports():
    messages = []
    assert _hop(response(204), messages.append) is None
    assert "204" in messages[0]


def test_a_redirect_without_a_location_header_is_reported_not_silent():
    messages = []
    assert _hop(response(302), messages.append) is None
    assert messages != []


# --- reading a forwarding page ----------------------------------------------
#
# A shortener that answers 200 text/html has not necessarily arrived: it may be
# a page that forwards on. Extraction is pure - html in, URL out - so every test
# below is offline, and the wiring that decides whether to read a body at all is
# tested separately.
#
# The two page shapes are modelled on real ones fetched over tor on 2026-08-24,
# with synthetic destinations. Their structure is copied exactly, because the
# structure is the whole discriminator.

# ebx.sh/kfCXO9, a Short.io forwarding page (Short.io fronts thousands of custom
# short domains, so this shape is not one publisher's). Note there is NO <meta
# refresh> here: the refresh rule alone would not find this target.
JS_FORWARDING_PAGE = """\
<html>
<head>
<script src='https://www.googletagmanager.com/gtm.js?id=GTM-EXAMPLE'></script>
<script src="https://connect.facebook.net/en_US/fbevents.js"></script>
<title></title>
<meta property="og:title" content="An example article headline" />
<meta property="og:image" content="https://cdn.example.net/images/example.jpg" />
</head>
<body>
<a style="visibility: hidden" id="urlToFollow"
   data-fallback-url="https://news.example/story-123?utm_source=Facebook"
   href="https://news.example/story-123?utm_source=Facebook">follow</a>
<script type="text/javascript">
document.getElementById('urlToFollow').click();
</script>
</body>
</html>
"""
JS_FORWARDING_PAGE_URL = "https://short.example/kfCXO9"
JS_FORWARDING_TARGET = "https://news.example/story-123?utm_source=Facebook"

# bit.ly/m/FIFA-World-Cup-26-Kansas-City-, a client-rendered landing page. It is
# not a shortener response at all, and the tool must not invent a destination
# for it. Verified against the real page: zero anchors, zero visible body text,
# and seven off-site hosts named only inside a script blob.
CLIENT_RENDERED_LANDING_PAGE = """\
<html>
<head>
<title>EXAMPLE EVENT - Landing Page</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css?family=Example">
<script src="https://d1example.cloudfront.net/bundle.js"></script>
</head>
<body class="viewport">
<script>
window.__DATA__ = {"links":[
  {"url":"https://x.com/example"},
  {"url":"https://www.youtube.com/@example"},
  {"url":"https://www.tiktok.com/@example"},
  {"url":"https://www.instagram.com/example"},
  {"url":"https://open.spotify.com/artist/example"}
]};
</script>
</body>
</html>
"""
LANDING_PAGE_URL = "https://short.example/m/Example-Event-"
# A page that links, and links only within its own host: the one shape that
# earns "this is the destination", since something was read and it went nowhere.
SAME_HOST_ONLY_PAGE = """\
<html>
<head><title>Example News - An article</title></head>
<body>
<a href="/">Home</a>
<a href="/section/world">World</a>
<a href="https://news.example/about">About us</a>
<p>The article itself.</p>
</body>
</html>
"""
SAME_HOST_ONLY_URL = "https://news.example/story/1"
# The shape a deep-link page takes: markup that renders nothing and links
# nowhere, with whatever it does held inside the script. Modelled on a real one,
# which carried 34KB of script, no anchors and not even a <title>.
SCRIPTED_SHELL_PAGE = """\
<html>
<head><meta name="viewport" content="width=device-width"></head>
<body>
<script nonce="synthetic">window.__DDL__ = {"t": "deeplink"}; render();</script>
</body>
</html>
"""
SCRIPTED_SHELL_URL = "https://link.example/abc123"


# --- <meta refresh>: a declared forward -------------------------------------


def test_a_meta_refresh_target_is_found():
    html = '<html><head><meta http-equiv="refresh" content="0; url=https://example.com/a"></head></html>'
    assert meta_refresh_target(html, "https://short.example/x") == "https://example.com/a"


def test_a_meta_refresh_is_matched_regardless_of_case():
    html = '<meta HTTP-EQUIV="Refresh" CONTENT="0; URL=https://example.com/a">'
    assert meta_refresh_target(html, "https://short.example/x") == "https://example.com/a"


def test_a_quoted_meta_refresh_url_loses_its_quotes():
    html = "<meta http-equiv=\"refresh\" content=\"0;url='https://example.com/a'\">"
    assert meta_refresh_target(html, "https://short.example/x") == "https://example.com/a"


def test_a_relative_meta_refresh_is_resolved_against_the_page():
    html = '<meta http-equiv="refresh" content="0; url=/landing">'
    assert meta_refresh_target(html, "https://short.example/x") == "https://short.example/landing"


def test_a_refresh_without_a_url_is_not_a_forward():
    # content="5" reloads this same page; it goes nowhere else.
    html = '<meta http-equiv="refresh" content="5">'
    assert meta_refresh_target(html, "https://short.example/x") is None


def test_a_page_without_a_refresh_yields_nothing():
    assert meta_refresh_target(JS_FORWARDING_PAGE, JS_FORWARDING_PAGE_URL) is None


# --- a sole off-site anchor: an inferred forward -----------------------------


def test_a_sole_offsite_anchor_is_the_forwarding_target():
    assert sole_offsite_anchor(JS_FORWARDING_PAGE, JS_FORWARDING_PAGE_URL) == JS_FORWARDING_TARGET


def test_a_client_rendered_landing_page_yields_nothing():
    assert sole_offsite_anchor(CLIENT_RENDERED_LANDING_PAGE, LANDING_PAGE_URL) is None


def test_urls_named_only_inside_a_script_are_not_candidates():
    # The reason this is parsed as markup rather than pattern-matched: grepping
    # the real landing page for http(s) URLs offers seven plausible off-site
    # answers, none of them anchors, and any pick would be confidently wrong.
    assert "x.com" in CLIENT_RENDERED_LANDING_PAGE
    assert sole_offsite_anchor(CLIENT_RENDERED_LANDING_PAGE, LANDING_PAGE_URL) is None


def test_two_offsite_anchors_are_ambiguous():
    html = '<body><a href="https://a.example/x">a</a><a href="https://b.example/y">b</a></body>'
    assert sole_offsite_anchor(html, "https://short.example/x") is None


def test_a_sole_samesite_anchor_is_not_a_forward():
    # "Back to the homepage" on an error page is not a destination.
    html = '<body><a href="/">home</a></body>'
    assert sole_offsite_anchor(html, "https://short.example/x") is None


def test_an_anchor_without_an_href_is_ignored():
    html = '<body><a name="top"></a><a href="https://news.example/a">go</a></body>'
    assert sole_offsite_anchor(html, "https://short.example/x") == "https://news.example/a"


def test_the_same_target_repeated_is_still_one_target():
    # A page offering the identical URL twice (a button and a text link) is not
    # ambiguous - there is only one place it goes.
    html = (
        '<body><a href="https://news.example/a">go</a>'
        '<a href="https://news.example/a">or click here</a></body>'
    )
    assert sole_offsite_anchor(html, "https://short.example/x") == "https://news.example/a"


# --- choosing between them ---------------------------------------------------


def test_a_declared_refresh_wins_over_an_anchor():
    html = (
        '<head><meta http-equiv="refresh" content="0; url=https://declared.example/a"></head>'
        '<body><a href="https://anchor.example/b">go</a></body>'
    )
    forward = forwarding_target(html, "https://short.example/x")
    assert forward.url == "https://declared.example/a"
    assert forward.declared is True


def test_a_js_forwarding_page_yields_an_inferred_target():
    forward = forwarding_target(JS_FORWARDING_PAGE, JS_FORWARDING_PAGE_URL)
    assert forward.url == JS_FORWARDING_TARGET
    # Inferred from the page's shape, not stated by it: the caller must report
    # this rather than silently substitute it the way it does a Location header.
    assert forward.declared is False


def test_a_landing_page_yields_no_forward_at_all():
    assert forwarding_target(CLIENT_RENDERED_LANDING_PAGE, LANDING_PAGE_URL) is None


def test_an_empty_body_is_not_a_forward():
    assert forwarding_target("", "https://short.example/x") is None


def test_extraction_reports_the_page_verbatim_and_does_not_clean_it():
    # Division of labour: the extractor says what the page says, and unwrap()
    # cleans it - exactly as follow() already cleans a raw Location header.
    forward = forwarding_target(JS_FORWARDING_PAGE, JS_FORWARDING_PAGE_URL)
    assert "utm_source" in forward.url
    assert unwrap(forward.url) == "https://news.example/story-123"


# --- corroborating an inferred forward ---------------------------------------


def test_the_page_title_is_read_from_og_title():
    assert og_title(JS_FORWARDING_PAGE) == "An example article headline"


def test_a_page_without_og_title_has_none():
    assert og_title(CLIENT_RENDERED_LANDING_PAGE) is None


def test_an_inferred_forward_needs_og_title_to_corroborate_it():
    # The anchor rule's untested failure mode is a page carrying exactly one
    # off-site link while being no kind of forward - a paywall offering
    # "subscribe", an error page offering one way out. A page about to show you
    # somewhere else says what it is showing; require it to say so.
    html = '<body><a href="https://news.example/a">go</a></body>'
    assert sole_offsite_anchor(html, "https://short.example/x") == "https://news.example/a"
    assert forwarding_target(html, "https://short.example/x") is None


def test_a_declared_refresh_needs_no_corroboration():
    # The page states its destination outright; there is nothing to corroborate.
    html = '<meta http-equiv="refresh" content="0; url=https://example.com/a">'
    forward = forwarding_target(html, "https://short.example/x")
    assert forward.url == "https://example.com/a"
    assert forward.declared is True


def test_an_inferred_forward_carries_the_title_that_corroborates_it():
    forward = forwarding_target(JS_FORWARDING_PAGE, JS_FORWARDING_PAGE_URL)
    assert forward.title == "An example article headline"


def test_a_refresh_url_may_contain_a_semicolon():
    # The parameter separator and the "; url=" separator are the same character.
    html = '<meta http-equiv="refresh" content="0; url=https://example.com/a?x=1;y=2">'
    assert meta_refresh_target(html, "https://short.example/x") == "https://example.com/a?x=1;y=2"


# --- deciding whether to read a body at all ----------------------------------
#
# Reading bodies is the exception, not the rule: only a 200 that says it is HTML
# is worth opening, and then only far enough to see the head.


def test_a_200_html_response_is_worth_reading():
    assert worth_reading(response(200, **{"content-type": "text/html; charset=utf-8"})) is True


def test_a_200_that_is_not_html_is_left_alone():
    assert worth_reading(response(200, **{"content-type": "application/pdf"})) is False


def test_an_error_page_is_not_read():
    # A 404 is not a forwarder, and its body is nobody's business.
    assert worth_reading(response(404, **{"content-type": "text/html"})) is False


def test_a_response_without_a_content_type_is_not_read():
    assert worth_reading(response(200)) is False


# --- only a page on the host we asked about can still be a forwarder ---------
#
# A page reached by following a Location is where the previous host said to go:
# reading it asks a destination whether it forwards on, which costs a request
# and up to MAX_BODY_BYTES to learn nothing. A page served by the host we asked
# about is the other case - nobody has told us where it goes, and the markup is
# the only thing that can.


def test_a_page_on_the_host_asked_is_still_a_candidate():
    # ebx.sh answers 200 text/html for the very URL handed in; the anchor in
    # that page is the only forward there is.
    assert same_host("https://ebx.sh/kfCXO9", "https://ebx.sh/kfCXO9") is True


def test_a_redirect_that_left_the_host_asked_has_arrived():
    # tinyurl said where it goes. Reading instagram.com only confirms that a
    # destination is a destination.
    assert same_host("https://www.instagram.com/p/abc/", "https://tinyurl.com/2p9dxbcy") is False


def test_a_same_host_redirect_is_still_a_candidate():
    # A shortener may bounce through its own interstitial before serving it.
    assert same_host("https://lnkd.in/interstitial?u=x", "https://lnkd.in/edx9SEqJ") is True


def test_the_host_comparison_ignores_case():
    assert same_host("https://EBX.SH/x", "https://ebx.sh/y") is True


def test_the_host_comparison_ignores_the_port():
    assert same_host("https://example.com:8443/x", "https://example.com/y") is True


def test_the_host_comparison_ignores_the_scheme():
    # An http -> https upgrade is not an arrival.
    assert same_host("http://example.com/x", "https://example.com/x") is True


def test_a_subdomain_is_not_the_host_asked():
    # Deliberately strict: no www-folding, no registrable-domain rule. A
    # shortener that redirects to its own www host loses its body pass, which
    # costs an inference; guessing which subdomains are "the same site" costs
    # correctness.
    assert same_host("https://www.example.com/x", "https://example.com/y") is False


def test_a_page_that_was_not_read_is_not_called_a_destination():
    # DESTINATION_MESSAGE is what a page that was actually parsed earns. Here
    # nothing looked, so the trace may not claim the same fact.
    assert UNREAD_MESSAGE != DESTINATION_MESSAGE
    assert UNREAD_MESSAGE not in (AMBIGUOUS_MESSAGE, UNCORROBORATED_MESSAGE)


# --- reading a bounded prefix of a body --------------------------------------


def test_bounded_text_joins_the_chunks():
    assert bounded_text([b"<html>", b"<body>"], limit=100) == "<html><body>"


def test_bounded_text_stops_at_the_limit():
    # The cap is the fence around "reads a page body": enough to see the head,
    # never enough to be a download.
    assert bounded_text([b"a" * 40, b"b" * 40], limit=50) == "a" * 40 + "b" * 10


def test_bounded_text_decodes_a_character_split_across_chunks():
    encoded = "«Bitcoin»".encode()
    assert bounded_text([encoded[:3], encoded[3:]], limit=100) == "«Bitcoin»"


def test_bounded_text_survives_bytes_that_are_not_utf8():
    assert bounded_text([b"\xff\xfe<html>"], limit=100).endswith("<html>")


def test_bounded_text_of_nothing_is_empty():
    assert bounded_text([], limit=100) == ""


# --- turning a settled page into a hop, or into a report ---------------------


def test_a_declared_refresh_becomes_a_hop():
    html = '<meta http-equiv="refresh" content="0; url=https://news.example/a">'
    assert _forwarding_hop(html, "https://short.example/x", lambda _: None) == "https://news.example/a"


def test_a_declared_hop_is_not_reported_here():
    # follow() prints the raw target and its cleaned form, exactly as it does
    # for a Location header. Announcing it here as well would say it twice.
    html = '<meta http-equiv="refresh" content="0; url=https://news.example/a">'
    messages = []
    _forwarding_hop(html, "https://short.example/x", messages.append)
    assert messages == []


def test_an_inferred_forward_is_reported_and_not_followed():
    messages = []
    assert _forwarding_hop(JS_FORWARDING_PAGE, JS_FORWARDING_PAGE_URL, messages.append) is None
    assert any("news.example/story-123" in m for m in messages)


def test_an_inferred_candidate_is_reported_without_its_tracking():
    messages = []
    _forwarding_hop(JS_FORWARDING_PAGE, JS_FORWARDING_PAGE_URL, messages.append)
    assert not any("utm_source" in m for m in messages)


def test_the_candidate_is_reported_with_the_title_that_corroborates_it():
    messages = []
    _forwarding_hop(JS_FORWARDING_PAGE, JS_FORWARDING_PAGE_URL, messages.append)
    assert any("An example article headline" in m for m in messages)


def test_a_page_that_forwards_nowhere_is_named_the_destination():
    # The whole point of reading the body: "no redirect" covered both "this is
    # where you were going" and "it goes on and I cannot see how". Now it does not.
    messages = []
    assert _forwarding_hop(SAME_HOST_ONLY_PAGE, SAME_HOST_ONLY_URL, messages.append) is None
    assert any("destination" in m for m in messages)


def test_a_client_rendered_page_is_not_named_the_destination():
    # It settles the same way - there is nothing to hop to - but the claim is
    # withdrawn. This page and a script-driven forwarder are the same page to a
    # parser, and only one of them is an arrival.
    messages = []
    assert _forwarding_hop(CLIENT_RENDERED_LANDING_PAGE, LANDING_PAGE_URL, messages.append) is None
    assert not any("destination" in m for m in messages)


# --- tracking that hides in the fragment -------------------------------------
#
# A fragment never reaches the server, so a marker there is read by the page's
# own JavaScript on arrival: the same job as an fbclid by another route.


def test_a_tracking_fragment_is_removed():
    assert unwrap("https://example.com/a#Echobox=1787588927") == "https://example.com/a"


def test_a_tracking_fragment_goes_along_with_the_query_it_came_with():
    url = "https://www.example.com/story-123?utm_source=Facebook#Echobox=1787588927"
    assert unwrap(url) == "https://www.example.com/story-123"


def test_a_tracking_fragment_is_matched_regardless_of_case():
    assert unwrap("https://example.com/a#echobox=123") == "https://example.com/a"


def test_a_utm_fragment_is_caught_by_the_existing_prefix_rule():
    assert unwrap("https://example.com/a#utm_source=x") == "https://example.com/a"


def test_a_plain_anchor_is_never_touched():
    # No "=", so nothing to read as a name: an anchor cannot be mistaken for one.
    assert unwrap("https://example.com/doc#section-3") == "https://example.com/doc#section-3"


@pytest.mark.parametrize("fragment", ["t=120", "page=5", ":~:text=some%20phrase"])
def test_a_functional_key_value_fragment_survives(fragment):
    # A video timestamp, a PDF page, a text fragment: key=value, but the key is
    # not tracking, so the same rule that catches Echobox leaves these alone.
    assert unwrap(f"https://example.com/a#{fragment}") == f"https://example.com/a#{fragment}"


# --- matching a name whatever its case ---------------------------------------


@pytest.mark.parametrize("name", ["FBCLID", "FbClid", "UTM_SOURCE", "Utm_Source", "MRFCID", "Echobox"])
def test_a_tracking_parameter_is_matched_regardless_of_case(name):
    assert unwrap(f"https://example.com/a?id=7&{name}=X") == "https://example.com/a?id=7"


def test_an_uppercase_cid_is_stripped_like_the_lowercase_one():
    # The suffix rule's accepted cost applies in every case, not just lowercase.
    assert unwrap("https://example.com/a?CID=7") == "https://example.com/a"


@pytest.mark.parametrize("name", ["ID", "Page", "V", "Q"])
def test_case_folding_does_not_widen_the_rule_to_ordinary_names(name):
    assert unwrap(f"https://example.com/a?{name}=7") == f"https://example.com/a?{name}=7"


# --- carrying an inferred candidate up to the caller -------------------------
#
# The candidate is found four levels below main(), and returning it as a hop
# would make follow() request it. It travels up its own way instead, and the
# choice of what to do with it is made in one place at the top.


def test_an_inferred_candidate_is_noted_while_still_not_being_followed():
    noted = []
    hop = _forwarding_hop(JS_FORWARDING_PAGE, JS_FORWARDING_PAGE_URL, lambda _: None, noted.append)
    assert hop is None
    # Cleaned, so that what is noted is what the "??" line showed.
    assert noted == [unwrap(JS_FORWARDING_TARGET)]


def test_a_declared_refresh_is_never_noted_as_inferred():
    # It is a hop, and follow() will resolve it. Nothing is being guessed.
    html = '<meta http-equiv="refresh" content="0; url=https://news.example/a">'
    noted = []
    _forwarding_hop(html, "https://short.example/x", lambda _: None, noted.append)
    assert noted == []


def test_a_page_that_forwards_nowhere_notes_nothing():
    noted = []
    _forwarding_hop(CLIENT_RENDERED_LANDING_PAGE, LANDING_PAGE_URL, lambda _: None, noted.append)
    assert noted == []


def test_noting_is_optional():
    assert _forwarding_hop(JS_FORWARDING_PAGE, JS_FORWARDING_PAGE_URL, lambda _: None) is None


# --- choosing between the settled URL and the candidate ----------------------

SETTLED = "https://short.example/kfCXO9"
CANDIDATE = "https://news.example/story-123"


def test_a_trusted_candidate_becomes_the_result():
    assert chosen_result(SETTLED, CANDIDATE, trust=True) == (CANDIDATE, INFERRED_LABEL)


def test_an_untrusted_candidate_leaves_the_settled_url_as_the_result():
    assert chosen_result(SETTLED, CANDIDATE, trust=False) == (SETTLED, UNWRAPPED_LABEL)


def test_trust_without_a_candidate_changes_nothing():
    assert chosen_result(SETTLED, None, trust=True) == (SETTLED, UNWRAPPED_LABEL)


def test_no_candidate_and_no_trust_is_the_ordinary_case():
    assert chosen_result(SETTLED, None, trust=False) == (SETTLED, UNWRAPPED_LABEL)


def test_an_inferred_candidate_is_taken_unless_it_is_refused():
    # The flag exists to decline the candidate, not to ask for it: a page that
    # names where it forwards is answering the question the tool was asked.
    assert _parser().parse_args([]).trust_inferred is True
    assert _parser().parse_args(["--no-trust-inferred"]).trust_inferred is False


# --- a refusal is not an answer ----------------------------------------------
#
# LinkedIn's edge returns 403 to a tor exit intermittently: the same request
# through a fresh circuit succeeds, and the plain 403 page it serves parses as
# HTML that forwards nowhere. Left alone, a refusal aimed at the exit node is
# reported as a fact about the URL. It is retried instead.
#
# Every test here is offline: the retry takes its request as a parameter, the
# way follow() takes its resolver.


@pytest.mark.parametrize("status", [403, 429])
def test_a_refusal_is_recognised_as_a_block(status):
    assert is_blocked(status)


@pytest.mark.parametrize("status", [200, 301, 404, 500])
def test_an_ordinary_answer_is_not_a_block(status):
    # A 404 is the URL's own answer and a 500 is the server failing at it;
    # neither is aimed at this client, so neither is worth a new circuit.
    assert not is_blocked(status)


def requester_from(statuses, tried=None):
    """Build a request(attempt) over canned statuses, recording its calls."""

    def request(attempt):
        if tried is not None:
            tried.append(attempt)
        return response(statuses[attempt])

    return request


def test_a_response_that_is_not_refused_is_returned_at_once():
    tried = []
    assert unblocked_response(requester_from([200], tried)).status_code == 200
    assert tried == [0]


def test_a_refusal_is_retried_and_the_answer_that_follows_is_kept():
    tried = []
    assert unblocked_response(requester_from([403, 200], tried)).status_code == 200
    assert tried == [0, 1]


def test_every_attempt_asks_for_a_circuit_of_its_own():
    tried = []
    unblocked_response(requester_from([403, 403, 200], tried))
    assert len(set(tried)) == len(tried)


def test_retrying_stops_at_the_cap_and_yields_the_last_refusal():
    tried = []
    refused = unblocked_response(requester_from([403] * 5, tried), attempts=3)
    assert is_blocked(refused.status_code)
    assert tried == [0, 1, 2]


def test_every_retry_is_reported():
    messages = []
    unblocked_response(requester_from([403, 403, 200]), report=messages.append)
    assert len(messages) == 2


def test_a_run_that_was_never_refused_reports_nothing():
    messages = []
    unblocked_response(requester_from([200]), report=messages.append)
    assert messages == []


def test_reporting_is_optional():
    assert unblocked_response(requester_from([403, 200])).status_code == 200


# --- a new circuit for every attempt -----------------------------------------
#
# tor gives a distinct circuit per distinct SOCKS username/password pair
# (IsolateSOCKSAuth, on by default), so a retry needs no control port and no
# NEWNYM: a new credential in the proxy URL is a new exit node.


def test_the_proxy_url_still_points_at_the_local_tor_port():
    parts = ul.urlsplit(_proxy_url(TOR_SOCKS_PORT))
    assert parts.scheme == SOCKS_SCHEME
    assert parts.hostname == TOR_SOCKS_HOST
    assert parts.port == TOR_SOCKS_PORT


def test_each_attempt_gets_a_credential_of_its_own():
    first = ul.urlsplit(_proxy_url(TOR_SOCKS_PORT, 0))
    second = ul.urlsplit(_proxy_url(TOR_SOCKS_PORT, 1))
    assert (first.username, first.password) != (second.username, second.password)


def test_the_same_attempt_stays_on_the_same_circuit():
    assert _proxy_url(TOR_SOCKS_PORT, 1) == _proxy_url(TOR_SOCKS_PORT, 1)


def test_the_credential_survives_the_round_trip_through_the_url():
    # A credential carrying ":" or "@" would silently reshape the proxy URL and
    # send the requests somewhere else entirely.
    parts = ul.urlsplit(_proxy_url(TOR_SOCKS_PORT, 2))
    assert parts.username
    assert parts.hostname == TOR_SOCKS_HOST
    assert parts.port == TOR_SOCKS_PORT


# --- a refused chain is not a destination ------------------------------------


def test_a_refused_chain_is_labelled_as_blocked():
    _, label = chosen_result("https://short.example/x", None, True, blocked=True)
    assert label == BLOCKED_LABEL


def test_a_refused_chain_still_yields_the_url_it_reached():
    result, _ = chosen_result("https://short.example/x", None, True, blocked=True)
    assert result == "https://short.example/x"


def test_a_chain_that_was_not_refused_keeps_the_ordinary_label():
    _, label = chosen_result("https://example.com/a", None, True, blocked=False)
    assert label == UNWRAPPED_LABEL


def test_a_candidate_read_off_a_page_outranks_a_late_refusal():
    result, label = chosen_result("https://short.example/x", "https://example.com/real", True, blocked=True)
    assert (result, label) == ("https://example.com/real", INFERRED_LABEL)


# --- asking again with a different verb --------------------------------------
#
# A HEAD that came back refused has not answered the question, and on lnkd.in
# it is refused far harder than a GET is: 1 HEAD in 9 got through where 4 GETs
# in 9 did, on fresh circuits with the same headers. So a refusal falls into
# the same streamed-GET path that a host refusing HEAD outright already takes.


@pytest.mark.parametrize("status", [405, 501])
def test_a_host_that_refuses_the_method_is_asked_with_get(status):
    assert needs_get(status)


@pytest.mark.parametrize("status", [403, 429])
def test_a_refused_head_is_asked_with_get_before_giving_up(status):
    assert needs_get(status)


@pytest.mark.parametrize("status", [200, 301, 404, 500])
def test_an_answered_head_is_not_asked_again(status):
    # These answered the question, even when the answer is bad news; asking the
    # same URL again with another verb would learn nothing.
    assert not needs_get(status)


def test_every_refusal_is_worth_a_get():
    # The two predicates must not drift apart: anything is_blocked() retries on
    # a new circuit is also something a GET should be tried on.
    assert all(needs_get(status) for status in BLOCKING_STATUSES)


# --- a page that names its own destination -----------------------------------
#
# lnkd.in's interstitial links off-site twice: to the destination, and to a
# LinkedIn help page - off-site because the page itself is on lnkd.in. The
# sole-anchor rule declines that, correctly, and the link is unresolvable.
#
# What separates the two is that a forwarding page shows you the URL it is
# about to send you to. "Learn more" never does. Modelled on the real page
# fetched over tor on 2026-08-25, structure copied exactly, destination
# synthetic.

INTERSTITIAL_PAGE = """\
<html lang="en">
<head>
<meta name="pageKey" content="d_shortlink_frontend_external_link_redirect_interstitial">
<meta property="og:title" content="LinkedIn">
</head>
<body>
<a class="artdeco-button artdeco-button--tertiary" data-tracking-control-name="external_url_click"
   data-tracking-will-navigate href="https://news.example/story-456">
    https://news.example/story-456
</a>
<a class="t-14 artdeco-button artdeco-button--tertiary" data-tracking-control-name="learn_more_click"
   data-tracking-will-navigate href="https://help.example/answer/a1341680?trk=in_page_learn_more_click"
   target="_blank">
    Learn more
</a>
</body>
</html>
"""
INTERSTITIAL_URL = "https://short.example/edx9SEqJ"
INTERSTITIAL_TARGET = "https://news.example/story-456"


def test_the_interstitial_links_off_site_more_than_once():
    # The premise of the whole rule: this is why the sole-anchor test fails here.
    assert len(offsite_targets(INTERSTITIAL_PAGE, INTERSTITIAL_URL)) > 1
    assert sole_offsite_anchor(INTERSTITIAL_PAGE, INTERSTITIAL_URL) is None


def test_an_anchor_that_prints_its_own_url_is_the_forward():
    assert named_offsite_anchor(INTERSTITIAL_PAGE, INTERSTITIAL_URL) == INTERSTITIAL_TARGET


def test_the_interstitial_now_yields_its_destination():
    forward = forwarding_target(INTERSTITIAL_PAGE, INTERSTITIAL_URL)
    assert forward.url == INTERSTITIAL_TARGET
    assert forward.declared is False


def test_whitespace_around_the_printed_url_does_not_matter():
    html = '<a href="https://news.example/x">\n      https://news.example/x\n   </a>'
    assert named_offsite_anchor(html, INTERSTITIAL_URL) == "https://news.example/x"


def test_an_anchor_whose_text_is_a_label_names_nothing():
    html = '<a href="https://news.example/x">Continue</a><a href="https://other.example/y">Help</a>'
    assert named_offsite_anchor(html, INTERSTITIAL_URL) is None


def test_an_anchor_printing_a_different_url_than_it_points_at_names_nothing():
    # The shape a deceptive link takes; it must not be read as a forward.
    html = '<a href="https://evil.example/x">https://news.example/x</a><a href="https://other.example/y">Help</a>'
    assert named_offsite_anchor(html, INTERSTITIAL_URL) is None


def test_two_anchors_printing_their_own_urls_are_still_ambiguous():
    html = (
        '<a href="https://news.example/x">https://news.example/x</a>'
        '<a href="https://other.example/y">https://other.example/y</a>'
    )
    assert named_offsite_anchor(html, INTERSTITIAL_URL) is None


def test_a_named_anchor_still_needs_og_title_to_corroborate_it():
    html = INTERSTITIAL_PAGE.replace('<meta property="og:title" content="LinkedIn">', "")
    assert named_offsite_anchor(html, INTERSTITIAL_URL) == INTERSTITIAL_TARGET
    assert forwarding_target(html, INTERSTITIAL_URL) is None


def test_a_declared_refresh_still_wins_over_a_named_anchor():
    html = '<meta http-equiv="refresh" content="0; url=https://declared.example/z">' + INTERSTITIAL_PAGE
    forward = forwarding_target(html, INTERSTITIAL_URL)
    assert forward.url == "https://declared.example/z"
    assert forward.declared is True


def test_the_sole_anchor_rule_is_untouched_by_the_tie_break():
    # The tie-break may only fill in a blank, never change an existing verdict.
    assert forwarding_target(JS_FORWARDING_PAGE, JS_FORWARDING_PAGE_URL).url == JS_FORWARDING_TARGET
    assert forwarding_target(CLIENT_RENDERED_LANDING_PAGE, LANDING_PAGE_URL) is None


# --- listing what a page links off-site --------------------------------------


def test_offsite_targets_keeps_document_order():
    assert offsite_targets(INTERSTITIAL_PAGE, INTERSTITIAL_URL)[0] == INTERSTITIAL_TARGET


def test_offsite_targets_ignores_links_back_into_the_page_host():
    html = '<a href="/help">Help</a><a href="https://news.example/x">x</a>'
    assert offsite_targets(html, INTERSTITIAL_URL) == ["https://news.example/x"]


def test_offsite_targets_counts_a_repeated_target_once():
    html = '<a href="https://news.example/x">a</a><a href="https://news.example/x">b</a>'
    assert offsite_targets(html, INTERSTITIAL_URL) == ["https://news.example/x"]


# --- saying why no forward was found -----------------------------------------
#
# "no forward in the page: this is the destination" was printed for four
# different situations, only one of which it describes. A page that forwards
# somewhere this tool cannot pin down is not a page that forwards nowhere, and a
# page carrying no links at all was never read in the first place.


def test_a_page_that_links_nowhere_off_site_is_the_destination():
    assert no_forward_message(SAME_HOST_ONLY_PAGE, SAME_HOST_ONLY_URL) == DESTINATION_MESSAGE


def test_several_off_site_links_and_no_candidate_is_reported_as_ambiguous():
    html = '<a href="https://news.example/x">Continue</a><a href="https://other.example/y">Help</a>'
    assert no_forward_message(html, INTERSTITIAL_URL) == AMBIGUOUS_MESSAGE


def test_a_candidate_without_a_title_is_reported_as_uncorroborated():
    html = '<a href="https://news.example/x">Continue</a>'
    assert no_forward_message(html, INTERSTITIAL_URL) == UNCORROBORATED_MESSAGE


def test_a_page_with_no_links_at_all_says_so():
    # Nothing was read, so nothing is claimed. The destination message is earned
    # by finding links and following none of them off-site.
    assert no_forward_message(SCRIPTED_SHELL_PAGE, SCRIPTED_SHELL_URL) == NO_LINKS_MESSAGE


def test_a_client_rendered_page_gets_the_same_answer_as_a_shell():
    # These two are indistinguishable without running their scripts, so they are
    # reported identically rather than guessed at. This is the case that used to
    # come back as "this is the destination".
    assert no_forward_message(CLIENT_RENDERED_LANDING_PAGE, LANDING_PAGE_URL) == NO_LINKS_MESSAGE


def test_a_shell_is_not_announced_as_the_destination():
    assert "destination" not in NO_LINKS_MESSAGE


def test_an_ambiguous_page_is_not_announced_as_the_destination():
    messages = []
    html = '<a href="https://news.example/x">Continue</a><a href="https://other.example/y">Help</a>'
    assert _forwarding_hop(html, INTERSTITIAL_URL, messages.append) is None
    assert messages == [AMBIGUOUS_MESSAGE]


# --- how many circuits to try before giving up -------------------------------
#
# Three was a guess made before any of this was measured. Against lnkd.in
# roughly one request in three gets through, and a run has to win twice - once
# for the HEAD, once for the body pass - which put the odds of a whole run
# succeeding near a coin flip. The cap is settable so the next stubborn host
# does not need a code change.


def test_a_cap_of_one_never_retries():
    tried = []
    refused = unblocked_response(requester_from([403, 200], tried), attempts=1)
    assert is_blocked(refused.status_code)
    assert tried == [0]


def test_the_attempt_cap_defaults_to_the_constant():
    assert _parser().parse_args([]).max_attempts == MAX_BLOCKED_ATTEMPTS


def test_the_attempt_cap_can_be_raised_from_the_command_line():
    assert _parser().parse_args(["-a", "12"]).max_attempts == 12


def test_the_attempt_cap_has_a_long_form():
    assert _parser().parse_args(["--max-attempts", "12"]).max_attempts == 12


# --- the README's Usage block -----------------------------------------------
#
# That block is help output pasted in by hand, and nothing regenerates it: it
# was stale from before -T existed and was only noticed while adding -a. This
# test is what pins it. To regenerate the block after a CLI change:
#
#   COLUMNS=80 ./urldecode.py --help
#
# argparse wraps to the terminal width and takes the program name from
# sys.argv[0], so this test fixes both. Before 3.13 it rendered a short and
# long option pair as "-a N, --max-attempts N" rather than "-a, --max-attempts
# N" - a difference in the interpreter, not a stale README - so the comparison
# is skipped there rather than failing misleadingly.

README = Path(__file__).parent / "README.md"
USAGE_HEADING = "## Usage"
FENCE = "```"
USAGE_BLOCK = re.compile(rf"{USAGE_HEADING}\n\n{FENCE}\n(.*?){FENCE}\n", re.DOTALL)
COLUMNS_ENV = "COLUMNS"
HELP_COLUMNS = 80
HELP_PROG = "urldecode.py"
ARGPARSE_PAIRED_OPTIONS = (3, 13)


def readme_usage_block():
    """The fenced block under the README's Usage heading, which should be the help text."""
    found = USAGE_BLOCK.search(README.read_text())
    assert found, f"no fenced block under {USAGE_HEADING!r} in {README}"
    return found.group(1)


@pytest.mark.skipif(
    sys.version_info < ARGPARSE_PAIRED_OPTIONS,
    reason="argparse renders short and long option pairs differently before 3.13",
)
def test_the_readme_usage_block_is_the_parsers_help(monkeypatch):
    monkeypatch.setenv(COLUMNS_ENV, str(HELP_COLUMNS))
    monkeypatch.setattr(sys, "argv", [HELP_PROG])
    assert readme_usage_block() == _parser().format_help()


# --- a URL that arrived without a scheme -------------------------------------
#
# Bare "tinyurl.com/x" is what a link copied out of running text looks like.
# Offline it used to pass straight through and be handed back as the answer;
# under --follow httpx refused it outright and the traceback was the whole
# output. The repair is made where the URL is read, and announced.


def test_a_bare_host_is_given_a_scheme():
    assert with_scheme("tinyurl.com/stadiband20260304?t=49") == (
        DEFAULT_SCHEME + "tinyurl.com/stadiband20260304?t=49"
    )


def test_a_url_that_has_a_scheme_is_untouched():
    assert with_scheme("https://example.com/x") == "https://example.com/x"
    assert with_scheme("http://example.com/x") == "http://example.com/x"


def test_the_scheme_is_recognised_whatever_its_case():
    # Mail clients and spreadsheets capitalise it; prepending a second scheme
    # would produce a URL that resolves nowhere at all.
    assert with_scheme("HTTPS://example.com/x") == "HTTPS://example.com/x"


def test_a_host_and_port_is_not_read_as_a_scheme():
    # The reason the test is a literal prefix: urlsplit reads the scheme of
    # "localhost:8080/x" as "localhost", so a check on urlsplit().scheme would
    # leave this exact input for httpx to refuse.
    assert with_scheme("localhost:8080/x") == DEFAULT_SCHEME + "localhost:8080/x"


def test_nothing_at_all_stays_nothing():
    # An empty clipboard is not a URL missing its scheme, and "https://" is a
    # worse answer than the emptiness it came from.
    assert with_scheme("") == ""
    assert with_scheme("   ") == "   "


def test_a_hostless_url_shares_its_host_with_nothing():
    # urlsplit reports both of these hosts as None. Read as equal, two
    # unrelated scheme-less URLs would count as the same host and open the
    # body pass on a page the chain never asked about.
    assert same_host("a.example.com/x", "b.example.com/y") is False


def test_a_hostless_url_is_not_even_its_own_host():
    assert same_host("example.com/x", "example.com/x") is False


def test_assuming_a_scheme_is_announced():
    # The one place this tool changes the URL it was handed rather than reading
    # it, so it must not happen silently.
    assert SCHEME_MESSAGE.endswith(DEFAULT_SCHEME)


# --- a request that never completed ------------------------------------------
#
# Everything above assumes a server answered. When httpx raises instead - a URL
# it will not form, a name that does not resolve, a circuit that dies mid-body -
# the chain has no answer, and used to have no trace either: the traceback was
# the entire output. A failure is not a refusal, so it gets its own label.


def test_a_chain_that_never_landed_is_not_called_a_destination():
    result, label = chosen_result("https://tinyurl.com/x", None, True, unreached=True)
    assert label is UNREACHED_LABEL
    assert result == "https://tinyurl.com/x"


def test_a_refusal_and_a_failure_are_told_apart():
    # Both mean "only as far as this got", but a host that said no told us
    # something about the URL and a request that never landed did not.
    _, refused = chosen_result("https://x/y", None, True, blocked=True)
    _, failed = chosen_result("https://x/y", None, True, unreached=True)
    assert refused is BLOCKED_LABEL
    assert failed is UNREACHED_LABEL


def test_an_inferred_forward_still_wins_over_a_failure():
    # A candidate can only exist because a page was read, which means a request
    # did land. The failure that follows does not unmake the finding.
    result, label = chosen_result("https://x/y", "https://real/z", True, unreached=True)
    assert (result, label) == ("https://real/z", INFERRED_LABEL)


def test_a_failure_names_its_reason():
    # "ConnectError" alone would not distinguish a dead circuit from a URL that
    # was never valid, and the difference is the whole diagnosis.
    line = REQUEST_FAILED_MESSAGE.format(reason="UnsupportedProtocol: missing an http://")
    assert "UnsupportedProtocol" in line
    assert line.startswith("  ..")


# --- a fresh identity on every attempt ---------------------------------------
# A retry gets a new exit node, and used to carry the one thing the new node
# could not disguise: an identical User-Agent, eight times over. These pin the
# rotation, not the strings - which Chrome versions are current is a value that
# goes stale, and asserting it would only pin today.

CHROME_UA = re.compile(
    r"^Mozilla/5\.0 \(.+\) AppleWebKit/537\.36 "
    r"\(KHTML, like Gecko\) Chrome/\d+\.0\.0\.0 Safari/537\.36$"
)


def test_every_identity_is_a_well_formed_chrome_ua():
    # A malformed UA is worse than a boring one: it is a fingerprint of its own.
    assert USER_AGENTS
    for candidate in USER_AGENTS:
        assert CHROME_UA.match(candidate), candidate


def test_a_whole_run_of_retries_never_repeats_an_identity():
    # The list is deliberately longer than the attempt cap, so the worst case -
    # a host refusing every circuit - still shows a different UA each time.
    used = [user_agent(attempt) for attempt in range(MAX_BLOCKED_ATTEMPTS)]
    assert len(set(used)) == len(used)


def test_consecutive_attempts_differ():
    assert user_agent(0) != user_agent(1)


def test_the_rotation_wraps_rather_than_running_out():
    # attempt is not bounded by the cap: -a can raise it past the list length.
    assert user_agent(len(USER_AGENTS)) == user_agent(0)
    assert user_agent(len(USER_AGENTS) + 3) == user_agent(3)


def test_the_headers_carry_this_attempts_identity():
    assert request_headers(2)[USER_AGENT_HEADER] == user_agent(2)


def test_the_headers_keep_the_rest_of_the_browser_set():
    headers = request_headers(0)
    for name, value in BROWSER_HEADERS.items():
        assert headers[name] == value


def test_building_headers_leaves_the_shared_set_alone():
    # A UA written into BROWSER_HEADERS by accident would pin attempt 0's
    # identity onto every later attempt, silently undoing the rotation.
    request_headers(1)
    assert USER_AGENT_HEADER not in BROWSER_HEADERS
