# urldecode

Find out where a link really goes, and get there without the tracking.

Unwraps redirector links that carry their destination in a query parameter -
Facebook's `l.php?u=`, Google's `/url?q=`, and anything shaped like them - as
pure text, and strips tracking parameters, including per-publisher click ids no
denylist knows about. With `--follow` it also resolves shorteners whose target
only the server knows, such as bit.ly, tinyurl or t.co, over tor - cleaning the
URL *before* each request, so the shortener never receives the click id that
sent you there.

No domain lists: any query parameter holding a URL, any redirect.

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
usage: urldecode.py [-h] [-f] [-t] [-n] [-q] [url]

positional arguments:
  url                   URL to unwrap (default: read the clipboard)

options:
  -h, --help            show this help message and exit
  -f, --follow          resolve shortener redirects over the network, through
                        tor (see below)
  -t, --trust-inferred  with --follow, take a page's inferred forward (the ??
                        line) as the result
  -n, --no-copy         do not copy the result to the clipboard
  -q, --quiet           print only the resulting URL

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

**Strips tracking parameters, and only those.** Parameters are matched by name,
case-insensitively, against `TRACKING_PARAMS` (`fbclid`, `gclid`, `msclkid`,
`igshid`, `mc_eid`, ...), the `utm_` prefix, and the `clid`/`cid` suffixes.
Publishers capitalise these inconsistently, so `FBCLID` goes the same way as
`fbclid`. The suffix rule catches
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
re-encoded, and decoding uses `unquote` rather than `unquote_plus` so a literal
`+` stays a `+`.

Fragments are kept, with one exception. A fragment never reaches the server, so
a marker in one is read by the page's own JavaScript on arrival - `#Echobox=...`
does the same job as an `fbclid` by another route. A fragment that reads as
`key=value` is therefore matched against the same rule as a query parameter and
dropped if it is tracking:

```console
$ urldecode.py -q -n 'https://www.example.com/story-123?utm_source=Facebook#Echobox=1787588927'
https://www.example.com/story-123
```

An ordinary anchor (`#section-3`) has no `=` and cannot be read as a name, so it
is never at risk, and a functional pair (`#t=120` for a video timestamp,
`#page=5`, `#:~:text=...`) survives because its name is not tracking.

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
  .. 200 text/html, no redirect
  .. no forward in the page: this is the destination
unwrapped:
https://www.example.com/feuilleton/some-article-ld.123
```

Every stage reads the same way and comes in pairs: `->` is the raw URL that
came back, `=>` is the same URL cleaned, and the `=>` is what gets requested
next. A final `..` line reports the status and content type of the URL that did
not redirect, so the chain ends by saying why it ended rather than just
stopping - a `404` or an unexpected content type shows up instead of being
silently presented as the destination. The first pair is printed before tor is even started, so you can see what
will go over the wire before anything does. The wrapper handed over an
`fbclid`, the shortener handed back four more parameters including the `mrfcid`
that no exact-name list would have known about, and none of them survived.

Each hop is a HEAD request (falling back to a streamed GET where HEAD is
refused), following `Location`, bounded by `MAX_FOLLOW_HOPS` and stopping early
on a redirect cycle. **Tracking is stripped before every request**, so the
shortener is never handed the `fbclid` that led you to it, and the text-level
unwrap runs again after each hop, since hops routinely land on URLs carrying
fresh `utm_*` junk.

### When there is no `Location` to follow

Some shorteners answer `200` with a page that forwards you on a moment later,
often showing an ad or a countdown while it does. There is no redirect header
for that, so the chain settles at the shortener while the real destination sits
in the HTML.

When a hop settles on a `200` that says it is `text/html`, and only then, up to
`MAX_BODY_BYTES` (64KB) of the page is read. It is parsed as markup, never
executed, and nothing it references is fetched. What happens next depends on how
the page says where it goes.

**It declares a `<meta http-equiv="refresh">`.** The page states its own
destination, so it is followed like any `Location` and printed as the usual
`->` / `=>` pair.

**It carries a single off-site link, with an `og:title` describing it.** That is
this tool reading the page's shape, not the page saying so, so it is shown and
not followed:

```console
  .. 200 text/html, no redirect
  ?? forwards to https://www.example.com/some-article
     The headline the page says it is about to show you
```

The result on stdout and the clipboard stays the URL that actually settled;
copy the `??` line yourself if it looks right. Taking it automatically would
make a wrong guess indistinguishable from a fact, and unlike stopping early,
that failure would be silent - the output shape is identical whether the guess
was right or wrong.

Which is why it is available, but only by asking. `-t/--trust-inferred` makes
the candidate the result, under a label that says where it came from:

```console
$ urldecode.py --follow --trust-inferred
  .. 200 text/html, no redirect
  ?? forwards to https://www.example.com/some-article
     The headline the page says it is about to show you
inferred:
https://www.example.com/some-article
(copied to clipboard)
```

`inferred:` in place of `unwrapped:` is the whole difference: one glance says
whether the URL you just copied was read out of a wrapper and confirmed by a
server, or read off a page's shape. The candidate is still never requested -
`--trust-inferred` changes what is reported as the answer, not what goes over
the wire - and the flag does nothing without `--follow`, which is what reads
pages in the first place.

**Neither, or several links with no way to choose.** The page is reported as the
destination, which is an answer rather than the absence of one:

```console
  .. 200 text/html, no redirect
  .. no forward in the page: this is the destination
```

That last line is what separates "this is where you were going" from "it goes on
and I cannot see how" - two situations the headers alone cannot tell apart.

A page whose destination exists only inside its JavaScript, computed or fetched
rather than written in the markup, is out of scope. Reading it properly needs a
browser engine, which would end the single-file script, and pattern-matching
script text works on the easy cases and fails silently on the rest.

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

Reading a forwarding page is pure too - HTML in, URL out - so it needs no
network either:

```python
from urldecode import forwarding_target

forwarding_target(html, "https://short.example/abc")
# Forward(url="https://news.example/story", declared=False, title="The headline")
# ... or None, when the page does not forward on
```

`declared` is provenance, not confidence: `True` means the page said so in a
`<meta refresh>`, `False` means it was inferred from a sole off-site link.
`meta_refresh_target(html, base)`, `sole_offsite_anchor(html, base)` and
`og_title(html)` are available individually.

`pyperclip` is imported lazily, so importing the module does not require it.

## Tests

```bash
uv run --with pytest pytest -q
uvx ruff check .
```

## License

MIT - see [LICENSE](LICENSE).
