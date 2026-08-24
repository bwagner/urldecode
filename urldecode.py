#!/usr/bin/env -S uv run --script
# /// script
# dependencies = ["pyperclip"]
# ///
"""Unwrap a redirector link (Facebook, Google, ...) and strip tracking parameters."""

import argparse
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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", nargs="?", help="URL to unwrap (default: read the clipboard)")
    parser.add_argument("-n", "--no-copy", action="store_true", help="do not copy the result to the clipboard")
    parser.add_argument("-q", "--quiet", action="store_true", help="print only the resulting URL")
    args = parser.parse_args(argv)

    if args.url:
        url, source = args.url, "command line"
    else:
        url, source = _clipboard().paste(), "clipboard"

    result = unwrap(url)

    if not args.quiet:
        print(f"original from {source}:\n{url}\n")
        target = unredirect(url)
        if target != url:
            print(f"redirect target:\n{target}\n")
        print("unwrapped:")
    print(result)

    if not args.no_copy:
        _clipboard().copy(result)
        if not args.quiet:
            print("\n(copied to clipboard)")


if __name__ == "__main__":
    main()
