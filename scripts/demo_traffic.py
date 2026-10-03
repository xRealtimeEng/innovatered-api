"""Send light, made-up traffic at a local red-api so the dashboard graphs have something to show.

Usage: python scripts/demo_traffic.py [base_url]   (default http://127.0.0.1:8010)
"""
import json
import random
import sys
import time
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8010"
UA = {"User-Agent": "red-demo-traffic"}


def call(method, path, body=None, token=None):
    headers = dict(UA)
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, {}
    except Exception:
        return 0, {}


users = [(f"tester{i}@innovatered.local", f"localtest{i}{i}{i}") for i in (1, 2, 3)]
tokens = []
while True:
    r = random.random()
    if r < 0.35:
        call("GET", "/health")
    elif r < 0.6:
        call("GET", "/db/ping")
    elif r < 0.7:
        email, pw = random.choice(users)
        s, j = call("POST", "/auth/login", {"email": email, "password": pw})
        if s == 200 and j.get("token"):
            tokens = (tokens + [j["token"]])[-6:]
    elif r < 0.85 and tokens:
        call("GET", "/auth/me", token=random.choice(tokens))
    elif r < 0.9:
        call("POST", "/auth/login", {"email": random.choice(users)[0], "password": "wrong-password"})
    elif r < 0.93:
        call("GET", random.choice(["/missing", "/api/v2/things", "/admin"]))
    elif r < 0.935:
        call("POST", "/contact", {"name": "Demo Visitor", "email": "visitor@example.com",
                                  "company": "Demo", "note": "Made-up message from the demo traffic script."})
    else:
        call("GET", "/health")
    # gentle waves so the graphs move
    time.sleep(random.uniform(0.15, 0.6) * (1.5 + __import__("math").sin(time.time() / 40)))
