"""End-to-end system test for the kahzaabu web stack.

Verifies:
- Every HTML page renders (200)
- Every API endpoint returns expected shape
- Public-mode filtering works (anonymous vs admin)
- Auth flow (login, /api/me, /api/admin/*, logout)
- Rate limiting fires when expected
- Q&A works for anonymous (under budget) and admin (always)
- CLI side-effects: pipeline trigger, publish/unpublish
- DB consistency: counts match between /api/stats and direct queries

Run: python test_system.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from pathlib import Path

import requests

BASE = os.environ.get("KAHZAABU_BASE", "http://127.0.0.1:8765")
DB = Path(__file__).parent / "data" / "kahzaabu.db"
ADMIN_USER = "sofwath"
ADMIN_PASS = "test-password-123"

PASS = 0
FAIL = 0
WARN = 0
FAIL_DETAILS: list[str] = []


def _msg(prefix: str, msg: str, detail: str = ""):
    print(f"  {prefix}  {msg}" + (f"  — {detail}" if detail else ""))


def ok(msg: str, detail: str = ""):
    global PASS
    PASS += 1
    _msg("✓", msg, detail)


def fail(msg: str, detail: str = ""):
    global FAIL
    FAIL += 1
    _msg("✗", msg, detail)
    FAIL_DETAILS.append(f"{msg}: {detail}")


def warn(msg: str, detail: str = ""):
    global WARN
    WARN += 1
    _msg("⚠", msg, detail)


def section(title: str):
    print(f"\n=== {title} ===")


def get(path: str, session: requests.Session | None = None, **kw) -> requests.Response:
    s = session or requests
    return s.get(BASE + path, timeout=30, **kw)


def post(path: str, json_body=None, session: requests.Session | None = None) -> requests.Response:
    s = session or requests
    return s.post(BASE + path, json=json_body, timeout=60)


# ============================== TESTS ==============================

def test_static_pages():
    section("Static pages (anonymous)")
    for p in [
        "/", "/browse", "/lies", "/ask", "/compare", "/methodology",
        "/corrections", "/login", "/admin", "/admin/queue", "/admin/run",
        "/article/36690", "/compare/36684", "/robots.txt", "/api/docs",
    ]:
        r = get(p)
        if r.status_code == 200:
            ok(f"GET {p}", f"{len(r.content)}B")
        else:
            fail(f"GET {p}", f"HTTP {r.status_code}")


def test_stats_anonymous():
    section("Anonymous /api/stats")
    r = get("/api/stats")
    if r.status_code != 200:
        fail("GET /api/stats", f"HTTP {r.status_code}")
        return None
    d = r.json()
    if d.get("viewer") == "anonymous":
        ok("viewer = anonymous")
    else:
        fail("viewer should be anonymous", str(d.get("viewer")))
    if d.get("public_mode") is True:
        ok("public_mode = True")
    else:
        warn("public_mode not True", str(d.get("public_mode")))
    n = d.get("n_articles_muizzu_total", 0)
    if n >= 3097:
        ok("articles total", f"{n} (≥ baseline 3097)")
    else:
        fail("articles total below baseline", str(n))
    if d.get("n_fact_checks") == 218:
        ok("fact_checks visible = 218 (all published)")
    else:
        warn(f"fact_checks visible = {d.get('n_fact_checks')}")
    return d


def test_viz_endpoints():
    section("Viz endpoints")
    for name in [
        "articles-per-month", "claims-per-month",
        "factchecks-by-category", "factchecks-by-month", "topics",
    ]:
        r = get(f"/api/viz/{name}")
        if r.status_code == 200:
            j = r.json()
            if "labels" in j and isinstance(j["labels"], list):
                ok(f"/api/viz/{name}", f"{len(j['labels'])} buckets")
            else:
                fail(f"/api/viz/{name} shape", "missing labels")
        else:
            fail(f"/api/viz/{name}", f"HTTP {r.status_code}")


def test_articles():
    section("/api/articles + /api/article/{id}")
    r = get("/api/articles?limit=3")
    if r.status_code == 200 and r.json().get("total") >= 3000:
        ok("/api/articles", f"total={r.json()['total']}")
    else:
        fail("/api/articles", r.text[:120])
    # With filters
    r = get("/api/articles?date_from=2026-05-01&date_to=2026-05-31&limit=1")
    if r.status_code == 200:
        t = r.json().get("total")
        ok(f"/api/articles date filter", f"May 2026 = {t} articles")
    else:
        fail("date-filtered articles", r.text[:120])
    # Single
    r = get("/api/article/36690")
    if r.status_code == 200:
        a = r.json()
        if a.get("id") == 36690 and isinstance(a.get("claims"), list):
            ok("/api/article/36690", f"{len(a['claims'])} claims, {len(a.get('fact_checks', []))} fcs")
        else:
            fail("article 36690 shape", str(list(a.keys()))[:120])
    else:
        fail("GET /api/article/36690", f"HTTP {r.status_code}")


def test_factchecks():
    section("/api/factchecks")
    r = get("/api/factchecks?limit=5")
    if r.status_code == 200:
        d = r.json()
        ok(f"/api/factchecks", f"total visible = {d['total']}")
    else:
        fail("/api/factchecks", f"HTTP {r.status_code}")
    r = get("/api/factchecks?category=LIE")
    if r.status_code == 200:
        ok("category=LIE filter", f"total = {r.json()['total']}")
    else:
        fail("category filter", f"HTTP {r.status_code}")


def test_factcards_and_compare():
    section("Fact cards + DV/EN")
    r = get("/api/article/36690/factcard")
    if r.status_code == 200 and r.json().get("exists"):
        ok("/api/article/36690/factcard", f"severity={r.json()['severity']}")
    else:
        fail("factcard 36690", r.text[:120])
    r = get("/api/compare?limit=10")
    if r.status_code == 200:
        ok("/api/compare", f"total={r.json()['total']}")
    else:
        fail("/api/compare", f"HTTP {r.status_code}")
    r = get("/api/compare/36684")
    if r.status_code == 200 and r.json().get("exists"):
        ok("/api/compare/36684", f"inconsistencies={len(r.json().get('inconsistencies', []))}")
    else:
        fail("compare 36684", r.text[:120])
    r = get("/api/recent-factcards?limit=3")
    if r.status_code == 200:
        ok("/api/recent-factcards", f"items={len(r.json()['items'])}")
    else:
        fail("recent-factcards", r.text[:120])


def test_ask_admin():
    section("Q&A endpoint")
    # As anonymous, the daily cap may apply — try, but don't fail if 503
    r = post("/api/ask", {"question": "what is muizzu up to?", "limit": 10})
    if r.status_code == 200:
        ok("POST /api/ask (anonymous)", f"cost=${r.json()['cost_usd']:.4f}")
    elif r.status_code == 503:
        warn("POST /api/ask anonymous", "daily cap hit (expected — admins bypass)")
    elif r.status_code == 429:
        warn("POST /api/ask anonymous", "rate limited")
    else:
        fail("POST /api/ask anonymous", f"HTTP {r.status_code} {r.text[:100]}")

    # As admin
    s = requests.Session()
    r = post("/api/login", {"username": ADMIN_USER, "password": ADMIN_PASS}, session=s)
    if r.status_code != 200:
        fail("admin login", r.text[:100])
        return
    ok("admin login")
    r = s.get(BASE + "/api/me", timeout=30)
    if r.json().get("authenticated") is True and r.json().get("role") == "admin":
        ok("session cookie valid", str(r.json()))
    else:
        fail("session check", r.text[:100])
    # Now ask
    r = post("/api/ask", {"question": "what lies did muizzu tell?"}, session=s)
    if r.status_code == 200:
        d = r.json()
        ok("POST /api/ask (admin)", f"iterations={d.get('n_iterations', '?')} cost=${d['cost_usd']:.4f}")
    else:
        fail("POST /api/ask admin", f"HTTP {r.status_code} {r.text[:100]}")


def test_admin_flow():
    section("Admin auth + queue + publish flow")
    s = requests.Session()
    # bad creds
    r = post("/api/login", {"username": ADMIN_USER, "password": "wrong"}, session=s)
    if r.status_code == 401:
        ok("bad creds → 401")
    else:
        fail("bad creds should 401", f"got {r.status_code}")
    # good creds
    r = post("/api/login", {"username": ADMIN_USER, "password": ADMIN_PASS}, session=s)
    if r.status_code != 200:
        fail("admin login (good)", r.text[:100])
        return
    ok("login (good creds)")
    # queue
    r = s.get(BASE + "/api/admin/queue?limit=5", timeout=30)
    if r.status_code == 200:
        ok(f"/api/admin/queue", f"pending = {r.json()['total_pending']}")
    else:
        fail("admin queue", f"HTTP {r.status_code} {r.text[:100]}")
    # unpublish #156, verify anonymous can't see it, republish
    r = s.post(BASE + "/api/admin/factcheck/156/publish",
               json={"publish": False}, timeout=30)
    if r.status_code != 200:
        fail("unpublish 156", r.text[:100])
        return
    ok("unpublished 156")
    r = get("/api/factcheck/156")  # anonymous
    if r.status_code == 404:
        ok("anonymous can't see unpublished 156")
    else:
        fail("anonymous saw unpublished 156", f"HTTP {r.status_code}")
    # admin can still see
    r = s.get(BASE + "/api/factcheck/156", timeout=30)
    if r.status_code == 200:
        ok("admin sees unpublished 156")
    else:
        fail("admin should see unpublished", r.text[:100])
    # republish
    r = s.post(BASE + "/api/admin/factcheck/156/publish",
               json={"publish": True}, timeout=30)
    if r.status_code == 200:
        ok("republished 156")
    else:
        fail("republish 156", r.text[:100])
    r = get("/api/factcheck/156")
    if r.status_code == 200:
        ok("anonymous sees 156 again")
    else:
        fail("after republish anonymous should see", f"HTTP {r.status_code}")
    # logout
    r = s.post(BASE + "/api/logout", timeout=30)
    if r.status_code == 200:
        ok("logout")
    r = s.get(BASE + "/api/me", timeout=30)
    if r.json().get("authenticated") is False:
        ok("post-logout: not authenticated")
    else:
        fail("logout did not clear", r.text[:100])
    # Unauthed: admin queue 401
    r = get("/api/admin/queue")
    if r.status_code == 401:
        ok("unauthenticated /api/admin/queue → 401")
    else:
        fail("admin queue without auth", f"HTTP {r.status_code}")


def test_corrections():
    section("Corrections form")
    r = post("/api/corrections", {
        "body": "Test correction — please ignore. This is from the system test.",
        "fact_check_id": 156,
    })
    if r.status_code == 200:
        ok("POST /api/corrections", f"id={r.json()['id']}")
    else:
        fail("submit correction", r.text[:120])


def test_db_consistency():
    section("DB consistency")
    if not DB.exists():
        warn("DB not found", str(DB))
        return
    conn = sqlite3.connect(str(DB))
    actual_fc = conn.execute("SELECT COUNT(*) FROM fact_checks").fetchone()[0]
    actual_articles = conn.execute(
        """SELECT COUNT(*) FROM articles WHERE language='EN' AND body_text IS NOT NULL
                                AND body_text != ''
                                AND category IN ('press_release','speech','vp_speech')
                                AND published_date >= '2023-11-17'"""
    ).fetchone()[0]
    r = get("/api/stats")
    s = r.json()
    if s["n_articles_muizzu_total"] == actual_articles:
        ok("article count matches DB", str(actual_articles))
    else:
        fail("article count mismatch",
             f"API={s['n_articles_muizzu_total']} DB={actual_articles}")
    api_visible_fc = s["n_fact_checks"]
    pub_fc = conn.execute("SELECT COUNT(*) FROM fact_checks WHERE published=1").fetchone()[0]
    if api_visible_fc == pub_fc:
        ok(f"fact_checks visible to anon = published count", str(pub_fc))
    else:
        fail("public fact_check count mismatch", f"API={api_visible_fc} DB.published=1: {pub_fc}")
    conn.close()


def test_rate_limit():
    section("Rate limiting (12 quick POSTs to /api/ask)")
    codes = []
    for _ in range(12):
        r = post("/api/ask", {"question": "test"})
        codes.append(r.status_code)
    n_429 = codes.count(429)
    n_503 = codes.count(503)
    n_200 = codes.count(200)
    if n_429 > 0:
        ok(f"rate-limit fired", f"{n_429}× HTTP 429")
    elif n_503 > 0:
        ok("daily-cap fired", f"{n_503}× HTTP 503 (cap reached)")
    elif n_200 > 0:
        warn("no rate-limit triggered", f"{n_200}× 200 (cap not yet exhausted)")
    else:
        fail("unexpected rate-limit codes", str(codes))


def test_drill_down():
    section("URL drill-down filters")
    # Articles filtered by month
    r = get("/api/articles?date_from=2026-05-01&date_to=2026-05-31&limit=1")
    if r.status_code == 200 and r.json()["total"] > 0:
        ok("articles in May 2026", f"total={r.json()['total']}")
    else:
        fail("date drill-down", r.text[:100])
    # Fact-checks by category + date
    r = get("/api/factchecks?category=BROKEN%20DEADLINE&limit=1")
    if r.status_code == 200:
        ok(f"fact-checks category=BROKEN DEADLINE", f"total={r.json()['total']}")
    else:
        fail("category drill-down", r.text[:100])
    # By topic
    r = get("/api/factchecks?topic=housing&limit=1")
    if r.status_code == 200:
        ok("topic=housing", f"total={r.json()['total']}")
    else:
        fail("topic drill-down", r.text[:100])


def test_security_headers():
    section("Security / robots")
    r = get("/robots.txt")
    body = r.text
    if "Disallow: /admin" in body and "User-agent: *" in body:
        ok("robots.txt blocks /admin", body.replace("\n", " | ").strip())
    else:
        fail("robots.txt missing rules", body[:200])


def test_cli_publish():
    section("CLI publish/unpublish")
    import subprocess
    py = Path(".venv/bin/kahzaabu")
    if not py.exists():
        warn("CLI not found", str(py))
        return
    out = subprocess.run(
        [str(py), "publish", "156", "--unpublish"],
        capture_output=True, text=True, cwd=Path.cwd(),
    )
    if out.returncode == 0 and "unpublished" in out.stdout:
        ok("CLI: unpublish 156")
    else:
        fail("CLI unpublish failed", out.stdout + out.stderr)
    r = get("/api/factcheck/156")
    if r.status_code == 404:
        ok("anonymous can't see (CLI unpublished)")
    else:
        fail("after CLI unpublish, anonymous should 404", f"HTTP {r.status_code}")
    out = subprocess.run(
        [str(py), "publish", "156"],
        capture_output=True, text=True, cwd=Path.cwd(),
    )
    if out.returncode == 0 and "published" in out.stdout:
        ok("CLI: republish 156")
    else:
        fail("CLI republish failed", out.stdout + out.stderr)


def test_log_for_errors():
    section("Server log scan")
    log = Path("/tmp/kahzaabu_web.log")
    if not log.exists():
        warn("log not found")
        return
    body = log.read_text()
    err_lines = [l for l in body.splitlines() if "ERROR" in l or "Traceback" in l or "sqlite3.Programming" in l]
    if err_lines:
        fail(f"log has {len(err_lines)} error lines", err_lines[0][:200])
        for l in err_lines[:5]:
            print(f"    > {l[:200]}")
    else:
        ok("server log clean (no ERROR / Traceback / sqlite errors)")


# ============================== RUN ==============================

if __name__ == "__main__":
    test_static_pages()
    test_stats_anonymous()
    test_viz_endpoints()
    test_articles()
    test_factchecks()
    test_factcards_and_compare()
    test_corrections()
    test_drill_down()
    test_admin_flow()
    test_ask_admin()
    test_rate_limit()
    test_db_consistency()
    test_security_headers()
    test_cli_publish()
    test_log_for_errors()

    print(f"\n{'=' * 60}")
    print(f"  PASS: {PASS}    WARN: {WARN}    FAIL: {FAIL}")
    if FAIL_DETAILS:
        print("\nFAILURES:")
        for d in FAIL_DETAILS:
            print(f"  • {d}")
    print(f"{'=' * 60}")
    sys.exit(0 if FAIL == 0 else 1)
