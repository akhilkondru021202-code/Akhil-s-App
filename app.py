#!/usr/bin/env python3
"""
PriceScope — compare a product's price across online stores.

Live prices come from Google Shopping via SerpApi (https://serpapi.com),
which aggregates listings from Amazon, Flipkart, Croma, Reliance Digital,
Tata CLiQ, Vijay Sales and thousands of other stores.

Run:
    1. Get a free API key at https://serpapi.com/manage-api-key
    2. export SERPAPI_KEY=your_key        (Windows: set SERPAPI_KEY=your_key)
    3. python app.py
    4. Open http://localhost:8000

No key? It still starts in DEMO mode with clearly labelled sample data,
so you can try the interface.

Only the Python standard library is used — nothing to install.
"""

import json
import os
import random
import re
import sys
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = int(os.environ.get("PORT", "8000"))
# On a hosting service (Render sets RENDER=true) the app listens publicly,
# the key comes only from the SERPAPI_KEY setting, and visitors can't change it.
HOSTED = bool(os.environ.get("RENDER") or os.environ.get("HOSTED"))
HERE = Path(__file__).resolve().parent
KEY_FILE = HERE / "serpapi_key.txt"


def _load_key():
    k = os.environ.get("SERPAPI_KEY", "").strip()
    if not k and KEY_FILE.exists():
        k = KEY_FILE.read_text("utf-8").strip()
    return k


API_KEY = _load_key()
HISTORY_FILE = HERE / "price_history.json"

COUNTRIES = {
    "in": ("India", "₹", "google.co.in"),
    "us": ("United States", "$", "google.com"),
    "uk": ("United Kingdom", "£", "google.co.uk"),
    "ae": ("UAE", "AED ", "google.ae"),
    "ca": ("Canada", "C$", "google.ca"),
    "au": ("Australia", "A$", "google.com.au"),
    "de": ("Germany", "€", "google.de"),
    "sg": ("Singapore", "S$", "google.com.sg"),
}


# ---------------------------------------------------------------- data

def _num(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = re.search(r"[\d,]+(?:\.\d+)?", str(v))
    return float(m.group(0).replace(",", "")) if m else None


def search_live(query, gl, sort):
    name, cur, domain = COUNTRIES.get(gl, COUNTRIES["in"])
    params = {
        "engine": "google_shopping",
        "q": query,
        "gl": gl,
        "hl": "en",
        "google_domain": domain,
        "api_key": API_KEY,
    }
    url = "https://serpapi.com/search.json?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "PriceScope/1.0"})
    with urllib.request.urlopen(req, timeout=40) as r:
        data = json.loads(r.read().decode("utf-8"))
    if data.get("error"):
        raise RuntimeError(data["error"])

    raw = list(data.get("shopping_results", []))
    for block in data.get("inline_shopping_results", []) or []:
        raw.append(block)
    items = []
    seen = set()
    for r in raw:
        price = _num(r.get("extracted_price")) or _num(r.get("price"))
        if not price:
            continue
        store = (r.get("source") or r.get("seller") or "Unknown store").strip()
        title = (r.get("title") or "").strip()
        key = (store.lower(), title.lower(), price)
        if key in seen:
            continue
        seen.add(key)
        items.append({
            "title": title,
            "store": store,
            "price": price,
            "old_price": _num(r.get("extracted_old_price")) or _num(r.get("old_price")),
            "rating": _num(r.get("rating")),
            "reviews": r.get("reviews"),
            "delivery": r.get("delivery") or "",
            "link": r.get("product_link") or r.get("link") or "",
            "thumbnail": r.get("thumbnail") or "",
        })
    return finish(items, query, gl, sort, demo=False)


DEMO_STORES = ["Amazon.in", "Flipkart", "Croma", "Reliance Digital",
               "Tata CLiQ", "Vijay Sales", "JioMart", "Poorvika"]


def search_demo(query, gl, sort):
    rnd = random.Random(query.lower())
    base = rnd.choice([1499, 2999, 7999, 18999, 54999, 79900])
    items = []
    for store in DEMO_STORES:
        for v in range(rnd.choice([1, 1, 2])):
            p = round(base * rnd.uniform(0.86, 1.14), -1) - 1
            items.append({
                "title": f"{query.title()}" + (" (Renewed)" if v else ""),
                "store": store,
                "price": p,
                "old_price": round(p * rnd.uniform(1.05, 1.3), -1) - 1 if rnd.random() < .5 else None,
                "rating": round(rnd.uniform(3.8, 4.8), 1),
                "reviews": rnd.randint(40, 9000),
                "delivery": rnd.choice(["Free delivery", "Free delivery by tomorrow", "₹49 delivery", ""]),
                "link": "",
                "thumbnail": "",
            })
    return finish(items, query, gl, sort, demo=True)


def finish(items, query, gl, sort, demo):
    if sort == "price_desc":
        items.sort(key=lambda x: -x["price"])
    elif sort == "rating":
        items.sort(key=lambda x: -(x["rating"] or 0))
    else:
        items.sort(key=lambda x: x["price"])

    stores = {}
    for it in items:
        s = stores.setdefault(it["store"], {"store": it["store"], "min": it["price"],
                                            "max": it["price"], "count": 0, "best": it})
        s["count"] += 1
        if it["price"] < s["min"]:
            s["min"], s["best"] = it["price"], it
        s["max"] = max(s["max"], it["price"])
    by_store = sorted(stores.values(), key=lambda s: s["min"])
    prices = [i["price"] for i in items]
    stats = {}
    if prices:
        sp = sorted(prices)
        stats = {"min": sp[0], "max": sp[-1], "median": sp[len(sp) // 2],
                 "avg": sum(sp) / len(sp), "listings": len(sp), "stores": len(by_store)}
    name, cur, _ = COUNTRIES.get(gl, COUNTRIES["in"])
    return {"query": query, "country": name, "currency": cur, "demo": demo,
            "items": items, "by_store": by_store, "stats": stats,
            "fetched_at": time.strftime("%Y-%m-%d %H:%M")}


# ------------------------------------------------------------- history

def load_history():
    try:
        return json.loads(HISTORY_FILE.read_text("utf-8"))
    except Exception:
        return {}


def save_history(h):
    HISTORY_FILE.write_text(json.dumps(h, indent=1, ensure_ascii=False), "utf-8")


def record_snapshot(result):
    """Store the cheapest price per store for this query, once per search."""
    if result.get("demo"):
        return
    h = load_history()
    key = result["query"].strip().lower() + "|" + result["country"]
    entry = h.setdefault(key, {"query": result["query"], "country": result["country"],
                               "currency": result["currency"], "tracked": False, "points": []})
    entry["points"].append({"t": result["fetched_at"],
                            "stores": {s["store"]: s["min"] for s in result["by_store"]}})
    entry["points"] = entry["points"][-200:]
    save_history(h)


# ---------------------------------------------------------------- http

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("  " + (fmt % args) + "\n")

    def _send(self, code, body, ctype="application/json"):
        b = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path in ("/", "/index.html"):
            return self._send(200, INDEX_HTML, "text/html")
        if u.path == "/api/config":
            return self._send(200, json.dumps({
                "demo": not API_KEY, "hosted": HOSTED,
                "countries": {k: v[0] for k, v in COUNTRIES.items()}}))
        if u.path == "/api/search":
            query = (q.get("q", [""])[0]).strip()
            gl = q.get("gl", ["in"])[0]
            sort = q.get("sort", ["price_asc"])[0]
            if not query:
                return self._send(400, json.dumps({"error": "Type a product to search."}))
            try:
                res = search_live(query, gl, sort) if API_KEY else search_demo(query, gl, sort)
                record_snapshot(res)
                return self._send(200, json.dumps(res, ensure_ascii=False))
            except Exception as e:
                return self._send(502, json.dumps({"error": f"Search failed: {e}"}))
        if u.path == "/api/history":
            return self._send(200, json.dumps(load_history(), ensure_ascii=False))
        self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        if u.path == "/api/key":
            global API_KEY
            if HOSTED:
                return self._send(403, json.dumps({"error": "Set SERPAPI_KEY in the hosting settings."}))
            API_KEY = str(body.get("key", "")).strip()
            KEY_FILE.write_text(API_KEY, "utf-8")
            return self._send(200, json.dumps({"ok": True, "demo": not API_KEY}))
        if u.path == "/api/track":
            if HOSTED:
                return self._send(403, json.dumps({"error": "read-only"}))
            h = load_history()
            if body.get("key") in h:
                h[body["key"]]["tracked"] = bool(body.get("tracked"))
                if body.get("delete"):
                    del h[body["key"]]
                save_history(h)
            return self._send(200, json.dumps({"ok": True}))
        self._send(404, json.dumps({"error": "not found"}))


INDEX_HTML = (HERE / "index.html").read_text("utf-8") if (HERE / "index.html").exists() else ""


def main():
    global INDEX_HTML
    if not INDEX_HTML:
        sys.exit("index.html must sit next to app.py")
    mode = "LIVE prices" if API_KEY else "DEMO - paste your SerpApi key in the app for real prices"
    print(f"\n  PriceScope running at http://localhost:{PORT}   [{mode}]\n")
    print("  Keep this window open while you use the app. Close it to stop.\n")
    srv = ThreadingHTTPServer(("0.0.0.0" if HOSTED else "127.0.0.1", PORT), Handler)
    if not HOSTED and os.environ.get("NO_BROWSER") != "1":
        import threading, webbrowser
        threading.Timer(1.0, lambda: webbrowser.open(f"http://localhost:{PORT}")).start()
    srv.serve_forever()


if __name__ == "__main__":
    main()
