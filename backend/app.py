#!/usr/bin/env python3
"""
Meta Account Troubleshoot — backend service.

Endpoints
  GET  /api/health           -> liveness + cache status
  POST /api/seller           -> {seller_id, mode}  merges Metabase 7753 + 10353
  POST /api/plan             -> {seller, mode}      Claude-generated 3-tab action plan

Data sources (Metabase, db 6 on https://metabase.kaip.in):
  card 7753  seller_manager_mapping        -> GC / GM / KAE / KAM (+ more)
  card 10353 Seller info (Category Engine) -> company, website, AOV, COGS, products...

Run:
  cd backend
  python3 -m venv .venv && source .venv/bin/activate
  pip install -r requirements.txt
  cp .env.example .env        # fill in the values
  uvicorn app:app --reload --port 8000
"""
import os, json, time, threading, urllib.request, urllib.error
from datetime import date, timedelta
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"), override=True)
except ImportError:
    pass
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel

# ----------------------------------------------------------------------------
# Config (env vars). See .env.example
# ----------------------------------------------------------------------------
MB_URL   = os.environ.get("METABASE_URL", "https://metabase.kaip.in").rstrip("/")
MB_USER  = os.environ.get("METABASE_USER_EMAIL", "")
MB_PASS  = os.environ.get("METABASE_PASSWORD", "")
# Preferred: a Metabase API key (Admin -> Settings -> API keys). Sent as the `x-api-key` header,
# no session/login needed. Falls back to email+password session auth if the key is not set.
MB_API_KEY = os.environ.get("METABASE_API_KEY", "")

ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_BASE_URL = os.environ.get("ANTHROPIC_BASE_URL", "").rstrip("/")  # optional proxy (e.g. LiteLLM)
CLAUDE_MODEL       = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")  # or claude-opus-4-8
# Per-stage model tiering (default = CLAUDE_MODEL). To cut cost/budget, set specialists to
# a cheaper model, e.g. SPECIALIST_MODEL=claude-sonnet-4-6, keep SYNTH_MODEL=claude-opus-4-8.
# Upstash Redis (team-shared AI memory) — free tier. Set both to enable server-side memory.
UPSTASH_URL   = os.environ.get("UPSTASH_REDIS_REST_URL", "").rstrip("/")
UPSTASH_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
MEM_KEEP = 8  # plans retained per seller

PLAN_MODEL         = os.environ.get("PLAN_MODEL", CLAUDE_MODEL)  # single-pass plan model (Opus default; set Sonnet for max speed)
ANALYST_MODEL      = os.environ.get("ANALYST_MODEL", CLAUDE_MODEL)
SPECIALIST_MODEL   = os.environ.get("SPECIALIST_MODEL", CLAUDE_MODEL)
SYNTH_MODEL        = os.environ.get("SYNTH_MODEL", CLAUDE_MODEL)

MAPPING_CARD  = 7753
BUSINESS_CARD = 10353
BUSINESS_BACKUP_CARD = 10352   # lighter Category Intelligence card — website/company/contact backup
CATEGORY_CARD = 3757    # "product level category mapping" (db6, per-seller) — clean l1>l2>l3>l4 chain.
CATEGORY_PARAM = "f9e4715e-fc82-466e-b6eb-ac9462c8cfaf"
                        # 3773 was WRONG (independently-ranked l1/l2/l3 -> incoherent hierarchy).
METRICS_CARD  = 10773   # daily spend/CPM/CTR/s_gmv time series (all HIT sellers, no params)
WEEKLY_PNL_CARD = 11011 # week-1/2/3 spend + P&L per seller (no params)
LAST_TS_CARD    = 10189 # last troubleshoot details + actions per seller (optional seller filter)
PNL_CARD = 1880         # full weekly P&L (db2, cheap) — auto-fills the P&L upload; AOV from spend>3540 week
PNL_SELLER_PARAM = "dc33c41d-bd5b-2eab-2dd9-07f2a8e9d6f8"
PNL_WEEKS = 26          # keep the most recent N weeks for the P&L CSV
SPEND_FLOOR = 3540      # weekly spend floor for AOV selection / bucket rules
COGS_CARD = 2497        # seller-wise SKUs + COGS (db2, all sellers) -> cosgs
SPEND_TODAY_CARD = 2787 # today/yesterday/lifetime spend + first date (db2, all sellers)
SPEND_OVERALL_CARD = 10065  # total spend + first/last spend date (db2, all sellers; key col 'seller id')
# Bulk-cached cards (pulled param-less = all sellers in one scan). Category is per-seller (not here).
CARDS = (MAPPING_CARD, BUSINESS_CARD, BUSINESS_BACKUP_CARD,
         METRICS_CARD, WEEKLY_PNL_CARD, LAST_TS_CARD,
         COGS_CARD, SPEND_TODAY_CARD, SPEND_OVERALL_CARD)
METRICS_KEEP_DAYS = 45  # trim each seller's series to the last N rows in the cache

# Change Log dashboard 96 — per-seller, date-ranged change events (db 2, cheap).
# (dashcard_id, card_id, area-label). Skips the noisy order-processing seller-level log.
CHANGELOG_DASHBOARD = 96
CHANGELOG_PARAM = {"seller": "fff2e0d8", "start": "6e08655a", "end": "b8faecc6"}
CHANGELOG_DASHCARDS = [
    (815, 785,  "Coupon"),
    (816, 787,  "Meta marketing"),
    (817, 792,  "Payment"),
    (818, 790,  "Product page"),
    (820, 791,  "Shipping"),
    (821, 788,  "Website"),
    (822, 793,  "Catalogue"),
    (825, 786,  "Communication & catalogue settings"),
    (3512, 5185, "Google marketing"),
]

# Dashboard 398 — per-product marketplace DEMAND validation (db6, required seller filter).
# High marketplace rating count = proven external demand -> safe to run campaigns on.
DEMAND_DASHBOARD = 398
DEMAND_DASHCARD = 2499
DEMAND_CARD = 3425
DEMAND_PARAM = {"seller": "c6ad588d", "image": "e35b7e93",
                "rating_cutoff": "f54ccc7e", "significant": "abb1206b"}
DEMAND_DEFAULTS = {"image": 92, "rating_cutoff": 10, "significant": 20}  # dashboard defaults
DEMAND_TTL = 12 * 3600
DEMAND_TOPN = 25

# RTO dashboards (db2, cheap) — per-seller + date. City-wise (266) and LP/courier-wise (187).
RTO_CITY = {"dash": 266, "dashcard": 1988, "card": 1806, "seller": "50c1eb23", "start": "b90603da", "end": "7a7c7f27"}
RTO_LP   = {"dash": 187, "dashcard": 1440, "card": 855,  "seller": "4991551a", "start": "d1d1bf4",  "end": "3083b6c3"}
BUSINESS_SELLER_PARAM_ID = "a45e1c84-6dc1-42fc-93da-79f43ee84255"  # card 10353
CATEGORY_SELLER_PARAM_ID = "232ba3d0-4861-4ebd-8775-5f8b9d488737"  # card 10362

# Card 9293 "call, chat, sos dump" (db6, native) — seller communications: call summaries,
# chat events (seller/gc/kam/poc/bot/system) and SOS/leadership escalations. The card itself has
# NO seller param (it scans paused HIT sellers), so we run a seller-scoped rewrite of its SQL via
# /api/dataset. Feeds the Account Overview / RCA multi-angle story.
ESCALATION_DB = 6
ESCALATION_CARD = 9293   # source of the SQL (kept for reference / lineage)
ESCALATION_WINDOW_DAYS = int(os.environ.get("ESCALATION_WINDOW_DAYS", "30"))
ESCALATION_TTL = 3600    # per-seller cache (s)

# Card 11834 "All seller All Troubleshoot Actions" (db6, per-seller) — the STRATEGY lens: every
# troubleshoot workflow run on the account with the actions/solutions given. Lets the RCA tell what
# problem was identified, what was done, and (vs later data) whether it helped. Has a seller_id param.
TROUBLESHOOT_CARD = 11834
TROUBLESHOOT_PARAM = "3db15dfe-70c1-4765-906a-cb62fb43cf55"
TROUBLESHOOT_KEEP = 12   # most recent workflows to keep
TROUBLESHOOT_TTL = 6 * 3600

# --- caching / quota control ---
# Each card's seller_id filter is OPTIONAL, so we pull ALL sellers in ONE scan and
# cache by seller_id. Every lookup is then free; we only scan once per refresh.
# Cards 10353/10362 scan ~15 GB EACH, so NEVER query them per-seller in a loop.
BULK_TTL    = int(os.environ.get("BULK_TTL", str(20 * 3600)))   # cache lifetime (s)
CACHE_DIR   = os.environ.get("CACHE_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
SEED_DIR    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")  # snapshot bundled with deploy
REFRESH_TOKEN = os.environ.get("REFRESH_TOKEN", "")            # protects /api/refresh
ALLOW_LIVE_PULL = os.environ.get("ALLOW_LIVE_PULL", "1") == "1"  # set 0 on serverless to forbid request-time scans

from benchmarks import BENCHMARKS  # hard-coded category benchmarks, keyed by primary_l2

# ----------------------------------------------------------------------------
# Metabase client (stdlib only, with auto re-auth)
# ----------------------------------------------------------------------------
_session = {"token": None, "ts": 0}
_session_lock = threading.Lock()


def _req(url, method="GET", body=None, headers=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _auth(force=False):
    with _session_lock:
        if not force and _session["token"]:
            return _session["token"]
        if not (MB_USER and MB_PASS):
            raise HTTPException(500, "Metabase credentials not configured (METABASE_USER_EMAIL / METABASE_PASSWORD)")
        tok = _req(f"{MB_URL}/api/session", "POST",
                   {"username": MB_USER, "password": MB_PASS},
                   {"Content-Type": "application/json"})["id"]
        _session["token"] = tok
        _session["ts"] = time.time()
        return tok


def _mb_headers(force_reauth=False):
    """Auth headers: API key (x-api-key) if configured, else a login session token."""
    h = {"Content-Type": "application/json"}
    if MB_API_KEY:
        h["x-api-key"] = MB_API_KEY
    else:
        h["X-Metabase-Session"] = _auth(force=force_reauth)
    return h


def _mb(path, method="GET", body=None, timeout=60):
    """Call Metabase. With a session token, re-authenticate once on 401/403 (API keys don't expire)."""
    headers = _mb_headers()
    try:
        return _req(f"{MB_URL}{path}", method, body, headers, timeout=timeout)
    except urllib.error.HTTPError as ex:
        if ex.code in (401, 403) and not MB_API_KEY:
            headers = _mb_headers(force_reauth=True)
            return _req(f"{MB_URL}{path}", method, body, headers, timeout=timeout)
        raise HTTPException(502, f"Metabase error {ex.code}: {ex.read().decode()[:200]}")


def _mb_sql(database_id, sql, timeout=120):
    """Run a native SQL query via /api/dataset and return rows as a list of dicts (col name -> value).
    Used for card 9293's call/chat/sos data, which has no per-seller param — we run a seller-scoped
    rewrite of its SQL. Requires the Metabase account to have native-query permission on the database."""
    res = _mb("/api/dataset", "POST",
              {"database": database_id, "type": "native", "native": {"query": sql}}, timeout=timeout)
    data = res.get("data", {}) if isinstance(res, dict) else {}
    if isinstance(data.get("rows"), list):
        cols = [c.get("name") for c in data.get("cols", [])]
        return [dict(zip(cols, r)) for r in data["rows"]]
    # a dataset error comes back as {"error": ...} / {"status":"failed"} rather than raising
    raise HTTPException(502, f"Metabase dataset query failed: {str(res)[:200]}")


# ----------------------------------------------------------------------------
# Bulk cache — one scan pulls ALL sellers for a card; lookups served from memory.
# Layers (newest wins): in-memory -> disk (CACHE_DIR) -> bundled snapshot (SEED_DIR)
# -> live full pull. A failed pull (e.g. quota) falls back to any stale cache.
# ----------------------------------------------------------------------------
import gzip
from collections import defaultdict

_bulk = {}                              # card_id -> {"by_id": {...}, "ts": float}
_bulk_locks = defaultdict(threading.Lock)


def _bulk_file(card_id, directory):
    return os.path.join(directory, f"card_{card_id}.json.gz")


def _disk_read(card_id):
    """Return the freshest on-disk cache (runtime dir or bundled seed), or None."""
    best = None
    for d in (CACHE_DIR, SEED_DIR):
        p = _bulk_file(card_id, d)
        if os.path.exists(p):
            try:
                with gzip.open(p, "rt", encoding="utf-8") as f:
                    obj = json.load(f)
                if best is None or obj.get("ts", 0) > best.get("ts", 0):
                    best = obj
            except Exception:
                pass
    return best


def _disk_write(card_id, by_id, ts):
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        tmp = _bulk_file(card_id, CACHE_DIR) + ".tmp"
        with gzip.open(tmp, "wt", encoding="utf-8") as f:
            json.dump({"ts": ts, "by_id": by_id}, f)
        os.replace(tmp, _bulk_file(card_id, CACHE_DIR))
    except Exception:
        pass


def _pull_card_all(card_id):
    """Run the card with NO seller filter -> all sellers in one scan, indexed by id.
    The metrics card (10773) is a time series, so group rows into a per-seller list."""
    rows = _mb(f"/api/card/{card_id}/query/json", "POST", {})
    if card_id == METRICS_CARD:
        from collections import defaultdict as _dd
        g = _dd(list)
        for r in rows:
            if isinstance(r, dict) and r.get("seller_id"):
                g[str(r["seller_id"])].append(r)
        out = {}
        for sid, lst in g.items():
            lst.sort(key=lambda x: str(x.get("date") or ""))
            out[sid] = lst[-METRICS_KEEP_DAYS:]
        return out
    out = {}
    for r in rows:
        if isinstance(r, dict):
            sid = r.get("seller_id") or r.get("seller id")   # 10065 uses 'seller id' (with space)
            if sid:
                out[str(sid)] = r
    return out


def _get_changelog(seller_id, start, end):
    """Fetch per-seller change events from dashboard 96 (db 2, cheap) for a date range."""
    params = [
        {"id": CHANGELOG_PARAM["seller"], "value": str(seller_id)},
        {"id": CHANGELOG_PARAM["start"], "value": start},
        {"id": CHANGELOG_PARAM["end"], "value": end},
    ]
    def _one(dc):
        dcid, cid, area = dc
        out = []
        try:
            rows = _mb(f"/api/dashboard/{CHANGELOG_DASHBOARD}/dashcard/{dcid}/card/{cid}/query/json",
                       "POST", {"parameters": params})
        except Exception:
            return out
        for r in rows if isinstance(rows, list) else []:
            who = " ".join(x for x in [r.get("first_name"), r.get("last_name")] if x).strip() \
                  or r.get("changed_by") or r.get("user_email")
            out.append({
                "date": (r.get("createdat") or r.get("change_date_time") or "")[:19],
                "area": area,
                "category": _clean(r.get("category")),
                "field": _clean(r.get("field_name") or r.get("changed_fields")),
                "from": _clean(r.get("initial_value") or r.get("old_resource")),
                "to": _clean(r.get("new_value") or r.get("new_resource")),
                "by": _clean(who),
                "user_type": _clean(r.get("user_type")),
            })
        return out

    from concurrent.futures import ThreadPoolExecutor
    events = []
    with ThreadPoolExecutor(max_workers=len(CHANGELOG_DASHCARDS)) as ex:
        for part in ex.map(_one, CHANGELOG_DASHCARDS):
            events.extend(part)
    events.sort(key=lambda e: e["date"], reverse=True)
    # cap per area so one bulk edit (e.g. catalogue) doesn't crowd out other change types
    per_area, kept = {}, []
    for e in events:
        a = e["area"]
        per_area[a] = per_area.get(a, 0) + 1
        if per_area[a] <= 25:
            kept.append(e)
    kept.sort(key=lambda e: e["date"], reverse=True)
    return kept[:150]


_category_cache = {}
_category_lock = threading.Lock()


_category_cache = {}
_category_lock = threading.Lock()


def _get_category(seller_id):
    """Coherent category chain (l1>l2>l3) = the seller's MOST COMMON product category from card 3757.
    Per-seller (db6), cached 24h. Returns {} on failure/quota."""
    with _category_lock:
        c = _category_cache.get(seller_id)
        if c and time.time() - c["ts"] < 24 * 3600:
            return c["data"]
    params = [{"type": "string/=", "value": str(seller_id), "id": CATEGORY_PARAM,
               "target": ["variable", ["template-tag", "seller_id"]]}]
    try:
        rows = _mb(f"/api/card/{CATEGORY_CARD}/query/json", "POST", {"parameters": params})
    except Exception:
        return {}
    from collections import Counter
    chains = Counter()
    for r in rows if isinstance(rows, list) else []:
        if isinstance(r, dict) and _clean(r.get("l2")):
            chains[(_clean(r.get("l1")), _clean(r.get("l2")), _clean(r.get("l3")), _clean(r.get("l4")))] += 1
    data = {}
    if chains:
        (l1, l2, l3, l4), _n = chains.most_common(1)[0]
        data = {"l1": l1, "l2": l2, "l3": l3, "l4": l4}
    with _category_lock:
        _category_cache[seller_id] = {"data": data, "ts": time.time()}
    return data


def _fnum(x):
    try:
        return float(str(x).replace(",", "").replace("%", "").replace("₹", "").strip())
    except (TypeError, ValueError):
        return None


def _get_pnl_csv(seller_id):
    """P&L CSV from card 1880 (db2) — recent weeks as CSV text, plus AOV (card 1880 'aov') from the
    most recent week where marketing spend > SPEND_FLOOR. Returns (csv, weeks, aov)."""
    params = [{"type": "string/=", "value": str(seller_id), "id": PNL_SELLER_PARAM,
               "target": ["variable", ["template-tag", "seller_id"]]}]
    try:
        rows = _mb(f"/api/card/{PNL_CARD}/query/json", "POST", {"parameters": params})
    except Exception:
        return "", 0, None
    if not isinstance(rows, list) or not rows:
        return "", 0, None
    rows = [r for r in rows if isinstance(r, dict)]
    rows.sort(key=lambda r: str(r.get("year_week", "")), reverse=True)  # newest first
    # AOV: most recent week with marketing spend above the floor
    aov = None
    for r in rows:
        if _fnum(r.get("marketing_spend")) is not None and _fnum(r.get("marketing_spend")) > SPEND_FLOOR:
            aov = _fnum(r.get("aov"))
            if aov is not None:
                break
    # Cancellation: recent 2 weeks with a value (ideal <5%; >5% bleeds P&L via higher CPP)
    cw = [{"week": r.get("year_week"), "pct": _fnum(r.get("cancelled_perc"))}
          for r in rows if _fnum(r.get("cancelled_perc")) is not None][:2]
    cancel_recent = round(sum(x["pct"] for x in cw) / len(cw), 2) if cw else None
    cancellation = {"recent_pct": cancel_recent, "weeks": cw,
                    "high": (cancel_recent is not None and cancel_recent > 5)}
    recent = rows[:PNL_WEEKS][::-1]  # most recent N, chronological, for the CSV
    import io
    import csv as _csvmod
    cols = list(recent[0].keys())
    buf = io.StringIO()
    w = _csvmod.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    for r in recent:
        w.writerow({k: r.get(k) for k in cols})
    return buf.getvalue(), len(recent), aov, cancellation


def _get_rto(seller_id, start, end):
    """Per-seller RTO breakdown by city (dash 266) + courier/LP (dash 187), db2. Highest-RTO first."""
    out = {"cities": [], "couriers": []}
    try:
        p = [{"id": RTO_CITY["seller"], "value": str(seller_id)},
             {"id": RTO_CITY["start"], "value": start}, {"id": RTO_CITY["end"], "value": end}]
        rows = _mb(f"/api/dashboard/{RTO_CITY['dash']}/dashcard/{RTO_CITY['dashcard']}/card/{RTO_CITY['card']}/query/json", "POST", {"parameters": p})
        cities = [{"city": _clean(r.get("city name")), "total": _clean(r.get("total")),
                   "delivered": _clean(r.get("delivered")), "rto": _clean(r.get("rto_%"))}
                  for r in (rows if isinstance(rows, list) else []) if isinstance(r, dict)]
        cities = [c for c in cities if c["city"] and _num(c["total"]) >= 15]
        cities.sort(key=lambda c: _num(c["rto"]), reverse=True)
        out["cities"] = cities[:15]
    except Exception:
        pass
    try:
        p = [{"id": RTO_LP["seller"], "value": str(seller_id)},
             {"id": RTO_LP["start"], "value": start}, {"id": RTO_LP["end"], "value": end}]
        rows = _mb(f"/api/dashboard/{RTO_LP['dash']}/dashcard/{RTO_LP['dashcard']}/card/{RTO_LP['card']}/query/json", "POST", {"parameters": p})
        lp = [{"courier": _clean(r.get("logistic")), "delivered": _clean(r.get("delivered")), "rto": _clean(r.get("rto_prec"))}
              for r in (rows if isinstance(rows, list) else []) if isinstance(r, dict)]
        lp = [x for x in lp if x["courier"]]
        lp.sort(key=lambda x: _num(x["rto"]), reverse=True)
        out["couriers"] = lp[:10]
    except Exception:
        pass
    return out


_demand_cache = {}
_demand_lock = threading.Lock()


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return -1


def _get_demand(seller_id):
    """Per-product marketplace demand validation (dashboard 398). Best-effort + cached."""
    with _demand_lock:
        c = _demand_cache.get(seller_id)
        if c and time.time() - c["ts"] < DEMAND_TTL:
            return c["data"]
    params = [
        {"id": DEMAND_PARAM["seller"], "value": str(seller_id)},
        {"id": DEMAND_PARAM["image"], "value": DEMAND_DEFAULTS["image"]},
        {"id": DEMAND_PARAM["rating_cutoff"], "value": DEMAND_DEFAULTS["rating_cutoff"]},
        {"id": DEMAND_PARAM["significant"], "value": DEMAND_DEFAULTS["significant"]},
    ]
    try:
        rows = _mb(f"/api/dashboard/{DEMAND_DASHBOARD}/dashcard/{DEMAND_DASHCARD}/card/{DEMAND_CARD}/query/json",
                   "POST", {"parameters": params})
    except Exception:
        return []
    out = []
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict):
            continue
        out.append({
            "product": _clean(r.get("product_name")) or _clean(r.get("product_id")),
            "product_id": _clean(r.get("dashboard_product_id") or r.get("product_id")),
            "link": _clean(r.get("product_link")),                 # our storefront product link
            "mp_link": _clean(r.get("exact_max_link")),            # marketplace product link (highest-rated match)
            "orders_30d": _clean(r.get("total_orders_in_last_30_days")),
            "contribution_pct": _clean(r.get("product_contribution_perc")),
            "our_price": _clean(r.get("avg_sku_selling_price")),
            "our_cost": _clean(r.get("avg_sku_cost_price")),
            "mp_rating_count": _clean(r.get("exact_max_life_time_rating_count")),
            "mp_rating_site": _clean(r.get("exact_max_rating_website")),
            "demand_price_min": _clean(r.get("exact_min_price_with_demand")),
            "demand_price_max": _clean(r.get("exact_max_price_with_demand")),
            "demand_price_avg": _clean(r.get("exact_avg_price_with_demand")),
            "pq_score": _clean(r.get("lifetime_avg_pq_score")),
            "is_unique": _clean(r.get("is_unique")),
        })
    # rank by marketplace rating count (demand strength), then recent orders
    out.sort(key=lambda p: (_num(p["mp_rating_count"]), _num(p["orders_30d"])), reverse=True)
    data = out[:DEMAND_TOPN]
    with _demand_lock:
        _demand_cache[seller_id] = {"data": data, "ts": time.time()}
    return data


import re

_escalation_cache = {}
_escalation_lock = threading.Lock()


def _strip_html(s):
    """Chat content carries HTML (<b>, <br>) and <@Name|id> mentions -> plain readable text."""
    if not isinstance(s, str):
        return s
    s = re.sub(r"<@(?:[A-Za-z ]+:\s*)?([^|>]+)\|[^>]+>", r"@\1", s)   # <@GC: Name|id> / <@Name|id> -> @Name
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)                                     # drop remaining tags
    s = (s.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
          .replace("&quot;", '"').replace("&#39;", "'").replace("&nbsp;", " "))
    return re.sub(r"[ \t]+", " ", s).strip()


def _escalation_sql(seller_id, days):
    """Seller-scoped rewrite of card 9293's SQL: calls + chats + SOS for ONE seller over `days`.
    seller_id is validated alphanumeric by the caller (safe to inline). Partition filters on every
    fact table's own created_at are required by BigQuery for these tables."""
    return f"""
DECLARE target_seller STRING DEFAULT '{seller_id}';
DECLARE win_days INT64 DEFAULT {int(days)};

WITH
calls_wb AS (
  SELECT ecd.created_at, u1.role AS caller_role, ecd.summary, TO_JSON_STRING(ecd.seller_summary) AS actionables
  FROM `blitzscale-prod-project.nushop.exotel_calls` ec
  INNER JOIN `blitzscale-prod-project.nushop.exotel_call_details` ecd ON ec.exotel_call_sid = ecd.sid
  JOIN `nushop.workboard_tasks` wt ON wt.id = ec.entity_id
  LEFT JOIN nushop.userprofiles up1 ON ABS(SAFE_CAST(up1.contact_number AS FLOAT64)) = SAFE_CAST(ecd.call_from AS FLOAT64)
  LEFT JOIN nushop.users u1 ON u1._id = up1.user_id
  WHERE DATE(ec.created_at,'Asia/Kolkata')  >= DATE_SUB(CURRENT_DATE('Asia/Kolkata'), INTERVAL win_days DAY)
    AND DATE(ecd.created_at,'Asia/Kolkata') >= DATE_SUB(CURRENT_DATE('Asia/Kolkata'), INTERVAL win_days DAY)
    AND DATE(wt.created_at,'Asia/Kolkata')  >= DATE_SUB(CURRENT_DATE('Asia/Kolkata'), INTERVAL 90 DAY)
    AND wt.seller_id = target_seller
),
calls_convo AS (
  SELECT ecd.created_at, u1.role AS caller_role, ecd.summary, TO_JSON_STRING(ecd.seller_summary) AS actionables
  FROM `blitzscale-prod-project.nushop.exotel_calls` ec
  JOIN `blitzscale-prod-project.nushop.exotel_call_details` ecd ON ec.exotel_call_sid = ecd.sid
  JOIN `seller_app_chat.conversations` c ON c.id = ec.entity_id
  LEFT JOIN nushop.userprofiles up1 ON ABS(SAFE_CAST(up1.contact_number AS FLOAT64)) = SAFE_CAST(ecd.call_from AS FLOAT64)
  LEFT JOIN nushop.users u1 ON u1._id = up1.user_id
  WHERE DATE(ec.created_at,'Asia/Kolkata')  >= DATE_SUB(CURRENT_DATE('Asia/Kolkata'), INTERVAL win_days DAY)
    AND DATE(ecd.created_at,'Asia/Kolkata') >= DATE_SUB(CURRENT_DATE('Asia/Kolkata'), INTERVAL win_days DAY)
    AND c.created_at < CURRENT_TIMESTAMP()
    AND c.user_id = target_seller
),
all_calls AS (
  SELECT DATETIME(created_at,'Asia/Kolkata') AS ev_at, caller_role, summary, actionables
  FROM (SELECT * FROM calls_wb UNION ALL SELECT * FROM calls_convo)
),
chats AS (
  SELECT DATETIME(ce.created_at,'Asia/Kolkata') AS ev_at, ce.sender_type AS who, ce.content
  FROM `blitzscale-prod-project.seller_app_chat.chat_events` ce
  JOIN `blitzscale-prod-project.seller_app_chat.conversations` c ON ce.conversation_id = c.id
  WHERE DATE(ce.created_at,'Asia/Kolkata') >= DATE_SUB(CURRENT_DATE('Asia/Kolkata'), INTERVAL win_days DAY)
    AND c.created_at < CURRENT_TIMESTAMP()
    AND c.user_id = target_seller
),
sos AS (
  SELECT DATETIME(created_at,'Asia/Kolkata') AS ev_at, request_channel, comment
  FROM `blitzscale-prod-project.nushop.seller_app_requests`
  WHERE request_type IN ('sos','leadership_escalation')
    AND created_at >= TIMESTAMP(DATE_SUB(CURRENT_DATE('Asia/Kolkata'), INTERVAL win_days DAY))
    AND seller_id = target_seller
)
SELECT 'call' AS source, CAST(ev_at AS STRING) AS ev_at, caller_role AS who, summary AS text, actionables AS extra FROM all_calls
UNION ALL
SELECT 'chat', CAST(ev_at AS STRING), who, content, CAST(NULL AS STRING) FROM chats
UNION ALL
SELECT 'sos', CAST(ev_at AS STRING), request_channel, comment, CAST(NULL AS STRING) FROM sos
ORDER BY ev_at DESC
"""


ESC_CALLS_KEEP, ESC_CHATS_KEEP, ESC_SOS_KEEP = 30, 160, 30


def _get_escalations(seller_id, days=None):
    """Per-seller calls / chats / SOS from card 9293's query (db6, native). Best-effort + cached.
    Returns {} on any failure (e.g. no native-query permission) so the overview degrades gracefully."""
    days = days or ESCALATION_WINDOW_DAYS
    sid = str(seller_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9]+", sid):   # ids are mongo-style hex; reject anything else (SQL-inject guard)
        return {}
    ck = (sid, days)
    with _escalation_lock:
        c = _escalation_cache.get(ck)
        if c and time.time() - c["ts"] < ESCALATION_TTL:
            return c["data"]
    try:
        rows = _mb_sql(ESCALATION_DB, _escalation_sql(sid, days))
    except Exception:
        return {}
    calls, chats, sos = [], [], []
    counts = {"seller": 0, "gc": 0, "kam": 0, "poc": 0, "bot": 0, "system": 0}
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict):
            continue
        src, when = r.get("source"), _clean(r.get("ev_at"))
        if src == "call":
            role = (r.get("who") or "").lower()
            side = "KAM" if "key-account" in role else ("GC" if "growth" in role else _clean(r.get("who")))
            calls.append({"at": when, "side": side, "summary": _strip_html(_clean(r.get("text")))})
        elif src == "chat":
            who = (_clean(r.get("who")) or "").lower()
            txt = _strip_html(_clean(r.get("text")))
            if not txt:
                continue
            if who in counts:
                counts[who] += 1
            chats.append({"at": when, "who": who, "text": txt})
        elif src == "sos":
            sos.append({"at": when, "channel": _clean(r.get("who")), "comment": _strip_html(_clean(r.get("text")))})
    data = {
        "window_days": days,
        "counts": {"calls": len(calls), "chats": len(chats), "sos": len(sos), **counts},
        "calls": calls[:ESC_CALLS_KEEP],
        "chats": chats[:ESC_CHATS_KEEP],
        "sos": sos[:ESC_SOS_KEEP],
    }
    with _escalation_lock:
        _escalation_cache[ck] = {"data": data, "ts": time.time()}
    return data


def _escalations_block(esc):
    """Compact, role-tagged text digest of the seller's calls/chats/SOS for the overview prompt."""
    if not esc or not (esc.get("calls") or esc.get("chats") or esc.get("sos")):
        return "### Seller communications (calls / chats / escalations)\n(none found in the recent window)\n"
    lines = [f"### Seller communications — last {esc.get('window_days')}d "
             f"({esc['counts'].get('calls',0)} calls, {esc['counts'].get('chats',0)} chat msgs, "
             f"{esc['counts'].get('sos',0)} SOS/escalations)"]
    if esc.get("sos"):
        lines.append("\nSOS / LEADERSHIP ESCALATIONS (most recent first):")
        for s in esc["sos"][:8]:
            lines.append(f"- [{s.get('at')}] ({s.get('channel')}) {str(s.get('comment') or '')[:300]}")
    if esc.get("calls"):
        lines.append("\nCALL SUMMARIES (side = who was on the call):")
        for c in esc["calls"][:8]:
            lines.append(f"- [{c.get('at')}] ({c.get('side')}) {str(c.get('summary') or '')[:300]}")
    if esc.get("chats"):
        # drop pure bot/system noise from the prompt; keep the human (seller/gc/kam/poc) exchange
        human = [m for m in esc["chats"] if (m.get("who") in ("seller", "gc", "kam", "poc"))]
        shown = (human or esc["chats"])[:45]
        lines.append("\nCHAT TIMELINE (who: seller / gc / kam / poc):")
        for m in shown:
            lines.append(f"- [{m.get('at')}] {m.get('who')}: {str(m.get('text') or '')[:180]}")
    return "\n".join(lines) + "\n"


_ts_cache = {}
_ts_lock = threading.Lock()


def _get_troubleshoots(seller_id):
    """Per-seller troubleshoot history from card 11834 (db6, has a seller_id param). Each row is one TS
    workflow with its rolled-up actions/solutions. Best-effort + cached. Returns [] on failure."""
    sid = str(seller_id or "").strip()
    if not sid:
        return []
    with _ts_lock:
        c = _ts_cache.get(sid)
        if c and time.time() - c["ts"] < TROUBLESHOOT_TTL:
            return c["data"]
    params = [{"type": "string/=", "value": sid, "id": TROUBLESHOOT_PARAM,
               "target": ["variable", ["template-tag", "seller_id"]]}]
    try:
        rows = _mb(f"/api/card/{TROUBLESHOOT_CARD}/query/json", "POST", {"parameters": params})
    except Exception:
        return []
    out = []
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict):
            continue
        acts = (_clean(r.get("ts_actions")) or "").replace("|DETAILS|", " — ")
        acts = re.sub(r"\s+", " ", acts).strip()
        out.append({
            "date": _clean(r.get("ts_date")),
            "type": _clean(r.get("ts_type")),
            "count": r.get("action_count"),
            "actions": acts[:900],
        })
    out.sort(key=lambda t: str(t.get("date") or ""), reverse=True)   # newest first
    data = out[:TROUBLESHOOT_KEEP]
    with _ts_lock:
        _ts_cache[sid] = {"data": data, "ts": time.time()}
    return data


def _troubleshoots_block(ts):
    """Compact text digest of the seller's past troubleshoot workflows for the overview prompt."""
    if not ts:
        return "### Past troubleshoots (strategy lens — card 11834)\n(none found)\n"
    lines = [f"### Past troubleshoots — {len(ts)} workflow(s), most recent first (card 11834). "
             "Each line is one TS: the date, type, and the actions/solutions the growth team gave."]
    for t in ts:
        lines.append(f"- [{t.get('date')}] ({t.get('type')}, {t.get('count')} actions): {t.get('actions')}")
    return "\n".join(lines) + "\n"


def _refresh_card(card_id):
    """Force a live full pull for one card, update memory + disk. Raises on failure."""
    by_id = _pull_card_all(card_id)
    ts = time.time()
    _bulk[card_id] = {"by_id": by_id, "ts": ts}
    _disk_write(card_id, by_id, ts)
    return len(by_id)


def _get_card_cache(card_id):
    """Return {seller_id: row} for a card, using cache; pull live only if stale & allowed."""
    with _bulk_locks[card_id]:
        mem = _bulk.get(card_id)
        if mem and (time.time() - mem["ts"] < BULK_TTL):
            return mem["by_id"]

        disk = _disk_read(card_id)
        if disk and (time.time() - disk.get("ts", 0) < BULK_TTL):
            _bulk[card_id] = {"by_id": disk["by_id"], "ts": disk.get("ts", time.time())}
            return _bulk[card_id]["by_id"]

        # stale/empty -> try a live full pull (one scan, all sellers)
        if ALLOW_LIVE_PULL:
            try:
                _refresh_card(card_id)
                return _bulk[card_id]["by_id"]
            except HTTPException:
                pass  # e.g. BigQuery quota — fall through to any stale cache

        # last resort: any stale cache we have (better than nothing)
        if mem:
            return mem["by_id"]
        if disk:
            _bulk[card_id] = {"by_id": disk["by_id"], "ts": disk.get("ts", 0)}
            return disk["by_id"]
        raise HTTPException(503, f"No data available for card {card_id} (cache empty and live pull unavailable)")


def refresh_all():
    """Force-refresh every card (used by snapshot.py and /api/refresh).
    Resilient: a per-card failure (e.g. BigQuery quota) is recorded, not fatal,
    so the cheaper cards still refresh and any prior snapshot is preserved."""
    out = {}
    for cid in CARDS:
        try:
            with _bulk_locks[cid]:
                out[cid] = _refresh_card(cid)
        except Exception as e:
            out[cid] = f"error: {str(e)[:120]}"
    return out


SPEND_BUCKET_THRESHOLD = 3540  # weekly spend floor for HIT bucket rules


def _bucket_flags(wp):
    """Bucket-health / hit flags from weekly P&L (11011). W1=most recent week.
    >-20 (health): W1 PnL > -20 & W1 spend > 3540
    Potential:  PnL >= 5 & spend > 3540 (W1)
    Objective:  PnL >= 5 in W1 & W2, spend > 3540 in both
    Subjective: PnL >= 5 in W1, PnL > 3 in W2, spend > 3540 in both
    Missed Hit: PnL >= 5 in W2 & W3, spend > 3540 in both
    """
    def n(x):
        try:
            return float(x)
        except (TypeError, ValueError):
            return None
    w1p, w2p, w3p = n(wp.get("w1_pnl")), n(wp.get("w2_pnl")), n(wp.get("w3_pnl"))
    w1s, w2s, w3s = n(wp.get("w1_spend")), n(wp.get("w2_spend")), n(wp.get("w3_spend"))
    S = SPEND_BUCKET_THRESHOLD
    ge = lambda a, b: a is not None and a >= b
    gt = lambda a, b: a is not None and a > b
    return {
        "bucket_health": gt(w1p, -20) and gt(w1s, S),
        "potential":     ge(w1p, 5) and gt(w1s, S),
        "objective":     ge(w1p, 5) and ge(w2p, 5) and gt(w1s, S) and gt(w2s, S),
        "subjective":    ge(w1p, 5) and gt(w2p, 3) and gt(w1s, S) and gt(w2s, S),
        "missed_hit":    ge(w2p, 5) and ge(w3p, 5) and gt(w2s, S) and gt(w3s, S),
    }


def _clean(v):
    """Normalize Metabase placeholders ('-', '', None) to None."""
    if v is None:
        return None
    if isinstance(v, str) and v.strip() in ("", "-"):
        return None
    return v.strip() if isinstance(v, str) else v


# ----------------------------------------------------------------------------
# FastAPI app
# ----------------------------------------------------------------------------
app = FastAPI(title="Meta Account Troubleshoot API", version="1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],          # internal tool; tighten to your Pages origin if you like
    allow_methods=["*"],
    allow_headers=["*"],
)


class SellerReq(BaseModel):
    seller_id: str
    mode: str = "hits"
    start_date: str = ""   # YYYY-MM-DD; default = 30 days ago
    end_date: str = ""     # YYYY-MM-DD; default = today


class CsvFile(BaseModel):
    name: str = "data.csv"
    content: str = ""


class InsightsReq(BaseModel):
    seller_id: str
    start_date: str = ""
    end_date: str = ""


_changelog_cache = {}
_changelog_lock = threading.Lock()


@app.post("/api/insights")
def insights(req: InsightsReq):
    """Slow per-seller data (changelog + marketplace demand) — loaded lazily by the UI."""
    sid = req.seller_id.strip()
    if not sid:
        raise HTTPException(400, "seller_id is required")
    end = (req.end_date or date.today().isoformat())[:10]
    start = (req.start_date or (date.today() - timedelta(days=30)).isoformat())[:10]

    key = (sid, start, end)
    with _changelog_lock:
        c = _changelog_cache.get(key)
        cached_log = c["data"] if c and time.time() - c["ts"] < 1800 else None

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=3) as ex:
        f_log = ex.submit((lambda: cached_log) if cached_log is not None
                          else (lambda: _safe_call(_get_changelog, sid, start, end, default=[])))
        f_dem = ex.submit(_safe_call, _get_demand, sid, default=[])
        f_rto = ex.submit(_safe_call, _get_rto, sid, start, end, default={"cities": [], "couriers": []})
        changelog = f_log.result()
        demand = f_dem.result()
        rto = f_rto.result()

    if cached_log is None:
        with _changelog_lock:
            _changelog_cache[key] = {"data": changelog, "ts": time.time()}
    return {"changelog": changelog, "demand_products": demand, "rto": rto, "range": {"start": start, "end": end}}


def _safe_call(fn, *args, default=None):
    try:
        return fn(*args)
    except Exception:
        return default


class PlanReq(BaseModel):
    seller: dict = {}
    mode: str = "hits"
    website: str = ""
    pnl_csv: str = ""          # P&L export (CSV text)
    meta_csv: str = ""         # Meta ad-account metrics export (CSV text)
    extra_csv: list[CsvFile] = []   # optional additional CSVs
    # multi-agent staging (frontend-orchestrated): "full" | "analyst" | "specialist" | "synth"
    stage: str = "full"
    digest: str = ""           # analyst output, fed to specialist/synth
    category: str = ""         # for stage=specialist: website|campaign|outside|scaleup
    drafts: dict = {}          # for stage=synth: {category: [actions]}
    prior_plans: list = []     # AI's own past plans for this seller (memory), most recent first


class OverviewReq(BaseModel):
    seller: dict = {}
    mode: str = "hits"
    pnl_csv: str = ""               # P&L export (CSV text) — required
    meta_csv: str = ""              # Meta ad-account metrics export (CSV text) — required
    extra_csv: list[CsvFile] = []   # optional additional CSVs
    window_days: int = 0            # escalation lookback (0 -> ESCALATION_WINDOW_DAYS)
    force: bool = False             # True = regenerate even if a fresh cached RCA exists


_INDEX_PATHS = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "index.html"),
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html"),
]


@app.get("/")
def home():
    """Serve the frontend at the API root (bundled index.html), else redirect to Pages."""
    for p in _INDEX_PATHS:
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                return HTMLResponse(f.read())
    return RedirectResponse("https://pawankumar-pkaytsk.github.io/troubleshoot-tool/")


@app.get("/api/health")
def health():
    # lightweight: report ONLY in-memory cache (no disk reads) so this stays fast on cold start
    cache = {str(cid): len((_bulk.get(cid) or {}).get("by_id", {})) for cid in CARDS}
    return {
        "ok": True,
        "metabase": bool(MB_API_KEY or (MB_USER and MB_PASS)),
        "claude": bool(ANTHROPIC_API_KEY),
        "model": CLAUDE_MODEL,
        "claude_base_url": ANTHROPIC_BASE_URL or "api.anthropic.com (direct)",
        "memory": bool(UPSTASH_URL and UPSTASH_TOKEN),
        "cache": cache,
    }


FUNDS_MODEL = os.environ.get("FUNDS_MODEL", "claude-haiku-4-5-20251001")  # cheapest, for the funds ping


@app.get("/api/funds")
def funds():
    """Report remaining AI budget via LiteLLM's response headers (x-litellm-key-*)."""
    if not (ANTHROPIC_API_KEY and ANTHROPIC_BASE_URL):
        return {"available": False}
    try:
        client = _anthropic()
        raw = client.messages.with_raw_response.create(
            model=FUNDS_MODEL, max_tokens=1, messages=[{"role": "user", "content": "."}])
        h = raw.headers
        mb, sp = h.get("x-litellm-key-max-budget"), h.get("x-litellm-key-spend")
        if mb is None or sp is None:
            return {"available": False}
        mb, sp = float(mb), float(sp)
        return {"available": True, "max_budget": round(mb, 2), "spend": round(sp, 4),
                "remaining": round(mb - sp, 2), "currency": "USD"}
    except Exception:
        return {"available": False}


# ---- Team-shared AI memory (Upstash Redis REST) --------------------------------
def _redis(cmd):
    """Run one Redis command via Upstash REST. cmd = ['LPUSH','key','val']. Returns result or None."""
    if not (UPSTASH_URL and UPSTASH_TOKEN):
        return None
    r = urllib.request.Request(UPSTASH_URL, data=json.dumps(cmd).encode(),
                               method="POST", headers={"Authorization": "Bearer " + UPSTASH_TOKEN,
                                                        "Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=10) as resp:
        return json.loads(resp.read().decode()).get("result")


class MemoryReq(BaseModel):
    seller_id: str
    plan: dict


@app.get("/api/memory")
def memory_get(seller_id: str = ""):
    """Return the AI's past plans for a seller (team-shared, newest first)."""
    if not (UPSTASH_URL and UPSTASH_TOKEN):
        return {"available": False, "plans": []}
    sid = seller_id.strip()
    if not sid:
        return {"available": True, "plans": []}
    try:
        items = _redis(["LRANGE", "plan:" + sid, "0", str(MEM_KEEP - 1)]) or []
        plans = []
        for it in items:
            try:
                plans.append(json.loads(it))
            except Exception:
                pass
        return {"available": True, "plans": plans}
    except Exception:
        return {"available": False, "plans": []}


@app.post("/api/memory")
def memory_post(req: MemoryReq):
    """Append a plan to a seller's shared history (keeps the most recent MEM_KEEP)."""
    if not (UPSTASH_URL and UPSTASH_TOKEN):
        return {"available": False}
    sid = req.seller_id.strip()
    if not sid:
        raise HTTPException(400, "seller_id required")
    try:
        key = "plan:" + sid
        _redis(["LPUSH", key, json.dumps(req.plan)])
        _redis(["LTRIM", key, "0", str(MEM_KEEP - 1)])
        return {"available": True, "ok": True}
    except Exception as e:
        return {"available": False, "error": str(e)[:120]}


@app.post("/api/refresh")
def refresh(token: str = ""):
    """Force a full re-pull of all cards (one scan each, all sellers). Token-protected."""
    if REFRESH_TOKEN and token != REFRESH_TOKEN:
        raise HTTPException(401, "invalid or missing refresh token")
    try:
        counts = refresh_all()
    except HTTPException as e:
        raise e
    return {"refreshed": {str(k): v for k, v in counts.items()}}


@app.post("/api/seller")
def seller(req: SellerReq):
    sid = req.seller_id.strip()
    if not sid:
        raise HTTPException(400, "seller_id is required")

    # all three come from cache (one scan per card serves every seller)
    def lookup(card_id):
        try:
            return _get_card_cache(card_id).get(sid) or {}
        except HTTPException:
            return {}
    m   = lookup(MAPPING_CARD)
    b   = lookup(BUSINESS_CARD)
    b2  = lookup(BUSINESS_BACKUP_CARD)   # backup for website/company/contact/offers
    cg  = lookup(COGS_CARD)              # 2497 -> cosgs
    sp1 = lookup(SPEND_TODAY_CARD)       # 2787 -> today/yesterday/lifetime/first
    sp2 = lookup(SPEND_OVERALL_CARD)     # 10065 -> total/first/last

    # date range (default last 30 days)
    end = (req.end_date or date.today().isoformat())[:10]
    start = (req.start_date or (date.today() - timedelta(days=30)).isoformat())[:10]

    # daily metrics (10773) from cache, sliced to range
    try:
        series = _get_card_cache(METRICS_CARD).get(sid) or []
    except HTTPException:
        series = []
    # daily trend uses FIXED recent windows (last 3/7/14d), NOT the picker range
    # (the range drives only the changelog). Return the last 14 days.
    daily_metrics = series[-14:]

    # product-quality (PQ) flags from card 10773 — latest non-null values
    def _latest(field):
        for r in reversed(daily_metrics):
            v = r.get(field)
            if v is not None:
                return v
        return None
    pq = {"lifetime": _clean(_latest("lifetime_avg_pq")), "last_15d": _clean(_latest("last_15d_avg_pd"))}

    cat = _get_category(sid)   # coherent l1>l2>l3 chain (card 3757, per-seller, cached)
    try:
        pnl_csv, pnl_weeks, pnl_aov, cancellation = _get_pnl_csv(sid)   # P&L (1880) + AOV + recent cancellation
    except Exception:
        pnl_csv, pnl_weeks, pnl_aov, cancellation = "", 0, None, {"recent_pct": None, "weeks": [], "high": False}
    changelog = []          # loaded via POST /api/insights
    demand_products = []     # loaded via POST /api/insights

    # weekly P&L + spend (11011) and last troubleshoot details (10189) from cache
    wp = lookup(WEEKLY_PNL_CARD)
    ts = lookup(LAST_TS_CARD)
    weekly_pnl = {
        "best_source": _clean(wp.get("best_source")),
        "w1_spend": _clean(wp.get("w1_spend")), "w2_spend": _clean(wp.get("w2_spend")), "w3_spend": _clean(wp.get("w3_spend")),
        "w1_pnl": _clean(wp.get("w1_pnl")), "w2_pnl": _clean(wp.get("w2_pnl")), "w3_pnl": _clean(wp.get("w3_pnl")),
    }
    last_ts = {
        "total_ts_done": _clean(ts.get("total_ts_done")),
        "last_ts_date": _clean(ts.get("last_ts_date")),
        "ts_type": _clean(ts.get("ts_type")),
        "last_ts_actions": _clean(ts.get("last_ts_actions")),
        "last_7d_meta_spend": _clean(ts.get("last_7_days__meta_spend")),
    }
    buckets = _bucket_flags(weekly_pnl)

    if not m and not b and not b2 and not cat and not daily_metrics and not changelog:
        raise HTTPException(404, f"No data found for seller_id '{sid}'")

    mapping = {
        "gc":  _clean(m.get("growth_consultant_name")),
        "gm":  _clean(m.get("growth_manager_name")),
        "kae": _clean(m.get("key_account_executive_name")),
        "kam": _clean(m.get("key_account_manager_name")),
        "growth_lead":       _clean(m.get("growth_lead_name")),
        "golive_poc":        _clean(m.get("golive_poc_name")),
        "onboarding_poc":    _clean(m.get("onboarding_poc_name")),
        "assistant_manager": _clean(m.get("assistant_manager_name")),
        "profitability_am":  _clean(m.get("profitability_associate_manager_name")),
        "go_live":           _clean(m.get("go_live_sellers")),
    }
    # prefer card 10353; fall back to lighter card 10352 for the basic fields
    def pick(field):
        return _clean(b.get(field)) or _clean(b2.get(field))
    business = {
        "company":           pick("company"),
        "website":           pick("website"),
        "contact":           pick("seller_contact"),
        "offers":            pick("offers"),
        "products_at_go_live": _clean(b.get("products_at_go_live")),
        "live_skus":         _clean(cg.get("live_skus")),
        # AOV from card 1880 (spend>3540 week); COGS from card 2497 (cosgs). Fall back to 10353.
        "aov_at_gl":         pnl_aov if pnl_aov is not None else _clean(b.get("aov_at_gl")),
        "cogs_at_gl":        _clean(cg.get("cosgs")) if _clean(cg.get("cosgs")) is not None else _clean(b.get("cogs_at_gl")),
        "cancellation_pct":  cancellation.get("recent_pct"),   # recent 2-week cancellation % (card 1880)
        "cancellation_high": cancellation.get("high"),         # True if > 5% (bleeds P&L)
        "website_source":    (BUSINESS_CARD if _clean(b.get("website")) else
                              (BUSINESS_BACKUP_CARD if _clean(b2.get("website")) else None)),
    }

    # spend details (2787 today/yesterday/lifetime/first + 10065 total/last)
    spend = {
        "today":      _clean(sp1.get("today_spend")),
        "yesterday":  _clean(sp1.get("yesterday_spend")),
        "total":      _clean(sp2.get("total spend")) if _clean(sp2.get("total spend")) is not None else _clean(sp1.get("lifetime_spend")),
        "first_date": _clean(sp1.get("first_spend_date")) or _clean(sp2.get("first spend date")),
        "last_date":  _clean(sp2.get("last spend date")),
    }

    # category levels (card 3757): coherent chain L1 > L2 > L3 > L4 from the dominant product category
    l1 = _clean(cat.get("l1"))
    l2 = _clean(cat.get("l2"))
    l3 = _clean(cat.get("l3"))
    l4 = _clean(cat.get("l4"))
    category = {"l1": l1, "l2": l2, "l3": l3, "l4": l4}
    # hard-coded benchmark keyed on primary_l2 (the CSV benchmark key)
    benchmark = BENCHMARKS.get(l2) if l2 else None

    # derived readiness status (honest: based only on fields we actually have)
    if mapping["go_live"] == "Yes":
        status = ["Live", "s-ok"] if mapping["gm"] else ["Live · unassigned", "s-warn"]
    elif mapping["go_live"] == "No":
        status = ["Not live yet", "s-warn"]
    else:
        status = ["Unknown", "s-warn"]

    return {
        "seller_id": sid,
        "company": business["company"] or f"Seller {sid}",
        "mode": req.mode,
        "status": status,
        "mapping": mapping,
        "business": business,
        "spend": spend,
        "category": category,
        "benchmark": benchmark,
        "range": {"start": start, "end": end},
        "daily_metrics": daily_metrics,
        "pq": pq,
        "changelog": changelog,
        "demand_products": demand_products,
        "weekly_pnl": weekly_pnl,
        "buckets": buckets,
        "last_ts": last_ts,
        "pnl_csv": pnl_csv,
        "pnl_weeks": pnl_weeks,
    }


# ----------------------------------------------------------------------------
# Claude-powered action plan
# ----------------------------------------------------------------------------
PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "website":  {"type": "array", "items": {"$ref": "#/$defs/action"}},
        "campaign": {"type": "array", "items": {"$ref": "#/$defs/action"}},
        "outside":  {"type": "array", "items": {"$ref": "#/$defs/action"}},
        "scaleup":  {"type": "array", "items": {"$ref": "#/$defs/action"},
                     "description": "2K->5K scaling plan (1k-5k track only; empty for HITS)"},
        "summary":  {"type": "string", "description": "2-4 sentence executive correlation summary across all categories"},
        "top_priorities": {"type": "array", "items": {"type": "string"},
                           "description": "3-5 highest-impact actions across all categories, in order, each prefixed with its area"},
    },
    "required": ["website", "campaign", "outside"],
    "$defs": {
        "action": {
            "type": "object",
            "properties": {
                "t": {"type": "string", "description": "Short action title"},
                "d": {"type": "string", "description": "1-2 sentence concrete how-to"},
                "p": {"type": "string", "enum": ["high", "med", "low"]},
            },
            "required": ["t", "d", "p"],
        }
    },
}

# Condensed 2K->5K Scaling Playbook (from ShopDeck playbook doc) — injected for the
# 1k-5k track so the scaleup plan follows the real methodology, not generic advice.
SCALEUP_PLAYBOOK = """2K->5K SCALING PLAYBOOK (for already-profitable/HIT sellers at ~1-2K/day, scaling to 5K/day profitably):

PRECONDITIONS (confirm before scaling): 2 full weeks profitable post-HIT; Marketing% below breakeven; RTO within category norm; stock can absorb 2-3x volume. If any fail -> fix first, don't scale.

PHASE 1 DIAGNOSIS:
- Product spend concentration: >=60-65% on 1 product = dominant (single-product RISK; build a 2nd product before 5K); 40-60% across 2 = push both; <40% distributed = healthy. Count UNIQUE products (variants=1). <=2 unique = high risk at 5K.
- Audience: LAL running -> open audience keeping Age/Gender/Region (don't just raise LAL budget, it exhausts); heavy restrictions -> remove 1-2 filters every 48h ONLY if RTO ok; already Open -> skip audience, focus creatives. RTO GATE: if campaign RTO >20% above account avg, DO NOT open audience; fix RTO/NDR first.
- Stock gate: OOS -> pause creatives; Low -> confirm restock ETA; OK -> proceed. Check before every new creative + budget increase.

PHASE 2 CAMPAIGN MATRIX: launch NEW campaigns in parallel (never touch the HIT campaign). Budgets: Rs250/creative (banner+video combo), Rs500/new creative or product test, Rs2000 minimum spend before judging a new product. 1 dominant product -> new creative same product Rs500 + new-product creative Rs500. 2 products -> push the lesser-spend product's creative in same audience. >10 products -> isolate winners into own campaigns.
Verdict logic: campaign S:GMV% < account avg = over-performing (scale it); > avg = under-performing (pause/restructure); no sales after Rs2000 = stop; CTR<1% = creative/hook issue; stable CPA 2-3 days = scale signal.

PHASE 3 SCALING 2K->5K: only after 2 proven campaigns. Scale 20-30%/day (never jump 2K->5K in one day), spread across MULTIPLE campaigns (never all budget in 1), check Marketing% daily, confirm stock before each increase, keep HIT campaign as anchor. If Marketing% > breakeven for 2 consecutive days -> pause scaling and diagnose. Don't change creative + budget at the same time. Don't scale into a seasonal spike (may be a false HIT).

RED FLAGS: OOS mid-scale (algorithm shifts to inferior products, Marketing% spikes) -> build 2nd product + stock alerts; RTO signature (Marketing% high but CPM/CTR/C2PR fine, W-2 loss) -> exclude high-RTO states (J&K, Bihar, UP, Assam), NDR calling if RTO>30%; False HIT (profitable only during festival/1 product) -> don't allocate scale budget, re-validate in 2 weeks.

BEYOND 5K (HIT2 = 2 profitable weeks at 5K): retargeting layer at ~20% of budget (7d/14d/30d warm, CPP where CTR>4%, Website Visitor for 2K+ weekly visitors, Purchase LLA for 50+ lifetime purchases).

ICP: High ICP (>=7) + High Spend = scale fast (best success ~37%); Low ICP + Low Spend = hold & validate (~20%). More unique products + one proven creative format + RTO in range + responsive seller/stock = the pattern of accounts that scaled."""


# Distilled from the 6 ShopDeck troubleshooting methodology docs. Editable in Upstash (kb:methodology);
# this is the fallback/default. Injected into the plan so the AI reasons like a senior TS analyst.
TROUBLESHOOT_METHODOLOGY = """SHOPDECK TROUBLESHOOTING METHODOLOGY — reason like a senior analyst, in this order:

1) CONTEXT FIRST (never diagnose cold). Same numbers mean different things by context: seller persona
(profit-obsessed & RTO-sensitive vs growth/risk-taker), products (AOV, marketplaces, USP, PQ; ShopDeck price
is often ~20% above marketplace = a demand/RTO driver), account HISTORY (find the MOST PROFITABLE period —
that is the 'normal' to rebuild toward), and the LAST troubleshoot (was it implemented? did it work?).
Read as 'recoverable' if a profitable period exists, 'structural' if pricing/mix/geo work against them.
ALWAYS separate RTO problems from profitability problems — different levers.

2) INFLECTION POINT (when did it change). Find the week Profit% flipped/declined; for a sudden change the
week is obvious, for gradual find where the decline STARTED (not just the biggest drop). Drop to day-level
for the exact day. Default weekly; for RTO use weekly (daily RTO is noise). Compare spend AND order volume
together — a spend rise matched by proportional purchases is NOT an anomaly. Anchor everything to the
seller's own inflection/best week.

3) METRIC IDENTIFICATION (what broke). Independent levers: Marketing (spend/GMV), RTO, Cancellation
(seller-side & customer-side), Logistics/forward charge, Pricing (COGS+AOV). Net P&L depends on all.
Pick the metric with the MOST P&L impact (mentally: bring it back to its profitable value — does P&L
recover? if not, next metric). Isolate ONE metric at a time. Priority: RTO and Marketing spend/GMV are
usually highest impact; Cancellation and Pricing secondary.

4) USE CASE -> direction. Marketing: never-picked (ramp), fluctuating (>~15% week-to-week variance ->
stabilise to the good-day pattern), recent-drop (recover to prior level), worse-with-scaling (pull back /
fix before scaling). RTO: always-high (manage, not the primary lever), increased-over-time (restore to last
stable week), fluctuating (stabilise), improving (fine-tune). Pricing: unexplained change (reconcile),
find-right-price (test).

BENCHMARKS: spend/GMV ~13.5% is the profitability line (product-level ideal 40-45% for AOV 500-1000).
Break-even ROAS rises with RTO (~1.65x at 0% RTO -> ~2.1x at 31% RTO). Need ~30 orders (or 3 days) before
trusting a segment/campaign's RTO or profit. Any campaign >5% of spend/GMV (account >20%) should be
rebalanced IF it has enough data.

READING DATA VIEWS (right grain, right order): Change Log FIRST — find the exact date a metric shifted and
diff before/after (catalogue->Marketing; best/worst-seller toggles->Marketing+RTO; cost-price->COGS;
product-page/website->RTO; payment settings->Marketing+RTO). Cuts: state, CITY (a state's high RTO is often
ONE city — remove the city, not the state), tier, campaign, LP/courier, payment mode, gender, age,
platform/placement, ad set (audience), ad (creative). OOS: check the marketing-log date range.
EXCLUDE a segment only if it stays worse over a meaningful window (e.g. RTO drives >10% loss) AND has >=30
orders. Removing age/gender/placement is HIGH RISK (blocks learning/reach) — fix creative/audience first.
DOUBLE DOWN via NEW campaigns (low risk) as a tracked trial, then revert or scale.

SMALL-ACTION CHECKLIST (fix red flags): O2S <=1.5d, S2A <=4.5d, FAD >=55%, seller-cancellation ->0,
customer-cancellation 0-15%, PPH>3d = 0, COGS 25-50%, PPO->BuyNow >10%, BuyNow->Address >32%,
Address->Charged >=55%, live products >30, product groups >=4, retargeting-coupon conv >13%, avg discount
<50%. Config ON: online payment + discount, partial COD + discount, Pixel, CAPI, ad-account id. Website:
clean photos (no text), size charts, testimonials, ATC coupons >=2, post-delivery 20%-off coupon, Bumper
timer 240min. Campaign setup: objective=Sales, Advantage Campaign Budget ON, maximise conversions, purchase
event, Advantage+ placements ON, Multi-advertiser ON, correct URL params, Instagram connected.

CONTEXT SHIFTS: online-payment share depends on trust/rating — fix audience before restricting payment.
Budget-mix skew (e.g. 30% forced to banners+videos) is a red flag — rebalance toward A+A + retargeting.
RTO fix != profitability fix (an LP/courier change may not lower RTO)."""


_kb_cache = {"text": None, "ts": 0}


def _get_methodology():
    """Return the troubleshooting methodology KB — Upstash (editable) first, else the built-in default. Cached 1h."""
    if _kb_cache["text"] is not None and time.time() - _kb_cache["ts"] < 3600:
        return _kb_cache["text"]
    text = TROUBLESHOOT_METHODOLOGY
    try:
        v = _redis(["GET", "kb:methodology"])
        if v and isinstance(v, str) and len(v) > 200:
            text = v
    except Exception:
        pass
    _kb_cache["text"] = text
    _kb_cache["ts"] = time.time()
    return text


CSV_CHAR_LIMIT = 35000  # cap each CSV sent to the model (keeps token use + latency sane)


def _summarize_csv(text):
    """Big row-level CSVs (e.g. Meta breakdown, 1000s of rows) are aggregated by campaign/ad-set
    so the model gets a clean ~30-row digest instead of drowning in raw rows (which caused runaway output)."""
    import csv as _c
    import io
    text = (text or "").strip()
    if not text:
        return ""
    try:
        rows = list(_c.reader(io.StringIO(text)))
    except Exception:
        return text[:CSV_CHAR_LIMIT]
    if len(rows) < 2:
        return text[:CSV_CHAR_LIMIT]
    header, data = rows[0], rows[1:]
    if len(data) <= 60:
        return text[:CSV_CHAR_LIMIT]  # small enough already

    def num(v):
        try:
            return float(str(v).replace(",", "").replace("%", "").replace("₹", "").strip())
        except (TypeError, ValueError):
            return None

    gi = next((i for i, h in enumerate(header)
               if any(k in str(h).lower() for k in ("campaign", "ad set", "adset", "ad name"))), None)
    if gi is None:
        out = [",".join(header)] + [",".join(r) for r in data[:60]]
        return "\n".join(out) + f"\n...[{len(data)} rows total; first 60 shown]"

    sample = data[:300]
    # sum only ADDITIVE metrics; drop rate/ratio columns (summing CPM/CTR/ROAS is meaningless —
    # the model derives rates from the summed spend/impressions/results instead)
    RATE_HINTS = ("cpm", "ctr", "roas", "rate", "cost per", "c2pr", "frequency", "%", "per 1,000", "budget type")
    numeric_cols = [i for i in range(len(header)) if i != gi
                    and not any(h in str(header[i]).lower() for h in RATE_HINTS)
                    and sum(1 for r in sample if i < len(r) and num(r[i]) is not None) > len(sample) * 0.5]
    agg = {}
    for r in data:
        if gi >= len(r):
            continue
        g = r[gi] or "(blank)"
        a = agg.setdefault(g, {"__rows": 0})
        a["__rows"] += 1
        for i in numeric_cols:
            if i < len(r):
                v = num(r[i])
                if v is not None:
                    a[i] = a.get(i, 0.0) + v
    key = numeric_cols[0] if numeric_cols else None
    groups = sorted(agg.items(), key=lambda kv: kv[1].get(key, 0) if key is not None else kv[1]["__rows"], reverse=True)[:30]
    out_header = [header[gi], "rows"] + [header[i] for i in numeric_cols]
    lines = [",".join('"' + str(h) + '"' for h in out_header)]
    for g, a in groups:
        row = [g, a["__rows"]] + [round(a.get(i, 0.0), 2) for i in numeric_cols]
        lines.append(",".join('"' + str(x) + '"' for x in row))
    note = f"...[aggregated {len(data)} rows into {len(agg)} campaigns; top {len(groups)} by {header[key] if key is not None else 'row count'}]"
    return "\n".join(lines) + "\n" + note


def _csv_block(title, text):
    text = (text or "").strip()
    if not text:
        return f"### {title}\n(none provided)\n"
    text = _summarize_csv(text)
    truncated = ""
    if len(text) > CSV_CHAR_LIMIT:
        text = text[:CSV_CHAR_LIMIT]
        truncated = f"\n...[truncated to {CSV_CHAR_LIMIT} chars]"
    return f"### {title}\n```csv\n{text}{truncated}\n```\n"


# ---- multi-agent helpers ----------------------------------------------------
ACTIONS_SCHEMA = {
    "type": "object",
    "properties": {"actions": {"type": "array", "items": {"$ref": "#/$defs/action"}}},
    "required": ["actions"],
    "$defs": {"action": {"type": "object", "properties": {
        "t": {"type": "string"}, "d": {"type": "string"},
        "p": {"type": "string", "enum": ["high", "med", "low"]}}, "required": ["t", "d", "p"]}},
}

SYNTH_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "2-4 sentence executive correlation summary"},
        "top_priorities": {"type": "array", "items": {"type": "string"},
                           "description": "3-5 highest-impact actions across all categories, in order"},
    },
    "required": ["summary", "top_priorities"],
}

SPECIALIST_ROLES = {
    "website":  "a website / CRO specialist. Focus ONLY on the storefront as a conversion surface: "
                "landing page & PDP quality, pixel/Conversions API tracking, catalogue hygiene, load speed, "
                "trust/pricing/reviews, and any changelog website change that hurt conversion.",
    "campaign": "a Meta ads campaign specialist. Focus ONLY on ad-account structure: campaign/ad-set/creative "
                "performance, ROAS vs the P&L break-even ROAS, CTR/CPM/frequency, audience, budget allocation, "
                "bidding — which campaigns to cut, cap, or scale.",
    "outside":  "an out-of-the-box growth specialist. Focus ONLY on levers OUTSIDE website+campaigns: retention "
                "(WhatsApp/win-back), COD->prepaid & RTO reduction, margin/COGS, pricing to the demand band, "
                "creator/UGC, incentives.",
    "scaleup":  "a 2K->5K scaling specialist. Apply the ShopDeck scaling playbook to THIS seller's data "
                "(product concentration, audience opening, stock/RTO gates, campaign matrix, 20-30%/day, "
                "HIT2 retargeting, ICP). Playbook:\n\n" + SCALEUP_PLAYBOOK,
}


def _anthropic():
    try:
        import anthropic
    except ImportError:
        raise HTTPException(500, "anthropic package not installed")
    return anthropic.Anthropic(api_key=ANTHROPIC_API_KEY,
                               **({"base_url": ANTHROPIC_BASE_URL} if ANTHROPIC_BASE_URL else {}))


def _tool_call(client, system, user, schema, max_tokens=1600, tool="submit", model=None):
    try:
        msg = client.messages.create(
            model=model or CLAUDE_MODEL, max_tokens=max_tokens, system=system,
            tools=[{"name": tool, "description": "Submit structured output.", "input_schema": schema}],
            tool_choice={"type": "tool", "name": tool},
            messages=[{"role": "user", "content": user}])
    except Exception as e:
        msg_txt = str(e)
        code = 429 if ("budget" in msg_txt.lower() or "rate" in msg_txt.lower() or "429" in msg_txt) else 502
        raise HTTPException(code, f"Claude error: {msg_txt}")
    for b in msg.content:
        if getattr(b, "type", None) == "tool_use" and b.name == tool:
            return b.input
    raise HTTPException(502, "no structured output from Claude")


def _prior_block(prior):
    """Compact text of the AI's own past plans for this seller (memory)."""
    if not prior:
        return ""
    lines = ["=== THE AI'S OWN PAST PLANS FOR THIS SELLER (most recent first) ==="]
    for p in prior[:5]:
        if not isinstance(p, dict):
            continue
        lines.append(f"[{p.get('date', '?')}] {str(p.get('summary', ''))[:220]}")
        tp = p.get('top_priorities') or []
        if tp:
            lines.append("  do-first: " + " | ".join(str(x)[:90] for x in tp[:5]))
    return "\n".join(lines) + "\n"


def _data_block(req, website):
    return (
        f"Website: {website or '(not available)'}\n\n"
        f"Seller account context (JSON — includes daily_metrics, weekly_pnl, buckets, last_ts, "
        f"changelog, demand_products, pq):\n{json.dumps(req.seller, indent=2)}\n\n"
        "=== UPLOADED DATA ===\n"
        + _csv_block("P&L (profit & loss)", req.pnl_csv)
        + _csv_block("Meta ad-account metrics", req.meta_csv)
        + "".join(_csv_block(f"Additional: {f.name}", f.content) for f in (req.extra_csv or []))
    )


@app.post("/api/plan")
def plan(req: PlanReq):
    if not ANTHROPIC_API_KEY:
        raise HTTPException(500, "ANTHROPIC_API_KEY not configured")
    website = (req.website or req.seller.get("business", {}).get("website") or "").strip()
    track = "HITS-managed account" if req.mode == "hits" else "seller in the 1k–5k weekly-spend band"

    # ---- STAGE 1: analyst — read the raw data once, emit a compact factual digest ----
    if req.stage == "analyst":
        if not req.pnl_csv.strip() or not req.meta_csv.strip():
            raise HTTPException(400, "Both a P&L CSV and a Meta ad-account metrics CSV are required")
        client = _anthropic()
        sysp = (
            "You are a performance-marketing DATA ANALYST for an Indian D2C marketplace. Read the seller "
            "context + P&L CSV + Meta metrics CSV and produce a TERSE, quantitative digest that specialist "
            "strategists will build on. Cover, with numbers: break-even ROAS (from COGS+shipping+RTO); each "
            "Meta campaign's spend/ROAS/CTR/CPM + over/under-performing verdict vs break-even; gross/"
            "contribution margin & net profit; RTO signal; weekly P&L trajectory + bucket status; product "
            "concentration & demand-validated winners (mp ratings); any changelog change that correlates with "
            "a metric drop (with dates); PQ trend; and the 4-6 biggest risks & opportunities. No fluff, no "
            "recommendations yet — just the facts and verdicts. Use short bullet lines. "
            "If PAST AI PLANS are shown, add a short 'FOLLOW-UPS' section flagging which prior recommendations "
            "the current data suggests are done/resolved vs still open."
        )
        prior = _prior_block(req.prior_plans)
        user_content = _data_block(req, website) + ("\n" + prior if prior else "")
        try:
            digest = "".join(b.text for b in client.messages.create(
                model=ANALYST_MODEL, max_tokens=1700, system=sysp,
                messages=[{"role": "user", "content": user_content}]).content
                if getattr(b, "type", None) == "text")
        except Exception as e:
            raise HTTPException(502, f"Claude error (analyst): {e}")
        return {"digest": digest.strip()}

    # ---- STAGE 2: specialist — one focused agent per category, off the digest ----
    if req.stage == "specialist":
        cat = req.category
        if cat not in SPECIALIST_ROLES:
            raise HTTPException(400, f"unknown category '{cat}'")
        client = _anthropic()
        sysp = (
            f"You are {SPECIALIST_ROLES[cat]}\n\nGiven the analyst's data digest below, produce 3-6 concrete, "
            "prioritized actions ONLY for your area. Each action must cite the actual numbers/dates that "
            "justify it. priority: 'high' = money-losing blocker / caused a drop, 'med' = clear lever, "
            "'low' = nice-to-have. Return via the submit tool."
        )
        user = f"Seller track: {track}.\nWebsite: {website or '(n/a)'}\n\n=== ANALYST DIGEST ===\n{req.digest}"
        # single call only — retry (if empty) is done by the frontend as a SEPARATE request
        # so one request never risks the serverless 60s timeout.
        out = _tool_call(client, sysp, user, ACTIONS_SCHEMA, max_tokens=1500, model=SPECIALIST_MODEL)
        return {"category": cat, "actions": out.get("actions", [])}

    # ---- STAGE 3: synthesizer — CORRELATE only (specialists own their categories) ----
    if req.stage == "synth":
        client = _anthropic()
        sysp = (
            "You are the LEAD strategist. You are given a data digest and the specialist agents' draft actions "
            "(website, campaign, outside" + (", scaleup" if req.mode != "hits" else "") + "). Do NOT rewrite "
            "the per-category actions. Instead CORRELATE across them: (1) write a 2-4 sentence executive "
            "'summary' — what is really driving the problem and the single most important next move, tying "
            "together the cross-cutting levers (e.g. an RTO/margin issue that gates scaling, or a website "
            "change that caused a campaign drop); (2) return 'top_priorities' — the 3-5 highest-impact actions "
            "ACROSS ALL categories, in order, each as one short imperative line prefixed with its area "
            "(e.g. 'Campaign: cut the Open campaign at 2.2x — below break-even'). Return via submit."
        )
        if req.prior_plans:
            sysp += (" IMPORTANT: past AI plans for this seller are provided below. In the summary, note the "
                     "PROGRESS since last time (which prior recommendations look done/resolved vs still open). "
                     "STILL return 3-5 top_priorities; for items repeated from a past plan, escalate them to a "
                     "stronger/next step rather than restating them verbatim.")
        user = (
            f"Seller track: {track}.\n\n=== ANALYST DIGEST ===\n{req.digest}\n\n"
            f"=== SPECIALIST DRAFTS (JSON) ===\n{json.dumps(req.drafts, indent=2)}\n\n"
            + _prior_block(req.prior_plans)
        )
        return _tool_call(client, sysp, user, SYNTH_SCHEMA, max_tokens=1200, tool="submit", model=SYNTH_MODEL)

    # ---- STAGE "full": legacy single-call (fallback) ----
    if not req.pnl_csv.strip() or not req.meta_csv.strip():
        raise HTTPException(400, "Both a P&L CSV and a Meta ad-account metrics CSV are required")
    try:
        import anthropic
    except ImportError:
        raise HTTPException(500, "anthropic package not installed (pip install anthropic)")

    client = anthropic.Anthropic(
        api_key=ANTHROPIC_API_KEY,
        **({"base_url": ANTHROPIC_BASE_URL} if ANTHROPIC_BASE_URL else {}),
    )
    website = (req.website or req.seller.get("business", {}).get("website") or "").strip()

    sys_prompt = (
        "You are a senior Meta (Facebook/Instagram) performance-marketing strategist for an "
        "Indian D2C marketplace's growth team. You are given a seller's account context (which "
        "includes a DAILY METRICS time series and a CHANGELOG of recent setting changes), their "
        "P&L data, their Meta ad-account metrics, and their website. ANALYSE THE DATA DEEPLY "
        "before recommending anything:\n"
        "- From the P&L: read revenue, COGS, gross/contribution margin, marketing spend, RTO/returns, "
        "shipping, net profit/loss. Identify what is bleeding money and the break-even ROAS.\n"
        "- From the Meta metrics: read spend, ROAS, CTR, CPM, CPC, CPP, AOV, frequency, conversion rate "
        "by campaign/ad set/creative. Find the winners, the wasted spend, and the funnel bottleneck.\n"
        "- From daily_metrics (date, spend, cpm, ctr, s_gmv, orders, gmv): detect any DROP or SPIKE — "
        "e.g. CTR falling, CPM rising, spend/gmv (s_gmv) worsening — and note the date it started.\n"
        "- From changelog (dated setting changes — website, catalogue, coupon, shipping, meta/google "
        "settings, payment, product page): for each performance drop, CORRELATE it with changes made "
        "right before that date and FLAG the suspected cause (e.g. 'CTR dropped 40% on 14 Jun, one day "
        "after a Website NavigationBar change on 13 Jun — likely culprit, revert/test'). Be explicit "
        "about the correlation and your confidence; say so if there's no clear link.\n"
        "- From weekly_pnl (w1/w2/w3 spend and P&L%): read the 3-week trajectory — is P&L improving or "
        "declining, did it turn negative, is spend rising while P&L falls? Call out the trend explicitly. "
        "The seller data also includes 'buckets' (bucket_health / potential / objective / subjective / "
        "missed_hit true/false per HIT rules: spend>3540 & P&L thresholds). Use the bucket status to frame "
        "how close the account is to a stable HIT and what specifically is missing to reach the next bucket.\n"
        "- From last_ts (the PREVIOUS troubleshoot): last_ts_actions is what was advised last time (on "
        "last_ts_date). CHECK the current data to judge whether those actions were done and whether they "
        "worked (e.g. if last TS said 'reduce cancellation >5%', is cancellation still high now?). Do NOT "
        "repeat actions that are now resolved; ESCALATE ones still unresolved; then give the NEXT actions. "
        "Explicitly reference the last TS in at least one action (e.g. 'Last TS on 30 Jun flagged X — still "
        "unresolved, escalate by …').\n"
        "- From demand_products (per-product marketplace demand validation): mp_rating_count is the "
        "external marketplace lifetime rating count for the matched product — high count = PROVEN demand. "
        "FLAG which products are SAFE to scale campaigns on (validated demand, mp_rating_count >= 20) vs "
        "those to HOLD (little/no marketplace demand). Recommend concentrating ad budget/catalogue pushes "
        "on validated-demand products, and check our_price sits within the demand price band "
        "(demand_price_min..demand_price_avg..demand_price_max) — flag mispriced winners.\n"
        "- Cross-reference: is the Meta ROAS above the P&L break-even ROAS? Are margins enough to scale?\n"
        "- Consider the website as the conversion surface.\n"
        "Then produce a concrete, prioritized next-plan-of-action. Every action must be specific and "
        "cite the actual numbers/dates/changes that justify it (e.g. 'COGS is 47% so break-even ROAS is "
        "~2.1x, but campaign X runs at 1.4x — cut it'). Lead with any change-caused-drop fixes. "
        "Return 3-6 actions per category. "
        "Priority 'high' = money-losing blocker or a change that caused a drop, 'med' = clear lever, "
        "'low' = nice-to-have."
    )

    is_growth = req.mode != "hits"
    if is_growth:
        sys_prompt += (
            "\n\nThis seller is on the 1k-5k SCALING track (already profitable at ~1-2K/day, goal 5K/day "
            "profitably). In ADDITION to the 3 categories, produce a 'scaleup' category (4-6 actions) that "
            "applies the following ShopDeck 2K->5K playbook to THIS seller's actual data (product "
            "concentration from demand_products, weekly P&L trajectory, buckets, RTO/COGS, PQ). Be specific: "
            "name the diagnosis, the exact next campaign moves, budget steps (Rs500 tests, Rs2000 eval, "
            "20-30%/day), and the gates (stock, RTO, single-product risk).\n\n" + SCALEUP_PLAYBOOK
        )

    # plain-English style + a concrete "new campaign" blueprint (applies to the single-pass plan)
    sys_prompt += (
        "\n\nWRITING STYLE — write for a busy operations manager, NOT an analyst:\n"
        "- Action title: a short, direct instruction in plain words, max ~9 words (e.g. 'Turn off the engagement campaign').\n"
        "- Description: 2-4 SHORT sentences in simple English. Say WHAT to do first, then a one-line WHY with the key number.\n"
        "- Minimise jargon. The first time you use a term like ROAS, CPM, AOV, break-even, A+A, LAL, place a 3-6 word plain "
        "meaning in brackets, e.g. 'ROAS (revenue per rupee spent)'. No dense analyst paragraphs.\n"
        "- Always ground it in the seller's real ₹ numbers.\n"
        "CAMPAIGN CATEGORY — besides the fixes, you MUST include ONE action titled like 'Build this new campaign' that is a "
        "step-by-step BLUEPRINT for the campaign to create now. Put each item on its own line in the description:\n"
        "  • Objective: (e.g. Sales / Purchase conversions)\n"
        "  • Daily budget: total ₹ and how to split it\n"
        "  • Ad sets: how many and how structured (e.g. 1 broad Advantage+ ad set)\n"
        "  • Audience: exactly who (Advantage+ / broad / interest + age/gender/region) and why\n"
        "  • Placements: which ones to keep on (and which to turn off)\n"
        "  • Creatives: how many and which formats (image/video/UGC/catalogue)\n"
        "  • Campaign name: a clear name to use\n"
        "  • Scale rule: when to raise budget and by how much\n"
        "Make the blueprint copy-paste ready so they can build it without guessing.\n"
    )
    # inject the ShopDeck troubleshooting methodology so the AI reasons like a senior analyst
    sys_prompt += "\n\n=== APPLY THIS TROUBLESHOOTING METHODOLOGY ===\n" + _get_methodology()

    # MANDATORY flag: high cancellation must always produce an action
    if req.seller.get("business", {}).get("cancellation_high"):
        cp = req.seller.get("business", {}).get("cancellation_pct")
        sys_prompt += (
            f"\n\nMANDATORY: this seller's recent 2-week CANCELLATION is {cp}% (card 1880), well above the "
            "5% ideal. You MUST include a HIGH-priority action (in 'outside') telling the growth team to get "
            "the seller to STOP cancelling orders — seller-side cancellations inflate CPP (cost per purchase) "
            "and directly bleed P&L. Cite the exact cancellation% and the <5% target.")

    cats = (
        "1. website  — landing page / storefront / pixel / catalogue / CRO fixes\n"
        "2. campaign — Meta ad structure, creative, audience, budget, bidding actions\n"
        "3. outside  — out-of-the-box / retention / incentive / margin / creator plays\n"
    )
    if is_growth:
        cats += "4. scaleup  — the 2K->5K scaling plan per the playbook (diagnosis -> matrix -> scale gates)\n"
    else:
        cats += "Return an empty 'scaleup' array (not applicable to the HITS track).\n"
    cats += ("ALSO return: 'summary' (2-4 sentence executive correlation of the biggest cross-cutting "
             "levers) and 'top_priorities' (3-5 highest-impact actions across ALL categories, in order, each "
             "prefixed with its area, e.g. 'Campaign: cut the Open campaign at 2.2x').\n")
    if req.prior_plans:
        cats += ("This seller has PAST AI plans (below). Do a LEARNING REVIEW: compared to the last plan and "
                 "current data, explicitly flag in the summary (and fold into actions) — IMPROVED (what got "
                 "better since last TS -> keep running), WORSENED (what got worse), REVERT (a past change that "
                 "hurt and should be undone), and KEEP (working actions to continue). Don't just restate "
                 "unresolved items — escalate them to a stronger next step.\n")

    user_prompt = (
        f"Seller track: {track}.\n"
        f"Website: {website or '(not available)'}\n\n"
        f"Seller account context (JSON):\n{json.dumps(req.seller, indent=2)}\n\n"
        "=== UPLOADED DATA ===\n"
        + _csv_block("P&L (profit & loss)", req.pnl_csv)
        + _csv_block("Meta ad-account metrics", req.meta_csv)
        + "".join(_csv_block(f"Additional: {f.name}", f.content) for f in (req.extra_csv or []))
        + ("\n" + _prior_block(req.prior_plans) if req.prior_plans else "")
        + "\nAnalyse the above deeply, then call submit_plan with the action plan across:\n"
        + cats
    )
    try:
        msg = client.messages.create(
            model=PLAN_MODEL,
            max_tokens=4500 if is_growth else 3800,
            system=sys_prompt,
            tools=[{
                "name": "submit_plan",
                "description": "Submit the structured action plan (all categories + summary + top_priorities).",
                "input_schema": PLAN_SCHEMA,
            }],
            tool_choice={"type": "tool", "name": "submit_plan"},
            messages=[{"role": "user", "content": user_prompt}],
        )
    except Exception as e:
        msg_txt = str(e)
        code = 429 if ("budget" in msg_txt.lower() or "429" in msg_txt) else 502
        raise HTTPException(code, f"Claude error: {msg_txt}")

    for block in msg.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "submit_plan":
            out = block.input
            # defensive cap: never let a runaway (e.g. 1000+ items) reach the UI
            for k in ("website", "campaign", "outside", "scaleup"):
                if isinstance(out.get(k), list):
                    out[k] = out[k][:8]
            if isinstance(out.get("top_priorities"), list):
                out["top_priorities"] = out["top_priorities"][:6]
            return out
    raise HTTPException(502, "Claude did not return a structured plan")


# ----------------------------------------------------------------------------
# Account Overview / RCA Engine — what actually happened in the account
# (spend, orders, cancellations, funnel-stage diagnosis) + a multi-angle account
# story (GC / KAM / Seller / requests / escalations) built from card 9293.
# ----------------------------------------------------------------------------
OVERVIEW_MODEL = os.environ.get("OVERVIEW_MODEL", CLAUDE_MODEL)  # descriptive RCA; Opus by default

OVERVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string",
                     "description": "2-3 sentence plain-English summary of what happened in this account and the money outcome"},
        "spend_orders": {"type": "array", "items": {"$ref": "#/$defs/stat"},
                         "description": "the money & orders facts: total spend, where it went, orders placed, orders "
                                        "cancelled, delivered/RTO, GMV/revenue, ROAS, AOV — each as label+value(+note)"},
        "funnel": {"type": "array", "items": {"$ref": "#/$defs/stage"},
                   "description": "funnel TOP to BOTTOM: Reach (CPM) -> Clicks (CTR) -> Cost per click (CPC) -> "
                                  "Landing/Add-to-cart -> Purchase (conversion/CPP) -> Delivery (RTO/cancellation). "
                                  "One entry per stage that the data supports, in order."},
        "what_happened": {"type": "array", "items": {"type": "string"},
                          "description": "chronological bullet points of the key events in the account (money + comms)"},
        "account_story": {"$ref": "#/$defs/story"},
        "weekly_progress": {"type": "array", "items": {"$ref": "#/$defs/week"},
                            "description": "CHRONOLOGICAL (oldest week first) week-by-week progress of the account: "
                                           "what happened each week across communication, issues, SOS/escalations, "
                                           "troubleshoot actions, and metrics/spend progress. Only include weeks that "
                                           "have some activity in the provided data."},
        "troubleshoot_lens": {"type": "array", "items": {"$ref": "#/$defs/ts_item"},
                              "description": "per PAST troubleshoot workflow (most recent first): the problem it "
                                             "identified, the solution/action given, and whether later data shows it "
                                             "helped. Empty array if no troubleshoot history is provided."},
        "key_issues": {"type": "array", "items": {"type": "string"},
                       "description": "the main problems, most important first, each grounded in a number or event"},
    },
    "required": ["headline", "spend_orders", "funnel", "key_issues"],
    "$defs": {
        "stat": {"type": "object", "properties": {
            "label": {"type": "string"}, "value": {"type": "string"},
            "note": {"type": "string", "description": "short context, e.g. 'good' / 'above 5% threshold'"}},
            "required": ["label", "value"]},
        "stage": {"type": "object", "properties": {
            "stage": {"type": "string", "description": "stage name, e.g. 'Reach (CPM)'"},
            "metric": {"type": "string", "description": "the numbers for this stage, e.g. 'CPM ₹182 · 1.2M impressions'"},
            "verdict": {"type": "string", "enum": ["good", "ok", "bad"]},
            "read": {"type": "string", "description": "1-2 sentence plain read of what this stage tells us "
                                                      "(e.g. 'CPM is fine so ads ARE being shown, but CTR is low — "
                                                      "people see them and don't click, so the creative/hook is the problem')"}},
            "required": ["stage", "verdict", "read"]},
        "story": {"type": "object",
                  "description": "what was going on from each angle, built from the calls/chats/escalations. "
                                 "Leave a field as an empty string if there is no signal for it.",
                  "properties": {
                      "gc_side": {"type": "string", "description": "what the Growth Consultant did / said / committed"},
                      "kam_side": {"type": "string", "description": "what the Key Account Manager did / said"},
                      "seller_side": {"type": "string", "description": "the seller's mood, complaints and what they asked for"},
                      "seller_requests": {"type": "string", "description": "concrete requests the seller made"},
                      "escalations": {"type": "string", "description": "SOS / leadership escalations raised and why"}}},
        "ts_item": {"type": "object", "properties": {
            "date": {"type": "string", "description": "the troubleshoot date"},
            "ts_type": {"type": "string", "description": "auto_ts or manual_ts"},
            "problem": {"type": "string", "description": "the problem this troubleshoot identified (infer from the actions)"},
            "solution": {"type": "string", "description": "the key action(s)/solution given, in plain words"},
            "outcome": {"type": "string", "enum": ["helped", "partial", "did_not_help", "not_implemented", "unclear"],
                        "description": "whether LATER data (P&L trend, cancellation, funnel, repeat of the same action "
                                       "in a later TS) shows it worked"},
            "note": {"type": "string", "description": "1 sentence of evidence for the outcome, grounded in a number/trend"}},
            "required": ["date", "problem", "solution", "outcome"]},
        "week": {"type": "object", "properties": {
            "week": {"type": "string", "description": "week label + date range, e.g. '2026-W25 (16–22 Jun)'"},
            "communication": {"type": "string", "description": "key calls/chats that week — who reached out, what about"},
            "issues": {"type": "string", "description": "problems/red flags that surfaced that week"},
            "sos": {"type": "string", "description": "SOS / leadership escalations raised that week (empty if none)"},
            "troubleshoot": {"type": "string", "description": "troubleshoot actions/solutions given that week (empty if none)"},
            "progress": {"type": "string", "description": "metrics/spend/P&L movement and net progress that week"}},
            "required": ["week"]},
    },
}


# ---- team-shared RCA cache (Upstash) — reuse a recent overview instead of re-spending AI budget ----
RCA_CACHE_DAYS = int(os.environ.get("RCA_CACHE_DAYS", "5"))


def _iso(ts):
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(ts)) if ts else ""


def _rca_cache_get(seller_id):
    """Most recent cached RCA for a seller if within RCA_CACHE_DAYS, else None. Team-shared via Upstash;
    None if Upstash isn't configured. Adds age_days + generated_at."""
    sid = str(seller_id or "").strip()
    if not sid or not (UPSTASH_URL and UPSTASH_TOKEN):
        return None
    try:
        raw = _redis(["GET", "rca:" + sid])
        if not raw:
            return None
        obj = json.loads(raw)
        age = time.time() - obj.get("ts", 0)
        if age > RCA_CACHE_DAYS * 86400:
            return None
        obj["age_days"] = round(age / 86400, 1)
        obj["generated_at"] = _iso(obj.get("ts", 0))
        return obj
    except Exception:
        return None


def _rca_cache_set(seller_id, data):
    """Store an RCA (keyed by seller_id only) with a RCA_CACHE_DAYS TTL. No-op without Upstash."""
    sid = str(seller_id or "").strip()
    if not sid or not (UPSTASH_URL and UPSTASH_TOKEN):
        return
    try:
        _redis(["SET", "rca:" + sid, json.dumps({"ts": time.time(), "data": data})])
        _redis(["EXPIRE", "rca:" + sid, str(RCA_CACHE_DAYS * 86400)])
    except Exception:
        pass


@app.get("/api/overview_cache")
def overview_cache(seller_id: str = ""):
    """Peek the RCA cache for the popup: is there a fresh (<RCA_CACHE_DAYS) saved overview for this seller?
    Returns the cached data too, so 'use saved' needs no extra AI call."""
    sid = (seller_id or "").strip()
    if not (UPSTASH_URL and UPSTASH_TOKEN):
        return {"available": False, "cached": False}
    c = _rca_cache_get(sid) if sid else None
    if not c or not c.get("data"):
        return {"available": True, "cached": False}
    return {"available": True, "cached": True, "age_days": c.get("age_days"),
            "generated_at": c.get("generated_at"), "data": c["data"]}


def _overview_data_block(req, esc, ts):
    seller = req.seller or {}
    return (
        f"Seller account context (JSON — includes mapping[gc/gm/kam], business[cancellation_pct], daily_metrics "
        f"[date/spend/cpm/ctr/orders/gmv/s_gmv], weekly_pnl, spend, category, benchmark, demand_products):\n"
        f"{json.dumps(seller, indent=2)}\n\n"
        "=== UPLOADED DATA ===\n"
        + _csv_block("P&L (profit & loss)", req.pnl_csv)
        + _csv_block("Meta ad-account metrics", req.meta_csv)
        + "".join(_csv_block(f"Additional: {f.name}", f.content) for f in (req.extra_csv or []))
        + "\n" + _escalations_block(esc)
        + "\n" + _troubleshoots_block(ts)
    )


@app.post("/api/overview")
def overview(req: OverviewReq):
    """Account Overview / RCA — describe what happened in the account (money, orders, funnel) and tell the
    multi-angle story (GC / KAM / seller / requests / escalations). One Claude call, structured output."""
    if not ANTHROPIC_API_KEY:
        raise HTTPException(500, "ANTHROPIC_API_KEY not configured")
    if not (req.pnl_csv or "").strip() or not (req.meta_csv or "").strip():
        raise HTTPException(400, "Both a P&L CSV and a Meta ad-account metrics CSV are required")
    sid = str((req.seller or {}).get("seller_id") or "").strip()
    # Reuse a recent team-shared RCA instead of spending AI budget again (unless force=True).
    if sid and not req.force:
        cached = _rca_cache_get(sid)
        if cached and cached.get("data"):
            out = dict(cached["data"])
            out["cached"] = True
            out["generated_at"] = cached.get("generated_at")
            out["age_days"] = cached.get("age_days")
            return out
    days = req.window_days or ESCALATION_WINDOW_DAYS
    esc = _safe_call(_get_escalations, sid, days, default={}) if sid else {}
    tsh = _safe_call(_get_troubleshoots, sid, default=[]) if sid else []

    track = "HITS-managed account" if req.mode == "hits" else "seller in the 1k-5k weekly-spend band"
    sysp = (
        "You are a performance-marketing analyst for an Indian D2C marketplace's growth team. Produce an "
        "ACCOUNT OVERVIEW / ROOT-CAUSE story of what has ACTUALLY HAPPENED in this seller's account so far — "
        "this is descriptive/diagnostic, NOT a recommendations plan. Work ONLY from the data provided; never "
        "invent numbers. Write for a busy operations manager in simple English; the first time you use a term "
        "like CPM/CTR/CPC/ROAS/RTO/AOV, add a 3-6 word plain meaning in brackets.\n\n"
        "1) MONEY & ORDERS ('spend_orders'): from the P&L CSV + Meta CSV + daily_metrics, state total spend and "
        "WHERE it went (top campaigns/products), orders placed, orders cancelled (P&L cancelled_perc / "
        "business.cancellation_pct), delivered vs RTO (returns), GMV/revenue, COGS & contribution margin, blended "
        "ROAS (revenue per rupee) and AOV. Reconcile the P&L revenue/spend with the Meta spend. Real ₹ numbers.\n"
        "2) FUNNEL ('funnel'), TOP to BOTTOM, one entry per stage the data supports, IN ORDER: Reach (CPM/"
        "impressions) -> Clicks (CTR) -> Cost per click (CPC) -> Landing/Add-to-cart -> Purchase (conversion/CPP) "
        "-> Delivery (RTO/cancellation). For each: the numbers, a verdict (good/ok/bad), and a plain read that "
        "PINPOINTS where it breaks — e.g. 'CPM is healthy so ads ARE reaching people, but CTR is low, so people "
        "see the ads and don't click -> the creative/hook is the problem', or 'clicks are fine but purchases are "
        "low -> the website/price/offer is the drop-off'. Diagnose the specific stage the account is losing at.\n"
        "3) WHAT HAPPENED ('what_happened'): a short chronological bullet list tying money movements to events "
        "(budget changes, pauses, spikes/drops) and to the communications below.\n"
        "4) ACCOUNT STORY ('account_story'): from the calls / chats / SOS below, summarise what was going on from "
        "EACH angle — gc_side (what the Growth Consultant did/committed), kam_side (Key Account Manager), "
        "seller_side (the seller's mood, complaints, frustration), seller_requests (concrete asks, e.g. 'revive "
        "paused campaigns', 'start at Rs500/day'), and escalations (SOS/leadership escalations and WHY they were "
        "raised). If there is no signal for an angle, return an empty string for it. Quote the seller's own words "
        "briefly where it captures the issue. ALSO fold in the PAST TROUBLESHOOTS below — e.g. if a TS told the "
        "seller to fix cancellation weeks ago and it is still high, say so in seller_side/escalations.\n"
        "5) TROUBLESHOOT LENS ('troubleshoot_lens'): for each PAST troubleshoot workflow provided (most recent "
        "first), give the problem it identified, the solution/action it gave (plain words), and the OUTCOME — did "
        "later data show it worked? Judge outcome from evidence: if the SAME action recurs across successive "
        "troubleshoots (e.g. 'reduce cancellation' flagged repeatedly with the % not falling) it 'did_not_help' or "
        "was 'not_implemented'; if the metric improved after, 'helped'; if you cannot tell, 'unclear'. Cite the "
        "number/trend in 'note'. This is how we show what WE did and whether it landed.\n"
        "6) WEEKLY PROGRESS ('weekly_progress'): a CHRONOLOGICAL (oldest week first) week-by-week log. Use the "
        "dates on the communications, SOS, troubleshoots, daily_metrics and weekly P&L to place each event in its "
        "week. For each week give a short line for: communication (calls/chats), issues (red flags that surfaced), "
        "sos (escalations raised), troubleshoot (actions given that week), and progress (spend/orders/P&L movement "
        "and whether things got better or worse). Leave a field '' if nothing happened there. Only include weeks "
        "that actually have activity.\n"
        "7) KEY ISSUES ('key_issues'): the main problems, most important first, each grounded in a number or event.\n\n"
        "KEEP IT TIGHT (this must finish well within a time budget): at most 7 spend_orders, 6 funnel stages, "
        "7 what_happened bullets, 8 troubleshoot_lens items, 8 weekly_progress weeks, and 5 key_issues; short "
        "phrases (not paragraphs) per weekly field. Prioritise the most important items rather than being exhaustive."
    )
    user = f"Seller track: {track}.\n\n" + _overview_data_block(req, esc, tsh)
    client = _anthropic()
    # Output is bounded by the "KEEP IT TIGHT" caps in the prompt so all sections fit and the single
    # call stays well under Vercel's 120s limit.
    out = _tool_call(client, sysp, user, OVERVIEW_SCHEMA, max_tokens=8000, tool="submit", model=OVERVIEW_MODEL)
    # defensive caps so a runaway never reaches the UI
    for k in ("spend_orders", "funnel", "what_happened", "troubleshoot_lens", "weekly_progress", "key_issues"):
        if isinstance(out.get(k), list):
            out[k] = out[k][:16]
    out["escalations_meta"] = {"window_days": days,
                               "counts": (esc or {}).get("counts", {}),
                               "available": bool(esc)}
    out["troubleshoot_meta"] = {"count": len(tsh), "available": bool(tsh)}
    out["cached"] = False
    out["generated_at"] = _iso(time.time())
    _rca_cache_set(sid, out)   # store for team-wide reuse within RCA_CACHE_DAYS
    return out


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
