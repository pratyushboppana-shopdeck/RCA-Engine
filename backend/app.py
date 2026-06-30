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
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ----------------------------------------------------------------------------
# Config (env vars). See .env.example
# ----------------------------------------------------------------------------
MB_URL   = os.environ.get("METABASE_URL", "https://metabase.kaip.in").rstrip("/")
MB_USER  = os.environ.get("METABASE_USER_EMAIL", "")
MB_PASS  = os.environ.get("METABASE_PASSWORD", "")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL      = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")  # or claude-opus-4-8

MAPPING_CARD  = 7753
BUSINESS_CARD = 10353
BUSINESS_SELLER_PARAM_ID = "a45e1c84-6dc1-42fc-93da-79f43ee84255"  # from card 10353
MAPPING_TTL = 30 * 60  # refresh the 43k-row mapping cache every 30 min

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
# Mapping cache (card 7753 returns ~43k rows — load once, refresh on TTL)
# ----------------------------------------------------------------------------
_mapping = {"by_id": {}, "ts": 0}
_mapping_lock = threading.Lock()


def _load_mapping():
    rows = _mb(f"/api/card/{MAPPING_CARD}/query/json", "POST", {})
    by_id = {str(r.get("seller_id")): r for r in rows if r.get("seller_id")}
    _mapping["by_id"] = by_id
    _mapping["ts"] = time.time()
    return by_id


def _get_mapping(seller_id):
    with _mapping_lock:
        if not _mapping["by_id"] or (time.time() - _mapping["ts"] > MAPPING_TTL):
            _load_mapping()
    return _mapping["by_id"].get(str(seller_id))


def _get_business(seller_id):
    params = [{
        "type": "string/=",
        "value": str(seller_id),
        "id": BUSINESS_SELLER_PARAM_ID,
        "target": ["variable", ["template-tag", "seller_id"]],
    }]
    rows = _mb(f"/api/card/{BUSINESS_CARD}/query/json", "POST", {"parameters": params})
    return rows[0] if isinstance(rows, list) and rows else None


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


class PlanReq(BaseModel):
    seller: dict
    mode: str = "hits"


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "metabase": bool(MB_USER and MB_PASS),
        "claude": bool(ANTHROPIC_API_KEY),
        "model": CLAUDE_MODEL,
        "mapping_cached": len(_mapping["by_id"]),
    }


@app.post("/api/seller")
def seller(req: SellerReq):
    sid = req.seller_id.strip()
    if not sid:
        raise HTTPException(400, "seller_id is required")

    m = _get_mapping(sid) or {}
    b = _get_business(sid) or {}
    if not m and not b:
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
    business = {
        "company":           _clean(b.get("company")),
        "website":           _clean(b.get("website")),
        "contact":           _clean(b.get("seller_contact")),
        "offers":            _clean(b.get("offers")),
        "products_at_go_live": _clean(b.get("products_at_go_live")),
        "aov_at_gl":         _clean(b.get("aov_at_gl")),
        "cogs_at_gl":        _clean(b.get("cogs_at_gl")),
    }

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


@app.post("/api/plan")
def plan(req: PlanReq):
    if not ANTHROPIC_API_KEY:
        raise HTTPException(500, "ANTHROPIC_API_KEY not configured")
    try:
        import anthropic
    except ImportError:
        raise HTTPException(500, "anthropic package not installed (pip install anthropic)")

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    track = "HITS-managed account" if req.mode == "hits" else "seller in the 1k–5k weekly-spend band"
    sys_prompt = (
        "You are a senior Meta (Facebook/Instagram) ads strategist for an Indian D2C "
        "marketplace's growth team. Given a seller's account-team mapping and business "
        "details, produce a concrete, prioritized next-plan-of-action. Be specific and "
        "practical — reference the seller's actual numbers where useful. Return 3-5 actions "
        "per category. Use priority 'high' for blockers, 'med' for impactful, 'low' for nice-to-have."
    )
    user_prompt = (
        f"Seller track: {track}.\n"
        f"Seller data (JSON):\n{json.dumps(req.seller, indent=2)}\n\n"
        "Generate the action plan across three categories:\n"
        "1. website  — landing page / storefront / pixel / catalogue fixes\n"
        "2. campaign — Meta ad structure, creative, audience, budget actions\n"
        "3. outside  — out-of-the-box / retention / incentive / creator plays\n"
        "Call the submit_plan tool with the result."
    )
    try:
        msg = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=2000,
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
