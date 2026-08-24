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
usage: urldecode.py [-h] [-n] [-q] [url]

positional arguments:
  url            URL to unwrap (default: read the clipboard)

options:
  -h, --help     show this help message and exit
  -n, --no-copy  do not copy the result to the clipboard
  -q, --quiet    print only the resulting URL
```

With no argument it reads the clipboard, so the usual gesture is to copy a link
and run `urldecode.py` with no arguments: the unwrapped URL is printed and
copied back, ready to paste.

By default it shows its work:

```console
$ urldecode.py -n 'https://l.facebook.com/l.php?u=https%3A%2F%2Fmrf.lu%2F2sW97%3Ffbclid%3DIwZXh0bgNhZW0Example&h=AT0Example'
original from command line:
https://l.facebook.com/l.php?u=https%3A%2F%2Fmrf.lu%2F2sW97%3Ffbclid%3DIwZXh0bgNhZW0Example&h=AT0Example

redirect target:
https://mrf.lu/2sW97?fbclid=IwZXh0bgNhZW0Example

unwrapped:
https://mrf.lu/2sW97
```

`-q` prints the bare URL, which makes it pipeable.

## What it does

**Unwraps redirectors.** Any URL carrying its target in a query parameter is
unwrapped, so `l.facebook.com/l.php?u=...` and `google.com/url?q=...` both work
without special-casing either. Nested wrappers are followed up to
`MAX_UNWRAP_DEPTH`. A URL that is not a redirector is returned unchanged.

**Strips tracking parameters, and only those.** Parameters are matched by name
against `TRACKING_PARAMS` (`fbclid`, `gclid`, `msclkid`, `igshid`, `mc_eid`, ...)
plus the `utm_` prefix. Everything else survives, so links that need their query
string keep working:

```console
$ urldecode.py -q 'https://www.youtube.com/watch?v=dQw4w9WgXcQ&utm_source=newsletter&fbclid=ABC'
https://www.youtube.com/watch?v=dQw4w9WgXcQ

$ urldecode.py -q 'https://www.google.com/url?q=https%3A%2F%2Fexample.com%2Fa&sa=D'
https://example.com/a
```

Surviving parameters keep their raw text rather than being decoded and
re-encoded, the fragment is preserved, and decoding uses `unquote` rather than
`unquote_plus` so a literal `+` stays a `+`.

## Library use

```python
from urldecode import unwrap, unredirect, strip_tracking

unwrap("https://l.facebook.com/l.php?u=...")   # unredirect + strip_tracking
unredirect("https://l.facebook.com/l.php?u=...")  # target only, tracking intact
strip_tracking("https://example.com/a?id=7&fbclid=X")  # https://example.com/a?id=7
```

`pyperclip` is imported lazily, so importing the module does not require it.

## Tests

```bash
uv run --with pytest pytest -q
uvx ruff check .
```
