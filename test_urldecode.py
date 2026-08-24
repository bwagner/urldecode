"""Tests for urldecode: unwrapping redirector links and stripping tracking params."""

import socket
import urllib.parse as ul
from types import SimpleNamespace

import pytest

from urldecode import (
    INFERRED_LABEL,
    UNWRAPPED_LABEL,
    TorCheckFailed,
    _forwarding_hop,
    _parser,
    _hop,
    bounded_text,
    chosen_result,
    confirm_tor,
    follow,
    forwarding_target,
    meta_refresh_target,
    og_title,
    sole_offsite_anchor,
    worth_reading,
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
    assert _forwarding_hop(CLIENT_RENDERED_LANDING_PAGE, LANDING_PAGE_URL, messages.append) is None
    assert any("destination" in m for m in messages)


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
