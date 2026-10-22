#!/usr/bin/env python3
"""Print a digest of schedule.bin that ignores its build timestamp.

The header stores built_unix at bytes 16..20, so two builds of the same feed
differ only there. Comparing digests tells whether the timetable changed.
"""
import hashlib
import sys

BUILT_UNIX = slice(16, 20)

data = bytearray(open(sys.argv[1], "rb").read())
data[BUILT_UNIX] = b"\0\0\0\0"
print(hashlib.sha256(data).hexdigest())
