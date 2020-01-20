#!/usr/bin/env python3
import sys
import re
import urllib.parse as ul
print()
s = ul.unquote_plus(sys.argv[1])
print("escaped:")
print(s)
print()
i = re.search("https?", s[1:]).start() + 1
t = s[i:]
print("unredirected:")
print(t)
print()
i = t.index('?');
u = t[0:i]
print("unparameterized:")
print(u)
print()
