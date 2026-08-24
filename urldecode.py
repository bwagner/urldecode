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


def strip_tracking(url):
    """Drop tracking parameters, keeping every other parameter byte-identical."""
    parts = ul.urlsplit(url)
    kept = [pair for name, _value, pair in _params(parts.query) if not is_tracking(name)]
    return ul.urlunsplit(parts._replace(query=PARAM_SEP.join(kept)))


def unwrap(url):
    """Return the real destination of url, without tracking parameters."""
    return strip_tracking(unredirect(url))


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

    Only the status and headers are read, never the body, so a page that
    redirects through JavaScript or <meta refresh> settles here. Saying so is
    all that can honestly be said without downloading it.
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


def tor_resolver(socks_port, report):
    """Return a resolve(url) that reads one redirect hop through tor."""
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
            # Some hosts refuse HEAD. Stream the GET so the body is never read.
            with client.stream("GET", url) as streamed:
                return _hop(streamed, report)
        return _hop(response, report)

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


def resolve_through_tor(url, report):
    """Follow url to its destination over tor, cleaning it at every hop."""
    port, started = tor_socks_port(report)
    try:
        confirm_tor(port, started is not None, report)
        resolve = tor_resolver(port, report)

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


def main(argv=None):
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
    parser.add_argument("-n", "--no-copy", action="store_true", help="do not copy the result to the clipboard")
    parser.add_argument("-q", "--quiet", action="store_true", help="print only the resulting URL")
    args = parser.parse_args(argv)

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

    cleaned = unwrap(url)
    if args.follow:
        # Before tor is even started: this, not the line above, is what will go
        # over the wire.
        report(f"  => {cleaned}")
        result = resolve_through_tor(url, report)
    else:
        result = cleaned

    report("unwrapped:")
    print(result)

    if not args.no_copy:
        _clipboard().copy(result)
        report("(copied to clipboard)")


if __name__ == "__main__":
    main()
