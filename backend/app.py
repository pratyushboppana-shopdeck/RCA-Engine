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

ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_BASE_URL = os.environ.get("ANTHROPIC_BASE_URL", "").rstrip("/")  # optional proxy (e.g. LiteLLM)
CLAUDE_MODEL       = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")  # or claude-opus-4-8

MAPPING_CARD  = 7753
BUSINESS_CARD = 10353
BUSINESS_BACKUP_CARD = 10352   # lighter Category Intelligence card — website/company/contact backup
CATEGORY_CARD = 10362
METRICS_CARD  = 10773   # daily spend/CPM/CTR/s_gmv time series (all HIT sellers, no params)
CARDS = (MAPPING_CARD, BUSINESS_CARD, BUSINESS_BACKUP_CARD, CATEGORY_CARD, METRICS_CARD)
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
BUSINESS_SELLER_PARAM_ID = "a45e1c84-6dc1-42fc-93da-79f43ee84255"  # card 10353
CATEGORY_SELLER_PARAM_ID = "232ba3d0-4861-4ebd-8775-5f8b9d488737"  # card 10362

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


def _mb(path, method="GET", body=None):
    """Call Metabase, re-authenticating once on 401/403."""
    tok = _auth()
    headers = {"Content-Type": "application/json", "X-Metabase-Session": tok}
    try:
        return _req(f"{MB_URL}{path}", method, body, headers)
    except urllib.error.HTTPError as ex:
        if ex.code in (401, 403):
            tok = _auth(force=True)
            headers["X-Metabase-Session"] = tok
            return _req(f"{MB_URL}{path}", method, body, headers)
        raise HTTPException(502, f"Metabase error {ex.code}: {ex.read().decode()[:200]}")


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
    return {str(r["seller_id"]): r for r in rows if isinstance(r, dict) and r.get("seller_id")}


def _get_changelog(seller_id, start, end):
    """Fetch per-seller change events from dashboard 96 (db 2, cheap) for a date range."""
    params = [
        {"id": CHANGELOG_PARAM["seller"], "value": str(seller_id)},
        {"id": CHANGELOG_PARAM["start"], "value": start},
        {"id": CHANGELOG_PARAM["end"], "value": end},
    ]
    events = []
    for dcid, cid, area in CHANGELOG_DASHCARDS:
        try:
            rows = _mb(f"/api/dashboard/{CHANGELOG_DASHBOARD}/dashcard/{dcid}/card/{cid}/query/json",
                       "POST", {"parameters": params})
        except HTTPException:
            continue
        for r in rows if isinstance(rows, list) else []:
            who = " ".join(x for x in [r.get("first_name"), r.get("last_name")] if x).strip() \
                  or r.get("changed_by") or r.get("user_email")
            events.append({
                "date": (r.get("createdat") or r.get("change_date_time") or "")[:19],
                "area": area,
                "category": _clean(r.get("category")),
                "field": _clean(r.get("field_name") or r.get("changed_fields")),
                "from": _clean(r.get("initial_value") or r.get("old_resource")),
                "to": _clean(r.get("new_value") or r.get("new_resource")),
                "by": _clean(who),
                "user_type": _clean(r.get("user_type")),
            })
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


class PlanReq(BaseModel):
    seller: dict
    mode: str = "hits"
    website: str = ""
    pnl_csv: str = ""          # P&L export (CSV text)
    meta_csv: str = ""         # Meta ad-account metrics export (CSV text)
    extra_csv: list[CsvFile] = []   # optional additional CSVs


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
    cache = {}
    for cid in CARDS:
        c = _bulk.get(cid) or _disk_read(cid) or {}
        cache[str(cid)] = {
            "sellers": len(c.get("by_id", {})),
            "age_hours": round((time.time() - c.get("ts", 0)) / 3600, 1) if c.get("ts") else None,
        }
    return {
        "ok": True,
        "metabase": bool(MB_USER and MB_PASS),
        "claude": bool(ANTHROPIC_API_KEY),
        "model": CLAUDE_MODEL,
        "claude_base_url": ANTHROPIC_BASE_URL or "api.anthropic.com (direct)",
        "cache": cache,
    }


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
    cat = lookup(CATEGORY_CARD)

    # date range (default last 30 days)
    end = (req.end_date or date.today().isoformat())[:10]
    start = (req.start_date or (date.today() - timedelta(days=30)).isoformat())[:10]

    # daily metrics (10773) from cache, sliced to range
    try:
        series = _get_card_cache(METRICS_CARD).get(sid) or []
    except HTTPException:
        series = []
    daily_metrics = [r for r in series if start <= str(r.get("date", ""))[:10] <= end] or series[-30:]

    # changelog (dashboard 96, db2 — live, cheap) for the range
    try:
        changelog = _get_changelog(sid, start, end)
    except Exception:
        changelog = []

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
        "aov_at_gl":         _clean(b.get("aov_at_gl")),
        "cogs_at_gl":        _clean(b.get("cogs_at_gl")),
        "website_source":    (BUSINESS_CARD if _clean(b.get("website")) else
                              (BUSINESS_BACKUP_CARD if _clean(b2.get("website")) else None)),
    }

    # category levels (10362): L1=primary_l2, L2=primary_l3, L3=primary_l4
    l1 = _clean(cat.get("primary_l2"))
    l2 = _clean(cat.get("primary_l3"))
    l3 = _clean(cat.get("primary_l4"))
    category = {"l1": l1, "l2": l2, "l3": l3}
    # hard-coded benchmark for the seller's Category Level 1 (primary_l2)
    benchmark = BENCHMARKS.get(l1) if l1 else None

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
        "category": category,
        "benchmark": benchmark,
        "range": {"start": start, "end": end},
        "daily_metrics": daily_metrics,
        "changelog": changelog,
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


CSV_CHAR_LIMIT = 60000  # cap each CSV sent to the model (keeps token use sane)


def _csv_block(title, text):
    text = (text or "").strip()
    if not text:
        return f"### {title}\n(none provided)\n"
    truncated = ""
    if len(text) > CSV_CHAR_LIMIT:
        text = text[:CSV_CHAR_LIMIT]
        truncated = f"\n...[truncated to {CSV_CHAR_LIMIT} chars]"
    return f"### {title}\n```csv\n{text}{truncated}\n```\n"


@app.post("/api/plan")
def plan(req: PlanReq):
    if not ANTHROPIC_API_KEY:
        raise HTTPException(500, "ANTHROPIC_API_KEY not configured")
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
    track = "HITS-managed account" if req.mode == "hits" else "seller in the 1k–5k weekly-spend band"
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
        "- Cross-reference: is the Meta ROAS above the P&L break-even ROAS? Are margins enough to scale?\n"
        "- Consider the website as the conversion surface.\n"
        "Then produce a concrete, prioritized next-plan-of-action. Every action must be specific and "
        "cite the actual numbers/dates/changes that justify it (e.g. 'COGS is 47% so break-even ROAS is "
        "~2.1x, but campaign X runs at 1.4x — cut it'). Lead with any change-caused-drop fixes. "
        "Return 3-6 actions per category. "
        "Priority 'high' = money-losing blocker or a change that caused a drop, 'med' = clear lever, "
        "'low' = nice-to-have."
    )
    user_prompt = (
        f"Seller track: {track}.\n"
        f"Website: {website or '(not available)'}\n\n"
        f"Seller account context (JSON):\n{json.dumps(req.seller, indent=2)}\n\n"
        "=== UPLOADED DATA ===\n"
        + _csv_block("P&L (profit & loss)", req.pnl_csv)
        + _csv_block("Meta ad-account metrics", req.meta_csv)
        + "".join(_csv_block(f"Additional: {f.name}", f.content) for f in (req.extra_csv or []))
        + "\nAnalyse the above deeply, then call submit_plan with the action plan across:\n"
        "1. website  — landing page / storefront / pixel / catalogue / CRO fixes\n"
        "2. campaign — Meta ad structure, creative, audience, budget, bidding actions\n"
        "3. outside  — out-of-the-box / retention / incentive / margin / creator plays"
    )
    try:
        msg = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=4000,
            system=sys_prompt,
            tools=[{
                "name": "submit_plan",
                "description": "Submit the structured 3-category action plan.",
                "input_schema": PLAN_SCHEMA,
            }],
            tool_choice={"type": "tool", "name": "submit_plan"},
            messages=[{"role": "user", "content": user_prompt}],
        )
    except Exception as e:
        raise HTTPException(502, f"Claude error: {e}")

    for block in msg.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "submit_plan":
            return block.input
    raise HTTPException(502, "Claude did not return a structured plan")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
