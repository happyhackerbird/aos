#!/usr/bin/env python3
"""seed.py '<json list>': post each row to the fake bus's /_probe/add (from, body, kind)."""
import json, sys, urllib.request
for row in json.loads(sys.argv[1]):
    req = urllib.request.Request("http://127.0.0.1:18090/_probe/add", data=json.dumps(row).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        print("seeded", json.loads(r.read())["id"])
