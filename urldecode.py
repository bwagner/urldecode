#!/usr/bin/env python
import re
import sys
import urllib.parse as ul

import pyperclip


def urlescape(url):
    return ul.unquote_plus(url)


def unredirect(url):
    return url[re.search("https?", url[1:]).start() + 1 :]


def unparametrize(url):
    return url[: url.index("?")] if "?" in url else url


def main():
    if len(sys.argv) == 1:
        o = pyperclip.paste()
        frm = "clipboard"
    else:
        o = sys.argv[1]
        frm = "command line"
    print(f"original from {frm}:\n{o}\n")
    s = urlescape(o)
    print(f"escaped:\n{s}\n")
    t = unredirect(s)
    print(f"unredirected:\n{t}\n")
    p = unparametrize(t)
    print(f"unparametrized (is now in clipbord):\n{p}\n")
    pyperclip.copy(p)


if __name__ == "__main__":
    main()
