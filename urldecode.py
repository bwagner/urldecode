#!/usr/bin/env -S uv run --script
# /// script
# dependencies = ["pyperclip", "httpx[socks]", "stem"]
# ///
"""Find where a link really goes, and strip the tracking on the way."""

import argparse
import shutil
import socket
import sys
import tempfile
import urllib.parse as ul
from html.parser import HTMLParser
from typing import NamedTuple, Optional

URL_SCHEMES = ("https://", "http://")
PARAM_SEP = "&"
KV_SEP = "="

# Parameters that identify the click, not the content.
TRACKING_PARAMS = frozenset(
    {
        "fbclid",
        "gclid",
        "dclid",
        "msclkid",
        "yclid",
        "twclid",
        "ttclid",
        "igshid",
        "mc_cid",
        "mc_eid",
        "vero_id",
        "s_kwcid",
        "_ga",
        "_gl",
        "ref_src",
        "ref_url",
        # Publishers using Echobox tag their posts with a "#Echobox=<id>"
        # fragment, read by its JavaScript on arrival rather than by the server.
        "echobox",
    }
)
TRACKING_PREFIXES = ("utm_",)

# Publishers invent their own click ids faster than a list can track them
# (nzz.ch's mrf.lu shortener uses "mrfcid"). The cost of catching them by
# suffix is that a legitimate "cid" - category, content, customer id - is
# stripped too; that is an accepted trade-off.
TRACKING_SUFFIXES = ("clid", "cid")

# A redirector wrapping a redirector is rare; a cycle should not hang the tool.
MAX_UNWRAP_DEPTH = 5


# --- following redirects over the network, through tor -----------------------

TOR_COMMAND = "tor"
TOR_SOCKS_HOST = "127.0.0.1"
TOR_SOCKS_PORT = 9050
# A private instance gets its own port so it never collides with a system tor.
TOR_LAUNCH_PORT = 9250
TOR_CHECK_URL = "https://check.torproject.org/api/ip"
TOR_BOOTSTRAP_PERCENT = 100
TOR_BOOTSTRAP_TIMEOUT = 90
# The first request through a freshly built circuit routinely dies with an
# SSL EOF; the next one succeeds. Retry before concluding anything.
TOR_CHECK_ATTEMPTS = 3
PORT_PROBE_TIMEOUT = 1.0
REQUEST_TIMEOUT = 30
MAX_FOLLOW_HOPS = 10
REDIRECT_STATUSES = range(300, 400)
METHOD_NOT_ALLOWED = (405, 501)
LOCATION_HEADER = "location"
CONTENT_TYPE_HEADER = "content-type"
CONTENT_TYPE_SEP = ";"
UNKNOWN_CONTENT_TYPE = "unknown type"
# curl's --socks5-hostname equivalent: DNS is resolved by the exit node, not here.
SOCKS_SCHEME = "socks5h"
# A plain, current UA: shorteners behind a WAF reject the default one, and an
# unusual one is a fingerprint.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)

FOLLOW_EPILOG = """\
--follow needs tor and never falls back to a direct connection:

  * An existing tor on 127.0.0.1:9050 is used, once check.torproject.org
    confirms it really is tor. Run "tor" in another terminal to have one; it
    stays up across runs, so repeated lookups skip the bootstrap wait.
  * Otherwise a private instance is started on {launch_port}, its bootstrap is
    streamed, and it is stopped again on exit.
  * If tor is not installed, it aborts with installation instructions.
"""

TOR_MISSING_MESSAGE = """\
--follow needs tor, and none is running or installed.

    brew install tor

Then either run this again and let it start its own private instance, or run
tor yourself in another terminal:

    tor                      # foreground, ctrl-C to quit, nothing registered
    brew services run tor    # background, does NOT come back after a reboot
    brew services start tor  # background, DOES start again at every login
    brew services stop tor   # stop and unregister ("kill" stops but keeps it)
"""

TOR_UNCONFIRMED_MESSAGE = """\
Something is listening on {host}:{port}, but this run did not start it and
{url} does not confirm it is tor:

    {reason}

Refusing to send the request: that port may be an ordinary proxy, and using it
would expose the IP that --follow exists to hide.
"""


def _params(query):
    """Yield (decoded name, decoded value, raw pair) for each pair in a query string."""
    for pair in query.split(PARAM_SEP):
        if not pair:
            continue
        name, _, value = pair.partition(KV_SEP)
        yield ul.unquote(name), ul.unquote(value), pair


def _redirect_target(url):
    """Return the URL this one redirects to, or None if it is not a redirector."""
    for _name, value, _pair in _params(ul.urlsplit(url).query):
        if value.startswith(URL_SCHEMES):
            return value
    return None


def is_tracking(name):
    """True when a parameter or fragment name identifies the click, not the content.

    The denylist, the prefix and the suffixes are all written in lower case and
    the name is folded to match: publishers capitalise these inconsistently
    ("Echobox" in a fragment, "fbclid" in a query), and the same marker should
    not survive one link and not the next because of a capital letter.
    """
    name = name.lower()
    return (
        name in TRACKING_PARAMS
        or name.startswith(TRACKING_PREFIXES)
        or name.endswith(TRACKING_SUFFIXES)
    )


def unredirect(url):
    """Follow redirector wrappers down to the target URL, textually."""
    for _ in range(MAX_UNWRAP_DEPTH):
        target = _redirect_target(url)
        if target is None:
            break
        url = target
    return url


def _clean_fragment(fragment):
    """Drop a fragment that is a tracking key=value pair; keep every other.

    A fragment is never sent to the server, so a marker in one is read by the
    page's own JavaScript on arrival - the same job as an fbclid, by another
    route. An ordinary anchor ("#section-3") has no "=" and cannot be read as a
    name, so it is never at risk; a functional pair ("#t=120") keeps its
    fragment because its name is not tracking.
    """
    name, separator, _value = fragment.partition(KV_SEP)
    if separator and is_tracking(ul.unquote(name)):
        return ""
    return fragment


def strip_tracking(url):
    """Drop tracking parameters, keeping every other parameter byte-identical."""
    parts = ul.urlsplit(url)
    kept = [pair for name, _value, pair in _params(parts.query) if not is_tracking(name)]
    return ul.urlunsplit(
        parts._replace(query=PARAM_SEP.join(kept), fragment=_clean_fragment(parts.fragment))
    )


def unwrap(url):
    """Return the real destination of url, without tracking parameters."""
    return strip_tracking(unredirect(url))


# --- reading a page that forwards on -----------------------------------------
#
# A shortener answering 200 text/html has not necessarily arrived; the page may
# forward on. Finding that out means reading a body, which the header-only path
# deliberately never does, so it is fenced in: the markup is parsed, never
# executed, and nothing it references is fetched.

META_TAG = "meta"
ANCHOR_TAG = "a"
HTTP_EQUIV_ATTRIBUTE = "http-equiv"
CONTENT_ATTRIBUTE = "content"
PROPERTY_ATTRIBUTE = "property"
HREF_ATTRIBUTE = "href"
REFRESH_EQUIV = "refresh"
REFRESH_URL_KEY = "url"
REFRESH_SEP = ";"
OG_TITLE_PROPERTY = "og:title"
QUOTE_CHARACTERS = "\"'"
SAME_PAGE_NETLOCS = ("",)
OK_STATUS = 200
HTML_CONTENT_TYPE = "text/html"
# A fence, not a budget: the forwarding pages seen so far are 2-14KB, and the
# head is all that is ever needed.
MAX_BODY_BYTES = 64 * 1024
BODY_ENCODING = "utf-8"
DECODE_ERRORS = "replace"
FORWARD_MARKER = "  ?? forwards to"
TITLE_INDENT = "     "
DESTINATION_MESSAGE = "  .. no forward in the page: this is the destination"
# The two kinds of answer this tool can end on: one read out of a wrapper or
# confirmed by a server, one read off a page's shape. They print differently so
# that a glance at the trace says which one reached stdout and the clipboard.
UNWRAPPED_LABEL = "unwrapped:"
INFERRED_LABEL = "inferred:"


class Forward(NamedTuple):
    """Where a page goes next, and how confidently that is known.

    `declared` separates the two ways of knowing. A <meta refresh> is the page
    stating its own destination, and can be followed like a Location header. A
    sole off-site anchor is an inference from the page's shape, and is only ever
    reported.
    """

    url: str
    declared: bool
    title: Optional[str]


class _PageFacts(HTMLParser):
    """Collect the few facts about a page that say where it forwards to."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.refresh = None
        self.title = None
        self.hrefs = []

    def handle_starttag(self, tag, attrs):
        # HTMLParser lowercases tag and attribute names, but not their values.
        attributes = {name: value or "" for name, value in attrs}
        if tag == META_TAG:
            self._read_meta(attributes)
        elif tag == ANCHOR_TAG and attributes.get(HREF_ATTRIBUTE):
            self.hrefs.append(attributes[HREF_ATTRIBUTE])

    def _read_meta(self, attributes):
        if attributes.get(HTTP_EQUIV_ATTRIBUTE, "").lower() == REFRESH_EQUIV:
            self.refresh = attributes.get(CONTENT_ATTRIBUTE, "")
        elif attributes.get(PROPERTY_ATTRIBUTE, "").lower() == OG_TITLE_PROPERTY:
            self.title = attributes.get(CONTENT_ATTRIBUTE)


def _page_facts(html):
    facts = _PageFacts()
    facts.feed(html)
    return facts


def _refresh_url(content, base_url):
    """Read the target out of a <meta refresh> content attribute.

    The attribute reads "<seconds>; url=<target>"; a bare "<seconds>" reloads
    this same page and forwards nowhere. Only the first ";" and the first "="
    are split on, so a target containing either character survives.
    """
    if content is None:
        return None
    _delay, separator, rest = content.partition(REFRESH_SEP)
    if not separator:
        return None
    key, separator, value = rest.strip().partition(KV_SEP)
    if not separator or key.strip().lower() != REFRESH_URL_KEY:
        return None
    target = value.strip().strip(QUOTE_CHARACTERS)
    return ul.urljoin(base_url, target) if target else None


def meta_refresh_target(html, base_url):
    """Return the URL a <meta refresh> forwards to, or None if there is none."""
    return _refresh_url(_page_facts(html).refresh, base_url)


def _sole_offsite(facts, base_url):
    host = ul.urlsplit(base_url).netloc
    targets = []
    for href in facts.hrefs:
        target = ul.urljoin(base_url, href)
        if ul.urlsplit(target).netloc not in SAME_PAGE_NETLOCS + (host,) and target not in targets:
            targets.append(target)
    return targets[0] if len(targets) == 1 else None


def sole_offsite_anchor(html, base_url):
    """Return the single off-site URL a page links to, or None.

    One link off the page is a forward; none is a page rendering its own
    content, and several is a choice this tool has no basis for making. The
    markup is parsed rather than pattern-matched, and the difference is not
    academic: a landing page names a dozen URLs inside its scripts and links to
    none of them, so matching on text offers a confident wrong answer.
    """
    return _sole_offsite(_page_facts(html), base_url)


def og_title(html):
    """Return the page's og:title - its own claim about what it is showing."""
    return _page_facts(html).title


def forwarding_target(html, base_url):
    """Return where this page forwards to, or None if it does not forward.

    A <meta refresh> stands on its own, since the page states its destination. A
    sole off-site anchor is trusted only when an og:title corroborates it: a
    page about to show you somewhere else describes where that is, while a
    paywall or an error page that happens to carry one link does not.
    """
    facts = _page_facts(html)
    declared = _refresh_url(facts.refresh, base_url)
    if declared is not None:
        return Forward(declared, True, facts.title)
    inferred = _sole_offsite(facts, base_url)
    if inferred is not None and facts.title:
        return Forward(inferred, False, facts.title)
    return None


def worth_reading(response):
    """True when a settled response is a page that could still forward on.

    Anything else - an error page, a PDF, a response that will not say what it
    is - stays unopened.
    """
    if response.status_code != OK_STATUS:
        return False
    content_type = response.headers.get(CONTENT_TYPE_HEADER, "")
    return content_type.split(CONTENT_TYPE_SEP)[0].strip().lower() == HTML_CONTENT_TYPE


def bounded_text(chunks, limit=MAX_BODY_BYTES):
    """Decode at most `limit` bytes of a streamed body.

    The bytes are collected first and decoded once at the end, so a character
    split across two chunks survives. The cap is what keeps "read the page" from
    becoming "download the page"; the charset the page declares is ignored,
    which can garble a title from a legacy-encoded page but never a URL.
    """
    read = bytearray()
    for chunk in chunks:
        read += chunk[: limit - len(read)]
        if len(read) >= limit:
            break
    return read.decode(BODY_ENCODING, errors=DECODE_ERRORS)


def _forwarding_hop(html, url, report, note=None):
    """Return the hop a page declares, or report what it shows and settle.

    A <meta refresh> is the page stating its destination, so it is returned
    silently and follow() prints it exactly as it prints a Location. A sole
    off-site anchor is this tool reading the markup rather than the page saying
    so, and is never returned as a hop: followed, a wrong guess would be
    indistinguishable from a fact, and nothing downstream could tell the
    difference. It is shown, and handed to `note`, which carries it past
    follow() to the one place that decides what to do with it.
    """
    forward = forwarding_target(html, url)
    if forward is None:
        report(DESTINATION_MESSAGE)
        return None
    if forward.declared:
        return forward.url
    candidate = unwrap(forward.url)
    report(f"{FORWARD_MARKER} {candidate}")
    if forward.title:
        report(f"{TITLE_INDENT}{forward.title}")
    if note is not None:
        note(candidate)
    return None


def _clipboard():
    import pyperclip

    return pyperclip


def socks_port_open(host, port):
    """True when something is accepting TCP connections there."""
    with socket.socket() as probe:
        probe.settimeout(PORT_PROBE_TIMEOUT)
        return probe.connect_ex((host, port)) == 0


def follow(url, resolve, max_hops=MAX_FOLLOW_HOPS, report=None):
    """Alternate textual unwrapping with network hops until the URL settles.

    `resolve(url)` returns the Location of a single redirect hop, or None when
    the URL does not redirect. unwrap() runs before every request as well as
    after it, so a shortener is never handed the click id that led us to it.
    `report`, if given, is called with each cleaned URL as it is produced.
    """
    url = unwrap(url)
    seen = {url}
    for _ in range(max_hops):
        target = resolve(url)
        if target is None:
            break
        url = unwrap(ul.urljoin(url, target))
        if report is not None:
            report(f"  => {url}")
        if url in seen:
            break
        seen.add(url)
    return url


def _proxy_url(socks_port):
    return f"{SOCKS_SCHEME}://{TOR_SOCKS_HOST}:{socks_port}"


def _hop(response, report):
    """Return the Location, or report why this URL settled and return None.

    Only the status and headers are read here. A page that forwards on through
    <meta refresh> or JavaScript settles at this layer, and the caller decides
    whether its body is worth opening to find out which of the two it is.
    """
    location = (
        response.headers.get(LOCATION_HEADER)
        if response.status_code in REDIRECT_STATUSES
        else None
    )
    if location is None:
        content_type = response.headers.get(CONTENT_TYPE_HEADER, UNKNOWN_CONTENT_TYPE)
        report(f"  .. {response.status_code} {content_type.split(CONTENT_TYPE_SEP)[0]}, no redirect")
    return location


def _read_forward(client, url, report, note=None):
    """Open a capped prefix of a page and see whether it forwards on."""
    with client.stream("GET", url) as streamed:
        html = bounded_text(streamed.iter_bytes())
    return _forwarding_hop(html, url, report, note)


def tor_resolver(socks_port, report, note=None):
    """Return a resolve(url) that reads one hop through tor.

    Headers alone answer the question for a redirect. A page that settles is
    opened only when it is a 200 that says it is HTML, and then only far enough
    to see whether it forwards on.
    """
    import httpx

    client = httpx.Client(
        proxy=_proxy_url(socks_port),
        timeout=REQUEST_TIMEOUT,
        follow_redirects=False,
        headers={"User-Agent": USER_AGENT},
    )

    def resolve(url):
        response = client.head(url)
        if response.status_code in METHOD_NOT_ALLOWED:
            # Some hosts refuse HEAD. Stream the GET so nothing is read beyond
            # what the body pass below asks for.
            with client.stream("GET", url) as streamed:
                location = _hop(streamed, report)
                if location is not None:
                    return location
                if not worth_reading(streamed):
                    return None
                return _forwarding_hop(bounded_text(streamed.iter_bytes()), url, report, note)
        location = _hop(response, report)
        if location is not None:
            return location
        return _read_forward(client, url, report, note) if worth_reading(response) else None

    return resolve


class TorCheckFailed(Exception):
    """The check service did not confirm that a SOCKS port fronts tor."""


def tor_exit_ip(socks_port, attempts=TOR_CHECK_ATTEMPTS):
    """Return the exit IP the check service reports, or raise TorCheckFailed."""
    import httpx

    last_error = "no attempt was made"
    for _ in range(attempts):
        try:
            with httpx.Client(proxy=_proxy_url(socks_port), timeout=REQUEST_TIMEOUT) as client:
                payload = client.get(TOR_CHECK_URL).json()
        except (httpx.HTTPError, ValueError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            continue
        if not payload.get("IsTor"):
            raise TorCheckFailed(f"{TOR_CHECK_URL} reports this is not tor")
        return payload.get("IP")
    raise TorCheckFailed(last_error)


def confirm_tor(socks_port, we_started_it, report, check=tor_exit_ip):
    """Establish that socks_port fronts tor, or refuse to use it.

    A port this run did not start could be any proxy, so a check that fails is
    fatal. An instance we launched ourselves was watched all the way to
    "Bootstrapped 100%", so there a failed check is only worth a warning.
    """
    try:
        report(f"tor: exit node {check(socks_port)}")
    except TorCheckFailed as exc:
        if not we_started_it:
            raise SystemExit(
                TOR_UNCONFIRMED_MESSAGE.format(
                    host=TOR_SOCKS_HOST, port=socks_port, url=TOR_CHECK_URL, reason=exc
                )
            ) from exc
        report(f"tor: bootstrapped here, but the exit check did not complete ({exc})")


def start_tor(report):
    """Launch a private tor instance and return its process."""
    import stem.process

    return stem.process.launch_tor_with_config(
        config={
            "SocksPort": str(TOR_LAUNCH_PORT),
            "DataDirectory": tempfile.mkdtemp(prefix="urldecode-tor-"),
        },
        completion_percent=TOR_BOOTSTRAP_PERCENT,
        init_msg_handler=report,
        timeout=TOR_BOOTSTRAP_TIMEOUT,
        take_ownership=True,
    )


def tor_socks_port(report):
    """Find a bootstrapped tor, starting a private one if none is running.

    Returns (socks_port, process or None); the caller kills a process it owns.
    Exits with instructions when tor is neither running nor installed.
    """
    if socks_port_open(TOR_SOCKS_HOST, TOR_SOCKS_PORT):
        report(f"tor: using what is already on {TOR_SOCKS_HOST}:{TOR_SOCKS_PORT}")
        return TOR_SOCKS_PORT, None
    if shutil.which(TOR_COMMAND) is None:
        raise SystemExit(TOR_MISSING_MESSAGE)
    report(f"tor: nothing on port {TOR_SOCKS_PORT}, starting a private instance")
    return TOR_LAUNCH_PORT, start_tor(report)


def resolve_through_tor(url, report, note=None):
    """Follow url to its destination over tor, cleaning it at every hop."""
    port, started = tor_socks_port(report)
    try:
        confirm_tor(port, started is not None, report)
        resolve = tor_resolver(port, report, note)

        def reporting_resolve(target):
            location = resolve(target)
            if location is None:
                # The last URL is probed too, to learn that it settles. Saying
                # so would just repeat the result printed below.
                return None
            # The raw Location. follow() reports the cleaned form right after,
            # so every "->" is answered by a "=>" on the line beneath it.
            report(f"  -> {location}")
            return location

        return follow(url, reporting_resolve, report=report)
    finally:
        if started is not None:
            started.kill()


def chosen_result(settled, inferred, trust):
    """Return the URL to print, and the label that says what kind it is.

    A candidate read off a page's shape is the answer when there is one, since
    it is the answer the user came for - but never silently: it is labelled for
    what it is, and --no-trust-inferred keeps the URL that actually settled.
    """
    if trust and inferred is not None:
        return inferred, INFERRED_LABEL
    return settled, UNWRAPPED_LABEL


def _parser():
    """The CLI, built apart from main() so its defaults can be read in a test."""
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog=FOLLOW_EPILOG.format(launch_port=TOR_LAUNCH_PORT),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("url", nargs="?", help="URL to unwrap (default: read the clipboard)")
    parser.add_argument(
        "-f",
        "--follow",
        action="store_true",
        help="resolve shortener redirects over the network, through tor (see below)",
    )
    parser.add_argument(
        "-T",
        "--no-trust-inferred",
        dest="trust_inferred",
        action="store_false",
        help="keep the URL that settled, even when a page names where it forwards (the ?? line)",
    )
    parser.add_argument("-n", "--no-copy", action="store_true", help="do not copy the result to the clipboard")
    parser.add_argument("-q", "--quiet", action="store_true", help="print only the resulting URL")
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)

    def report(message):
        """The trace goes to stderr; stdout carries the resulting URL alone."""
        if not args.quiet:
            print(message, file=sys.stderr)

    if args.url:
        url, source = args.url, "command line"
    else:
        url, source = _clipboard().paste(), "clipboard"

    # One vocabulary for both stages: a URL that was read or asked, and "->" the
    # raw thing it yielded, tracking included. Extracting a target from a wrapper
    # and following a redirect are the same move, so they read the same way.
    report(f"from {source}: {url}")
    target = unredirect(url)
    if target != url:
        report(f"  -> {target}")

    inferred = None

    def note(candidate):
        """Remember a forward this tool read off a page but did not follow."""
        nonlocal inferred
        inferred = candidate

    cleaned = unwrap(url)
    if args.follow:
        # Before tor is even started: this, not the line above, is what will go
        # over the wire.
        report(f"  => {cleaned}")
        settled = resolve_through_tor(url, report, note)
    else:
        settled = cleaned

    result, label = chosen_result(settled, inferred, args.trust_inferred)
    report(label)
    print(result)

    if not args.no_copy:
        _clipboard().copy(result)
        report("(copied to clipboard)")


if __name__ == "__main__":
    main()
