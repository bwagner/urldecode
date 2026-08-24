# urldecode

Unwrap a redirector link and strip its tracking parameters, leaving the URL the
link actually points at.

```console
$ urldecode.py -q 'https://l.facebook.com/l.php?u=https%3A%2F%2Fmrf.lu%2F2sW97%3Ffbclid%3DIwZXh0bgNhZW0Example&h=AT0Example&c[0]=AUDExample'
https://mrf.lu/2sW97
```

## Install

The script is self-contained: the [uv](https://docs.astral.sh/uv/) shebang
declares its own dependency, so there is nothing to install into a virtualenv.
Clone the repo and put it on your `PATH`:

```bash
git clone https://github.com/bwagner/urldecode.git
ln -s "$PWD/urldecode/urldecode.py" ~/bin/urldecode.py
```

Requires `uv`. The first run installs `pyperclip` into uv's cache.

## Usage

```
usage: urldecode.py [-h] [-f] [-n] [-q] [url]

positional arguments:
  url            URL to unwrap (default: read the clipboard)

options:
  -h, --help     show this help message and exit
  -f, --follow   resolve shortener redirects over the network, through tor
                 (see below)
  -n, --no-copy  do not copy the result to the clipboard
  -q, --quiet    print only the resulting URL

--follow needs tor and never falls back to a direct connection:

  * An existing tor on 127.0.0.1:9050 is used, once check.torproject.org
    confirms it really is tor. Run "tor" in another terminal to have one; it
    stays up across runs, so repeated lookups skip the bootstrap wait.
  * Otherwise a private instance is started on 9250, its bootstrap is
    streamed, and it is stopped again on exit.
  * If tor is not installed, it aborts with installation instructions.
```

With no argument it reads the clipboard, so the usual gesture is to copy a link
and run `urldecode.py` with no arguments: the unwrapped URL is printed and
copied back, ready to paste.

By default it traces what it did:

```console
$ urldecode.py -n 'https://l.facebook.com/l.php?u=https%3A%2F%2Fmrf.lu%2F2sW97%3Ffbclid%3DIwZXh0bgNhZW0Example&h=AT0Example'
from command line: https://l.facebook.com/l.php?u=https%3A%2F%2Fmrf.lu%2F2sW97%3Ffbclid%3DIwZXh0bgNhZW0Example&h=AT0Example
  -> https://mrf.lu/2sW97?fbclid=IwZXh0bgNhZW0Example
unwrapped:
https://mrf.lu/2sW97
```

An indented `->` is the raw URL that came back from the line above it, tracking
included, and with `--follow` a `=>` beneath it shows the cleaned form that will
actually be requested. **The trace goes
to stderr and the resulting URL to stdout**, so piping gives you the URL alone
whether or not you pass `-q`; `-q` silences the trace itself.

## What it does

**Unwraps redirectors.** Any URL carrying its target in a query parameter is
unwrapped, so `l.facebook.com/l.php?u=...` and `google.com/url?q=...` both work
without special-casing either. Nested wrappers are followed up to
`MAX_UNWRAP_DEPTH`. A URL that is not a redirector is returned unchanged.

**Strips tracking parameters, and only those.** Parameters are matched by name
against `TRACKING_PARAMS` (`fbclid`, `gclid`, `msclkid`, `igshid`, `mc_eid`, ...),
the `utm_` prefix, and the `clid`/`cid` suffixes. The suffix rule catches
per-publisher click ids that no list can keep up with, such as the `mrfcid` used
by nzz.ch's `mrf.lu` shortener; the price is that a legitimate `cid` (category,
content or customer id) is stripped too. Everything else survives, so links that
need their query string keep working:

```console
$ urldecode.py -q 'https://www.youtube.com/watch?v=dQw4w9WgXcQ&utm_source=newsletter&fbclid=ABC'
https://www.youtube.com/watch?v=dQw4w9WgXcQ

$ urldecode.py -q 'https://www.google.com/url?q=https%3A%2F%2Fexample.com%2Fa&sa=D'
https://example.com/a
```

Surviving parameters keep their raw text rather than being decoded and
re-encoded, the fragment is preserved, and decoding uses `unquote` rather than
`unquote_plus` so a literal `+` stays a `+`.

## Following shorteners (`--follow`)

Everything above is pure string manipulation: no network, no requests, nothing
observable. That only works because a wrapper like `l.php?u=...` *carries* its
destination. A shortener such as `mrf.lu/2sW97` does not - the path is a
database key, and the only way to learn where it points is to ask the server.

`--follow` asks, through tor, and keeps unwrapping until the URL settles:

```console
$ urldecode.py --follow 'https://l.facebook.com/l.php?u=https%3A%2F%2Fmrf.lu%2F2sW97%3Ffbclid%3DIwZXh0bgNhZW0Example&h=AT0Example'
from command line: https://l.facebook.com/l.php?u=https%3A%2F%2Fmrf.lu%2F2sW97%3Ffbclid%3DIwZXh0bgNhZW0Example&h=AT0Example
  -> https://mrf.lu/2sW97?fbclid=IwZXh0bgNhZW0Example
  => https://mrf.lu/2sW97
tor: nothing on port 9050, starting a private instance
tor: exit node 185.220.101.19
  -> https://www.example.com/feuilleton/some-article-ld.123?utm_campaign=mrf-facebook-x&utm_source=facebook&utm_medium=social&mrfcid=20260101ExampleMrfClickId
  => https://www.example.com/feuilleton/some-article-ld.123
unwrapped:
https://www.example.com/feuilleton/some-article-ld.123
```

Every stage reads the same way and comes in pairs: `->` is the raw URL that
came back, `=>` is the same URL cleaned, and the `=>` is what gets requested
next. The first pair is printed before tor is even started, so you can see what
will go over the wire before anything does. The wrapper handed over an
`fbclid`, the shortener handed back four more parameters including the `mrfcid`
that no exact-name list would have known about, and none of them survived.

Each hop is a HEAD request (falling back to a streamed GET where HEAD is
refused), following `Location` without ever reading a page body, bounded by
`MAX_FOLLOW_HOPS` and stopping early on a redirect cycle. **Tracking is
stripped before every request**, so the shortener is never handed the `fbclid`
that led you to it, and the text-level unwrap runs again after each hop, since
hops routinely land on URLs carrying fresh `utm_*` junk.

### tor

`--follow` needs tor and will not fall back to a direct connection. Three cases:

1. **Something already on `127.0.0.1:9050`** - it is used, after
   `check.torproject.org` confirms it really is tor. This run did not start it,
   so it could be any proxy: if the check cannot be completed the request is
   refused rather than sent through something unknown. A failed check on an
   instance started here is only a warning, since it was watched all the way to
   `Bootstrapped 100%`. The check is retried, because the first request through
   a fresh circuit routinely dies with an SSL EOF and the next one succeeds.
2. **No tor running, `tor` installed** - a private instance is started on port
   9250 with its own temporary `DataDirectory`, so it never collides with a
   system or brew-managed tor. Bootstrap output is streamed, and the process is
   killed when the script exits. That bootstrap costs several seconds on every
   invocation, so for repeated lookups it is worth running `tor` once in a
   spare terminal and letting case 1 apply.
3. **tor not installed** - it aborts with instructions (`brew install tor`, and
   the difference between `brew services run` and `start`).

Requests are proxied with `socks5h://`, so DNS is resolved by the exit node
rather than locally.

### What this does and does not hide

Tor hides your IP from the shortener and the destination. It does not make the
click disappear: resolving a tracking link is exactly the event the link exists
to record, and `--follow` makes spending one a single keystroke. Some
shorteners also vary their destination by geography, so an exit node elsewhere
may legitimately resolve to something different than you would see.


## Library use

```python
from urldecode import unwrap, unredirect, strip_tracking

unwrap("https://l.facebook.com/l.php?u=...")   # unredirect + strip_tracking
unredirect("https://l.facebook.com/l.php?u=...")  # target only, tracking intact
strip_tracking("https://example.com/a?id=7&fbclid=X")  # https://example.com/a?id=7
```

`follow(url, resolve, max_hops=...)` takes its resolver as a parameter -
`resolve(url)` returns the `Location` of one hop or `None` - so the chain logic
is testable without a network or a mocking library:

```python
follow("https://l.facebook.com/l.php?u=...", {"https://mrf.lu/2sW97": "https://example.com/a"}.get)
```

`pyperclip` is imported lazily, so importing the module does not require it.

## Tests

```bash
uv run --with pytest pytest -q
uvx ruff check .
```
