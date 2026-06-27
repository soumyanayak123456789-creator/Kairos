"""Clutch — FastAPI app.

Through build step 6: OAuth login, Firestore persistence, the read path
(get_schedule_snapshot), the Gemini function-calling agent loop, and LANE A
execution (reversible calendar/Firestore writes) with an action log + undo.

Auth: direct Google OAuth 2.0 (design.md §5); the OAuth verifier and durable
user tokens live in Firestore. Firestore AND Vertex AI (Gemini) use Application
Default Credentials (no service-account key file, no Gemini API key) — Gemini is
called via Vertex AI so it bills the GCP project. Calendar tokens auto-refresh
and are written back.

Risk lanes (design.md §3): Lane A (create/reschedule events, task writes) is
executed automatically and logged for undo. Lane B (draft_message) is NEVER
auto-sent: when a deadline genuinely cannot be met the agent drafts a
context-aware rescue/heads-up message (Gemini) and persists it as a `proposed`
draft for the user to confirm/edit/dismiss. Confirming only MARKS it approved —
Clutch has no send capability, by design (no Gmail-send scope).

--------------------------------------------------------------------------------
Firestore data model (Native mode)
--------------------------------------------------------------------------------
Collection `oauth_states`  — ephemeral OAuth handshake state (one-time use)
  Document {state}                 # the OAuth `state` string Google echoes back
    code_verifier : str            # PKCE verifier generated during /login
    created_at    : timestamp      # server time; enables an optional TTL policy
  Deleted immediately after the token exchange.

Collection `users`         — durable per-user data, keyed by Google account id
  Document {sub}                   # Google `sub` = stable, never-reused user id
    email, name                    # identity
    token, refresh_token,          # OAuth creds (refresh_token = §5 requirement)
    token_uri, client_id,
    client_secret, scopes, expiry, updated_at
  Subcollections:
    tasks/{taskId}       subtasks: title, effort, status, parent_goal, due,
                         priority, created_at/updated_at
    action_log/{id}      undo + receipts: action, args, result, undo (reversal
                         info), undone, created_at
    drafts/{id}          Lane B rescue messages (confirm-first): recipient,
                         subject, body, goal, unmet_portion, status
                         (proposed|confirmed|dismissed), created_at/updated_at.
                         NEVER sent — Clutch has no send capability by design.
    # plan/{planId}      plan ledger (planned; not built yet)
"""

import json
import logging
import os
import pathlib
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from google import genai
from google.genai import types
from google.auth.transport.requests import AuthorizedSession
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.cloud import firestore
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from pydantic import BaseModel

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("clutch.agent")

# --- OAuth configuration -----------------------------------------------------

# Minimum scopes per design.md / CLAUDE.md: calendar.events for the action
# surface (events.list / insert / patch), plus openid/email/profile for
# identity. NEVER a Gmail-send scope.
#
# VERIFIED CORRECTION (against design.md §5): freebusy.query is NOT authorized
# by calendar.events — the official reference only accepts calendar(.readonly),
# calendar.freebusy, or calendar.events.freebusy. So we add the narrowest one,
# calendar.freebusy, purely for freebusy.query. (Adding a scope => users must
# re-consent at /login.)
SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.freebusy",
]

# Read secrets from the environment — never hardcode them.
CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")
REDIRECT_URI = os.environ.get(
    "OAUTH_REDIRECT_URI", "http://localhost:8080/oauth2callback"
)

# GCP project for Firestore. If unset, the client infers it from ADC.
PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT")

USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"
CALENDAR_BASE = "https://www.googleapis.com/calendar/v3"

# Google may return scopes in a different order / add `openid`; relax the check
# so the token exchange doesn't raise a spurious "scope has changed" warning.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

# google-auth-oauthlib refuses plain-HTTP redirects unless told otherwise. The
# localhost loopback redirect used for LOCAL testing is http, so allow it only
# in that case. (On Cloud Run the redirect is https and this stays off.)
if REDIRECT_URI.startswith("http://"):
    os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")

# Firestore collection names.
OAUTH_STATES = "oauth_states"
USERS = "users"
TASKS = "tasks"            # subcollection under users/{sub}
ACTION_LOG = "action_log"  # subcollection under users/{sub}: undo + receipts
DRAFTS = "drafts"          # subcollection under users/{sub}: Lane B rescue drafts

# Marker stamped on agent-created calendar events (extendedProperties.private)
# so they're distinguishable from the user's own events for undo + the UI.
CLUTCH_MARKER = "clutch"

# Cookie holding the opaque user id (Google `sub`) so /me knows which user's
# tokens to read from Firestore. It is NOT a credential — the tokens stay
# server-side in Firestore; this only points at the right document.
UID_COOKIE = "clutch_uid"

# --- Gemini agent config (model names change often — edit them HERE) ----------
# Gemini is called via VERTEX AI (bills against the GCP project's credit) using
# Application Default Credentials — NOT an AI Studio API key. The model IDs are
# the same on Vertex; the SDK routes them to publishers/google/models/<id>.
# Primary = fast/cheap current Flash; fallback = a DIFFERENT Gemini model used
# only on the primary's rate-limit (429) or timeout. Both kept all-Google.
PRIMARY_MODEL = "gemini-2.5-flash"
FALLBACK_MODEL = "gemini-2.5-flash-lite"
GEMINI_TIMEOUT_MS = 30_000          # per-call timeout; a timeout triggers fallback
# Vertex region. Default "global" for broadest model availability + lower error
# rates; override via env (e.g. VERTEX_LOCATION=asia-south1) once confirmed.
VERTEX_LOCATION = os.environ.get("VERTEX_LOCATION", "global")
# A granular plan costs many writes: break_down_task (~1) + ~2 calls/subtask
# (create_calendar_event + upsert_task), often one call per turn. A 12h goal
# (~7 subtasks + ~6 blocks) needs ~15+ actions, so the budget must comfortably
# exceed that or booking gets starved by decomposition. MAX_EVENTS_PER_RUN (the
# ≤8 booked-events safety cap) stays the real runaway guard; the action budget and
# step cap just need to be wide enough to let a feasible plan finish in one run.
AGENT_STEP_CAP = 24                 # max Gemini turns per run; never loop forever
AGENT_MAX_ACTIONS = 20              # hard cap on total executed writes per run
MAX_EVENTS_PER_RUN = 8             # hard cap on create_calendar_event per run; HALTS the run

# Lane A (design.md §3): reversible actions on the user's OWN surface — executed
# automatically. Lane B: outbound/irreversible — proposed only (confirm later).
# get_schedule_snapshot is read-only; notify_user just surfaces a message.
LANE_A_TOOLS = {
    "create_calendar_event",
    "reschedule_event",
    "upsert_task",
    "reprioritize",
    "break_down_task",
}
LANE_B_PROPOSE = {"draft_message"}

# --- Scheduling policy (working hours + block length) ------------------------
# Defaults in ONE place; per-user overrides live in Firestore (users/{sub}.prefs)
# and are editable via /prefs. Enforced deterministically in the executor.
WORK_TZ = "Asia/Kolkata"     # user's local timezone (IST for now)
WORK_START_HOUR = 8          # earliest a focus block may START (local)
WORK_END_HOUR = 22           # latest a focus block may END (local)
MAX_BLOCK_MINUTES = 120      # cap a single focus block at ~2h; split longer work
MIN_BLOCK_MINUTES = 30       # reject a clamped block shorter than this

app = FastAPI(title="Kairos", description="The Last-Minute Life Saver")

# The single-page frontend lives in static/ and is served at GET / (below).
STATIC_DIR = pathlib.Path(__file__).parent / "static"

# --- Firestore client (lazy; uses Application Default Credentials) ------------

_db: firestore.Client | None = None


def db() -> firestore.Client:
    """Return a process-wide Firestore client, created on first use.

    Lazy so importing this module (tests, py_compile) doesn't require ADC, and
    so the app can boot even before the first Firestore call.
    """
    global _db
    if _db is None:
        _db = firestore.Client(project=PROJECT_ID)
    return _db


# --- OAuth helpers -----------------------------------------------------------


def _require_oauth_config() -> None:
    if not CLIENT_ID or not CLIENT_SECRET:
        raise HTTPException(
            status_code=500,
            detail=(
                "Missing GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET. Set them in a "
                ".env file (see README/instructions)."
            ),
        )


def _build_flow(state: str | None = None) -> Flow:
    """Construct an OAuth Flow from env-provided client credentials."""
    client_config = {
        "web": {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [REDIRECT_URI],
        }
    }
    return Flow.from_client_config(
        client_config, scopes=SCOPES, state=state, redirect_uri=REDIRECT_URI
    )


# --- Firestore-backed stores -------------------------------------------------


def save_verifier(state: str, code_verifier: str) -> None:
    """Persist the PKCE verifier for an in-flight handshake, keyed by state."""
    db().collection(OAUTH_STATES).document(state).set(
        {"code_verifier": code_verifier, "created_at": firestore.SERVER_TIMESTAMP}
    )


def pop_verifier(state: str) -> str | None:
    """Read-and-delete the verifier for `state` (one-time use)."""
    ref = db().collection(OAUTH_STATES).document(state)
    snap = ref.get()
    if not snap.exists:
        return None
    ref.delete()
    return snap.to_dict().get("code_verifier")


def save_user_tokens(sub: str, email: str, name: str, creds: Credentials) -> None:
    """Upsert a user's tokens + identity into Firestore."""
    payload = {
        "email": email,
        "name": name,
        "token": creds.token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": creds.scopes,
        "updated_at": firestore.SERVER_TIMESTAMP,
    }
    # Google omits the refresh_token on re-consent sometimes; only overwrite when
    # we actually received one, so we never clobber a stored refresh token.
    if creds.refresh_token:
        payload["refresh_token"] = creds.refresh_token
    db().collection(USERS).document(sub).set(payload, merge=True)


def load_user(sub: str) -> dict | None:
    snap = db().collection(USERS).document(sub).get()
    return snap.to_dict() if snap.exists else None


def load_tasks(sub: str) -> list[dict]:
    """Read the user's subtasks from Firestore (empty until later steps)."""
    docs = db().collection(USERS).document(sub).collection("tasks").stream()
    return [{"id": d.id, **d.to_dict()} for d in docs]


# --- Calendar read path (get_schedule_snapshot) ------------------------------


def _credentials_from_doc(data: dict) -> Credentials:
    """Rebuild OAuth Credentials from a stored user doc."""
    expiry = data.get("expiry")
    # Firestore returns tz-aware datetimes; google-auth wants naive UTC.
    if expiry is not None and expiry.tzinfo is not None:
        expiry = expiry.astimezone(timezone.utc).replace(tzinfo=None)
    return Credentials(
        token=data.get("token"),
        refresh_token=data.get("refresh_token"),
        token_uri=data.get("token_uri"),
        client_id=data.get("client_id"),
        client_secret=data.get("client_secret"),
        scopes=data.get("scopes"),
        expiry=expiry,
    )


def _ensure_valid(sub: str, creds: Credentials) -> Credentials:
    """Refresh the access token if expired, persisting the new one to Firestore.

    Only refreshes when actually needed, so we don't hammer the token endpoint.
    """
    if creds.valid:
        return creds
    if not creds.refresh_token:
        raise HTTPException(
            status_code=401, detail="Session expired and no refresh token. Re-login at /login."
        )
    creds.refresh(GoogleAuthRequest())
    # Store the new access token + expiry (tz-aware UTC for Firestore).
    expiry = creds.expiry
    if expiry is not None and expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    db().collection(USERS).document(sub).set(
        {"token": creds.token, "expiry": expiry, "updated_at": firestore.SERVER_TIMESTAMP},
        merge=True,
    )
    return creds


def _list_events(session: AuthorizedSession, time_min: str, time_max: str) -> list[dict]:
    resp = session.get(
        f"{CALENDAR_BASE}/calendars/primary/events",
        params={
            "timeMin": time_min,
            "timeMax": time_max,
            "singleEvents": "true",   # expand recurring into instances
            "orderBy": "startTime",
            "maxResults": 50,
        },
    )
    if resp.status_code != 200:
        raise HTTPException(
            status_code=502, detail=f"events.list failed ({resp.status_code}): {resp.text}"
        )
    out = []
    for e in resp.json().get("items", []):
        start, end = e.get("start", {}), e.get("end", {})
        out.append(
            {
                "id": e.get("id"),
                "summary": e.get("summary", "(no title)"),
                # dateTime for timed events, date for all-day events.
                "start": start.get("dateTime") or start.get("date"),
                "end": end.get("dateTime") or end.get("date"),
                "all_day": "date" in start,
                "status": e.get("status"),
            }
        )
    return out


def _freebusy(session: AuthorizedSession, time_min: str, time_max: str) -> list[dict]:
    resp = session.post(
        f"{CALENDAR_BASE}/freeBusy",
        json={"timeMin": time_min, "timeMax": time_max, "items": [{"id": "primary"}]},
    )
    if resp.status_code != 200:
        raise HTTPException(
            status_code=502, detail=f"freebusy.query failed ({resp.status_code}): {resp.text}"
        )
    primary = resp.json().get("calendars", {}).get("primary", {})
    return primary.get("busy", [])  # list of {start, end}


# --- Routes ------------------------------------------------------------------


@app.get("/")
def index():
    """Serve the Kairos single-page app (the user-facing frontend)."""
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
def health():
    """Liveness/health check. Was GET / before the frontend was added; the
    internal service name stays 'clutch' for deploy/continuity (not user-facing).
    """
    return {"status": "ok", "service": "clutch"}


@app.get("/login")
def login():
    """Redirect the user to Google's consent screen."""
    _require_oauth_config()
    flow = _build_flow()
    auth_url, state = flow.authorization_url(
        access_type="offline",       # ask for a refresh token...
        prompt="consent",            # ...and force its issuance every time
        include_granted_scopes="true",
    )
    # Persist the auto-generated PKCE code_verifier in Firestore, keyed by the
    # state Google will echo back — works across Cloud Run instances.
    save_verifier(state, flow.code_verifier)
    return RedirectResponse(auth_url)


def _callback_url(request: Request) -> str:
    """Reconstruct the full callback URL for the OAuth token exchange.

    Cloud Run terminates TLS at its front proxy and forwards to the container
    over plain HTTP, so ``request.url.scheme`` is "http" even though the user
    actually arrived over https. oauthlib then rejects the exchange with
    InsecureTransportError. The proxy tells us the real client-facing scheme via
    the ``X-Forwarded-Proto`` header (it will be "https" on Cloud Run), so we
    trust that to restore the correct scheme. For genuine local http (no proxy,
    no header) the URL is returned unchanged. We do NOT blanket-disable
    oauthlib's https check — the request really is https; the app just needs to
    see it.
    """
    url = request.url
    forwarded_proto = request.headers.get("x-forwarded-proto")
    if forwarded_proto:
        # May be a comma-separated chain (e.g. "https,http"); the first hop is
        # the original client-facing scheme.
        proto = forwarded_proto.split(",")[0].strip()
        if proto and proto != url.scheme:
            url = url.replace(scheme=proto)
    return str(url)


@app.get("/oauth2callback")
def oauth2callback(request: Request):
    """Handle Google's redirect and exchange the code for tokens."""
    _require_oauth_config()
    if request.query_params.get("error"):
        raise HTTPException(
            status_code=400, detail=f"OAuth error: {request.query_params['error']}"
        )

    state = request.query_params.get("state")
    # One-time-use verifier from Firestore (deleted on read).
    code_verifier = pop_verifier(state) if state else None
    if not state or not code_verifier:
        # Stale/replayed callback — e.g. the user pressed browser Back to Google's
        # consent page (still in history from the login round-trip) and clicked
        # Continue, but the one-time state was already consumed. Don't surface raw
        # JSON: send an already-signed-in user back into the app, otherwise restart
        # the login cleanly.
        existing = request.cookies.get(UID_COOKIE)
        if existing and load_user(existing):
            return RedirectResponse("/")
        return RedirectResponse("/login")

    flow = _build_flow(state=state)
    flow.code_verifier = code_verifier
    # Exchange the authorization code (carried on the callback URL) for tokens.
    flow.fetch_token(authorization_response=_callback_url(request))
    creds = flow.credentials

    # Identify the user (sub/email/name) using the fresh access token.
    info = AuthorizedSession(creds).get(USERINFO_URL).json()
    sub = info.get("sub")
    if not sub:
        raise HTTPException(status_code=502, detail="Could not read Google user id.")

    save_user_tokens(sub, info.get("email"), info.get("name"), creds)

    resp = RedirectResponse("/")  # land back on the Kairos SPA, signed in
    # Point the browser at this user's Firestore doc. Opaque id, not a secret.
    resp.set_cookie(
        UID_COOKIE,
        sub,
        httponly=True,
        samesite="lax",
        secure=REDIRECT_URI.startswith("https"),
    )
    return resp


@app.get("/me")
def me(request: Request):
    """Confirm login status from Firestore (proves persistence across restarts)."""
    sub = request.cookies.get(UID_COOKIE)
    data = load_user(sub) if sub else None
    if not data:
        return JSONResponse(
            {"logged_in": False, "hint": "Visit /login to connect your Google account."}
        )

    return {
        "logged_in": True,
        "email": data.get("email"),
        "name": data.get("name"),
        "has_refresh_token": bool(data.get("refresh_token")),
        "scopes": data.get("scopes"),
        "source": "firestore",
    }


def _jsonify(obj):
    """Recursively convert Firestore datetimes to ISO strings (JSON / proto safe)."""
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: _jsonify(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_jsonify(v) for v in obj]
    return obj


def build_snapshot(
    sub: str, data: dict, hours: int, session: AuthorizedSession | None = None
) -> dict:
    """get_schedule_snapshot: perceive calendar (events + free/busy) + tasks.

    Read-only. Returns calendar/task data only — never token values. An existing
    refreshed `session` may be passed in to avoid re-refreshing within an agent run.
    """
    if session is None:
        creds = _ensure_valid(sub, _credentials_from_doc(data))
        session = AuthorizedSession(creds)

    now = datetime.now(timezone.utc)
    end = now + timedelta(hours=hours)
    time_min, time_max = now.isoformat(), end.isoformat()

    events = _list_events(session, time_min, time_max)
    busy = _freebusy(session, time_min, time_max)
    tasks = _jsonify(load_tasks(sub))  # tasks may carry Firestore datetimes

    return {
        "window": {"start": time_min, "end": time_max, "hours": hours, "timezone": "UTC"},
        "counts": {"events": len(events), "busy_blocks": len(busy), "tasks": len(tasks)},
        "events": events,
        "busy": busy,
        "tasks": tasks,
    }


@app.get("/snapshot")
def snapshot(request: Request, hours: int = 48):
    if hours < 1 or hours > 24 * 30:
        raise HTTPException(status_code=400, detail="hours must be between 1 and 720.")
    sub = request.cookies.get(UID_COOKIE)
    data = load_user(sub) if sub else None
    if not data:
        raise HTTPException(status_code=401, detail="Not logged in. Visit /login first.")
    return build_snapshot(sub, data, hours)


@app.get("/ui/timeline")
def ui_timeline(
    request: Request,
    hours: int = 72,
    start: str | None = None,
    end: str | None = None,
):
    """Serving route for the frontend timeline.

    Returns calendar events, each annotated with `kairos: bool` (True for
    agent-created focus blocks, detected via the existing clutch-marker filter).
    UI-only and read-only — it does NOT feed Gemini, so it never changes agent
    behavior (unlike build_snapshot, which is left untouched).

    Range selection: if `start` and/or `end` (local `YYYY-MM-DD` dates) are
    given, the window runs from `start` local-midnight to `end` end-of-day
    (inclusive). Otherwise it falls back to local-midnight-today → now + `hours`,
    so the full current day shows.
    """
    if hours < 1 or hours > 24 * 30:
        raise HTTPException(status_code=400, detail="hours must be between 1 and 720.")
    sub = request.cookies.get(UID_COOKIE)
    data = load_user(sub) if sub else None
    if not data:
        raise HTTPException(status_code=401, detail="Not logged in. Visit /login first.")

    creds = _ensure_valid(sub, _credentials_from_doc(data))
    session = AuthorizedSession(creds)

    tz = ZoneInfo(get_prefs(data)["work_tz"])
    now_local = datetime.now(tz)

    if start or end:
        try:
            win_start = (
                datetime.fromisoformat(start).replace(tzinfo=tz)
                if start
                else now_local.replace(hour=0, minute=0, second=0, microsecond=0)
            )
            # `end` is inclusive: extend to the very end of that local day.
            win_end = (
                datetime.fromisoformat(end).replace(
                    hour=23, minute=59, second=59, microsecond=0, tzinfo=tz
                )
                if end
                else win_start + timedelta(hours=hours)
            )
        except ValueError:
            raise HTTPException(
                status_code=400, detail="start/end must be YYYY-MM-DD dates."
            )
        if win_end <= win_start:
            raise HTTPException(status_code=400, detail="end must be after start.")
        time_min = win_start.isoformat()
        time_max = win_end.isoformat()
    else:
        win_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        time_min = win_start.isoformat()
        time_max = (now_local + timedelta(hours=hours)).isoformat()

    events = _list_events(session, time_min, time_max)
    # Map each clutch block -> its parent goal (from extendedProperties) so the UI
    # can label/group blocks by goal. UI-only; the agent's snapshot is unaffected.
    clutch = _list_clutch_events(session, time_min, time_max)
    kairos_ids = {e.get("id") for e in clutch}
    goal_by_id = {
        e.get("id"): (e.get("extendedProperties", {}).get("private", {}) or {}).get("clutch_goal")
        for e in clutch
    }
    for ev in events:
        ev["kairos"] = ev.get("id") in kairos_ids
        if ev["kairos"] and goal_by_id.get(ev.get("id")):
            ev["goal"] = goal_by_id[ev["id"]]

    return {
        "now": now_local.isoformat(),
        "timezone": str(tz),
        "window": {"start": time_min, "end": time_max},
        "events": events,
    }


# --- Gemini agent loop (read / propose only; no writes this build) ------------

# Full tool schema from design.md §5. Declared to Gemini so it can plan with the
# real action surface, but only EXECUTABLE_TOOLS are actually run this build.
FUNCTION_DECLARATIONS = [
    types.FunctionDeclaration(
        name="get_schedule_snapshot",
        description="Perceive the user's world: calendar events + free/busy and "
        "the Firestore subtask list for a time window. Call this FIRST.",
        parameters_json_schema={
            "type": "object",
            "properties": {
                "window_start": {"type": "string", "description": "ISO 8601 start of window."},
                "window_end": {"type": "string", "description": "ISO 8601 end of window."},
            },
        },
    ),
    types.FunctionDeclaration(
        name="create_calendar_event",
        description="Book a focus block on the user's calendar (Lane A write).",
        parameters_json_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "start": {"type": "string", "description": "ISO 8601 start."},
                "end": {"type": "string", "description": "ISO 8601 end."},
                "description": {"type": "string"},
            },
            "required": ["title", "start", "end"],
        },
    ),
    types.FunctionDeclaration(
        name="reschedule_event",
        description="Move an existing calendar event to a new time (Lane A write).",
        parameters_json_schema={
            "type": "object",
            "properties": {
                "event_id": {"type": "string"},
                "new_start": {"type": "string", "description": "ISO 8601 start."},
                "new_end": {"type": "string", "description": "ISO 8601 end."},
                "reason": {"type": "string"},
            },
            "required": ["event_id", "new_start", "new_end"],
        },
    ),
    types.FunctionDeclaration(
        name="break_down_task",
        description="Decompose a goal into time-estimated subtasks (writes to Firestore).",
        parameters_json_schema={
            "type": "object",
            "properties": {
                "goal": {"type": "string"},
                "deadline": {"type": "string", "description": "ISO 8601 deadline."},
            },
            "required": ["goal"],
        },
    ),
    types.FunctionDeclaration(
        name="upsert_task",
        description="Create/update/complete a subtask in Firestore.",
        parameters_json_schema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Omit to create a new task."},
                "title": {"type": "string"},
                "due": {"type": "string", "description": "ISO 8601 due time."},
                "effort": {"type": "integer", "description": "Estimated minutes."},
                "status": {"type": "string", "enum": ["todo", "doing", "done"]},
            },
            "required": ["title"],
        },
    ),
    types.FunctionDeclaration(
        name="reprioritize",
        description="Commit a new ordering / 'do now' pick of subtasks in Firestore.",
        parameters_json_schema={
            "type": "object",
            "properties": {
                "ranked_task_ids": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["ranked_task_ids"],
        },
    ),
    types.FunctionDeclaration(
        name="draft_message",
        description=(
            "Lane B (PROPOSE-ONLY, never sent): call ONLY when a deadline genuinely "
            "cannot be met — the remaining work does not fit before the deadline even "
            "after rescheduling. Drafts a short heads-up / extension-request message "
            "for the user to review and confirm. Give the goal at risk and the "
            "SPECIFIC unmet portion. You may supply subject/body, or leave them empty "
            "to have one composed for you."
        ),
        parameters_json_schema={
            "type": "object",
            "properties": {
                "recipient": {
                    "type": "string",
                    "description": "Who the message is to (e.g. 'manager', 'professor'). A placeholder is fine.",
                },
                "goal": {"type": "string", "description": "The goal/deliverable at risk."},
                "unmet_portion": {
                    "type": "string",
                    "description": "The SPECIFIC part that will not be done in time (e.g. 'the final formatting pass won't be done by Friday 6pm').",
                },
                "new_eta": {
                    "type": "string",
                    "description": "A realistic completion time, or the extension you would ask for.",
                },
                "subject": {"type": "string", "description": "Optional subject line; omit to auto-compose."},
                "body": {"type": "string", "description": "Optional full message body; omit to auto-compose from the fields above."},
            },
            "required": ["unmet_portion"],
        },
    ),
    types.FunctionDeclaration(
        name="notify_user",
        description="Surface a receipt/nudge to the user.",
        parameters_json_schema={
            "type": "object",
            "properties": {
                "message": {"type": "string"},
                "urgency": {"type": "string", "enum": ["low", "normal", "high"]},
            },
            "required": ["message"],
        },
    ),
]

SYSTEM_PROMPT = (
    "You are Clutch, a proactive scheduling agent. Your job: ensure the user "
    "finishes their work before its deadline by taking concrete action.\n"
    "Process: (1) Call get_schedule_snapshot first to perceive the calendar and "
    "tasks. (2) Reason about time remaining vs. work remaining. (3) Act via "
    "function calls: break the goal into subtasks, book focus blocks in the "
    "user's actual FREE time before the deadline, reschedule conflicts, and "
    "re-prioritize. These calendar/task actions are executed for real and are "
    "reversible, so act decisively but do not double-book. Respect the scheduling "
    "policy in the prompt: keep focus blocks inside working hours and within the "
    "max block length, splitting longer work across multiple days.\n"
    "Decompose FIRST, then schedule. Always call break_down_task to split the goal "
    "into several GRANULAR, DISTINCT, differently-named subtasks (for a research "
    "report, that is: background research, source gathering, outline, draft "
    "introduction, draft each body section, draft conclusion, revise/edit, format & "
    "citations — NOT one giant 'draft the main body'). Book exactly ONE focus block "
    "per distinct subtask, and title that block after the subtask it implements.\n"
    "Schedule by EFFORT and COVER THE WHOLE WORKLOAD: book blocks until the ENTIRE "
    "estimated effort (the SUM of the subtask efforts) is scheduled in the user's "
    "free time before the deadline. When there is enough free time before the "
    "deadline, you MUST schedule all of it — do NOT stop after one or two blocks "
    "while free time remains. A 12-hour goal spread over several free days should "
    "become roughly 6 or more distinct blocks across those days, not a single "
    "block. PREFER more distinct subtasks over splitting one big subtask: if a "
    "piece of work is large, break it into several differently-named subtasks "
    "(e.g. 'Draft Section 1', 'Draft Section 2') instead of booking the same title "
    "as '(Part 1)', '(Part 2)', '(Part 3)'. Only split a SINGLE indivisible subtask "
    "into multiple blocks when its own effort truly exceeds the max block length, "
    "and spread those across different days. NEVER repeat the same subtask title or "
    "create near-duplicate blocks to pad time. You may issue several function calls "
    "in one step.\n"
    "FEASIBILITY CHECK: only fall back to a rescue message when the work GENUINELY "
    "cannot fit — i.e. you have already scheduled into all the usable free time "
    "before the deadline and estimated effort STILL remains unscheduled. In that "
    "case do two things: (a) call notify_user with a high-urgency warning, and "
    "(b) call draft_message to draft a short rescue/heads-up message (an extension "
    "request or a 'running behind' note) — pass the goal and the SPECIFIC unmet "
    "portion (e.g. 'the final formatting pass won't be done by Friday 6pm'). This "
    "draft is only PROPOSED for the user's review; it is never sent. If the full "
    "effort DOES fit before the deadline, schedule ALL of it and do NOT draft a "
    "message — choosing to book less is not a reason to draft one.\n"
    "When done, stop calling functions and reply with a one-paragraph receipt."
)


def rank_tasks(tasks: list[dict]) -> list[dict]:
    """Deterministic pre-ranker: soonest deadline + bigger effort => more urgent.

    Auditable ordering fed to Gemini (design.md §3). Robust to missing fields.
    """
    now = datetime.now(timezone.utc)

    def due_dt(t: dict) -> datetime | None:
        due = t.get("due")
        if due is None:
            return None
        if isinstance(due, datetime):
            return due if due.tzinfo else due.replace(tzinfo=timezone.utc)
        try:
            return datetime.fromisoformat(str(due).replace("Z", "+00:00"))
        except ValueError:
            return None

    def score(t: dict) -> float:
        d = due_dt(t)
        effort_min = float(t.get("effort") or 30)
        # Lower score = more urgent. Tasks with no due date sink to the bottom.
        slack_seconds = 1e12 if d is None else (d - now).total_seconds()
        return slack_seconds - effort_min * 60

    ranked = sorted(tasks, key=score)
    return [
        {
            "rank": i + 1,
            "id": t.get("id"),
            "title": t.get("title"),
            "due": t.get("due").isoformat() if isinstance(t.get("due"), datetime) else t.get("due"),
            "effort_min": t.get("effort"),
            "status": t.get("status"),
        }
        for i, t in enumerate(ranked)
    ]


def _gemini_client() -> genai.Client:
    """Vertex AI Gemini client over ADC (no API key). Bills to the GCP project."""
    if not PROJECT_ID:
        raise HTTPException(
            status_code=500,
            detail="Set GOOGLE_CLOUD_PROJECT (and run `gcloud auth application-default "
            "login`) to use Vertex AI.",
        )
    return genai.Client(
        vertexai=True,
        project=PROJECT_ID,
        location=VERTEX_LOCATION,
        http_options=types.HttpOptions(timeout=GEMINI_TIMEOUT_MS),
    )


def _is_rate_limit(e: Exception) -> bool:
    s = str(e).lower()
    return getattr(e, "code", None) == 429 or "429" in s or "resource_exhausted" in s or "quota" in s


def _is_timeout(e: Exception) -> bool:
    s = str(e).lower()
    return "timeout" in s or "timed out" in s or "deadline" in s


def _is_transient(e: Exception) -> bool:
    s = str(e).lower()
    return (
        _is_rate_limit(e)
        or _is_timeout(e)
        or getattr(e, "code", None) in (500, 503)
        or "unavailable" in s
        or "internal" in s
    )


def generate_with_fallback(client, contents, config) -> tuple[object, str, list[str]]:
    """Call Gemini: primary, retry once on transient, then fall back to a second
    Gemini model only on rate-limit (429) or timeout. Returns (response, model,
    attempt_log). Logs which model served the request.
    """
    log: list[str] = []
    # Primary: up to 2 attempts (one retry on transient errors/timeouts).
    for attempt in range(2):
        try:
            resp = client.models.generate_content(
                model=PRIMARY_MODEL, contents=contents, config=config
            )
            log.append(f"{PRIMARY_MODEL}: ok (attempt {attempt + 1})")
            logger.info("Gemini served by %s (attempt %d)", PRIMARY_MODEL, attempt + 1)
            return resp, PRIMARY_MODEL, log
        except Exception as e:  # noqa: BLE001 - we classify below
            log.append(f"{PRIMARY_MODEL}: failed (attempt {attempt + 1}): {type(e).__name__}: {e}")
            logger.warning("Primary %s failed (attempt %d): %s", PRIMARY_MODEL, attempt + 1, e)
            if attempt == 0:
                if _is_transient(e):
                    continue  # retry primary once
                raise HTTPException(status_code=502, detail={"error": "Gemini primary error", "log": log})
            # Second failure: fall over only on rate-limit or timeout.
            if not (_is_rate_limit(e) or _is_timeout(e)):
                raise HTTPException(status_code=502, detail={"error": "Gemini primary error", "log": log})

    # Fallback model (different Gemini model).
    try:
        resp = client.models.generate_content(
            model=FALLBACK_MODEL, contents=contents, config=config
        )
        log.append(f"{FALLBACK_MODEL}: ok (fallback)")
        logger.info("Gemini fell back to %s", FALLBACK_MODEL)
        return resp, FALLBACK_MODEL, log
    except Exception as e:  # noqa: BLE001
        log.append(f"{FALLBACK_MODEL}: failed (fallback): {type(e).__name__}: {e}")
        logger.error("Fallback %s failed: %s", FALLBACK_MODEL, e)
        raise HTTPException(status_code=502, detail={"error": "Both Gemini models failed", "log": log})


# --- Lane A executors + action log -------------------------------------------


def _event_time(dt_str: str) -> dict:
    """Build a Calendar start/end object; add timeZone only if no offset given."""
    try:
        d = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        if d.tzinfo is None:
            return {"dateTime": dt_str, "timeZone": "UTC"}
    except (ValueError, AttributeError):
        return {"dateTime": dt_str, "timeZone": "UTC"}
    return {"dateTime": dt_str}


# Filler/stop words and common leading action verbs dropped when deriving the
# short goal tag — the noun phrase that follows is what makes a useful label.
_GOAL_STOPWORDS = {
    # articles / prepositions / conjunctions / determiners
    "a", "an", "the", "to", "for", "of", "and", "or", "my", "your", "our", "this",
    "that", "in", "on", "at", "with", "by", "before", "due", "until", "into", "from",
    "which", "while", "as", "so", "but", "about", "around", "roughly", "approximately",
    # pronouns / modals / intent fillers ("i need to finish a …", "i want to …")
    "i", "me", "we", "us", "you", "mine", "ours", "yours", "it",
    "need", "needs", "needed", "want", "wants", "wanted", "have", "has", "had",
    "will", "would", "shall", "should", "can", "could", "must", "may", "might",
    "like", "going", "gonna", "gotta", "just", "really", "very", "am", "is", "are",
    "take", "takes", "taking", "took", "spend", "spent",
    # time units (don't let "12 hours" leak into the tag)
    "hour", "hours", "hr", "hrs", "minute", "minutes", "min", "mins", "second",
    "seconds", "day", "days", "week", "weeks", "month", "months", "time",
    # common leading action verbs — the noun phrase that follows is the useful label
    "write", "writing", "finish", "finishing", "complete", "completing", "do",
    "doing", "make", "making", "prepare", "preparing", "study", "studying", "build",
    "building", "create", "creating", "submit", "submitting", "send", "sending",
    "plan", "planning", "get", "getting", "review", "reviewing", "read", "reading",
    "draft", "drafting", "pass", "passing", "work", "working", "start", "starting",
}


def _short_goal(goal: str | None) -> str:
    """Condense a goal into a ~2-word tag from its first meaningful keywords.

    DISPLAY/LABELING ONLY. Skips filler/stop words, leading action verbs, and
    deadline clauses, then Title-Cases the first ~2 keywords.
    "write a research report by Friday" -> "Research Report".
    """
    g = (goal or "").strip()
    if not g:
        return ""
    # Cut deadline tails ("by Friday", "before…") and clause boundaries first.
    g = re.split(r"\s+(?:by|before|due|until|on)\s+", g, maxsplit=1, flags=re.IGNORECASE)[0]
    g = re.split(r"[—–\-:,.;]", g, maxsplit=1)[0].strip()
    raw = re.findall(r"[A-Za-z0-9']+", g)
    # Skip stop words and pure-number tokens ("12") so only real keywords remain.
    keywords = [w for w in raw if w.lower() not in _GOAL_STOPWORDS and not w.isdigit()]
    chosen = (keywords or raw)[:2]
    if not chosen:
        return ""
    return " ".join(w[:1].upper() + w[1:] for w in chosen)


def _format_block_title(raw_title: str, goal: str | None) -> str:
    """Calendar event title: '<subtask> (<2-word short tag>)'. No 'Focus:' prefix.

    DISPLAY/LABELING ONLY — does not affect scheduling, subtask breakdown, or the
    runaway cap. We normalize whatever title the model proposed (stripping any
    "Focus"/"Focus block" prefix, a bracketed goal, and any goal it already
    appended), then attach the SHORT 2-word goal tag from _short_goal(). The FULL
    goal sentence is never written to the title — only the 2-word tag. With no
    goal, the title is just the subtask.
    e.g. subtask "Draft Main Body Content and Analysis" + goal "i need to finish a
    research report which will take me 12 hours" -> "Draft Main Body Content and
    Analysis (Research Report)".
    """
    title = (raw_title or "Focus block").strip()
    g = _short_goal(goal)

    # Drop a leading "[Goal] " bracket the model may have added.
    title = re.sub(r"^\[[^\]]*\]\s*", "", title).strip()
    # Drop a leading "Focus" / "Focus block" / "Focus:" prefix to isolate the subtask.
    subtask = re.sub(r"(?i)^focus(?:\s+block)?\b\s*[:\-–—]?\s*", "", title).strip()
    if not subtask:
        subtask = "Work block"

    # If the model already appended the goal (short tag OR full sentence), strip it
    # so we don't double-label.
    full = (goal or "").strip()
    for suffix in (f" — {full}", f" - {full}", f" – {full}", f" ({full})", f" [{full}]",
                   f" — {g}", f" - {g}", f" – {g}", f" ({g})", f" [{g}]"):
        if suffix.strip(" —–-([") and subtask.lower().endswith(suffix.lower()):
            subtask = subtask[: -len(suffix)].strip()
            break

    if not g:
        return subtask
    return f"{subtask} ({g})"


def _cal_create(session, title, start, end, description=None, goal=None) -> dict:
    note = "🤖 Created by Clutch (agent focus block)."
    private = {CLUTCH_MARKER: "1", "clutch_action": "focus_block"}
    if goal:
        # Stored for the UI (timeline grouping/labeling). Not read by the agent.
        private["clutch_goal"] = goal[:300]
    body = {
        "summary": title,
        "start": _event_time(start),
        "end": _event_time(end),
        "description": f"{description}\n\n{note}" if description else note,
        # Tag so undo/UI can tell agent events from the user's own.
        "extendedProperties": {"private": private},
    }
    resp = session.post(f"{CALENDAR_BASE}/calendars/primary/events", json=body)
    if resp.status_code not in (200, 201):
        raise HTTPException(502, f"events.insert failed ({resp.status_code}): {resp.text}")
    return resp.json()


def _cal_get(session, event_id) -> dict:
    resp = session.get(f"{CALENDAR_BASE}/calendars/primary/events/{event_id}")
    if resp.status_code != 200:
        raise HTTPException(502, f"events.get failed ({resp.status_code}): {resp.text}")
    return resp.json()


def _cal_patch(session, event_id, patch) -> dict:
    resp = session.patch(f"{CALENDAR_BASE}/calendars/primary/events/{event_id}", json=patch)
    if resp.status_code != 200:
        raise HTTPException(502, f"events.patch failed ({resp.status_code}): {resp.text}")
    return resp.json()


def _cal_delete(session, event_id) -> None:
    resp = session.delete(f"{CALENDAR_BASE}/calendars/primary/events/{event_id}")
    if resp.status_code not in (200, 204, 410):  # 410 = already gone (idempotent)
        raise HTTPException(502, f"events.delete failed ({resp.status_code}): {resp.text}")


def _list_clutch_events(session, time_min, time_max) -> list[dict]:
    """List ONLY events carrying the clutch marker (privateExtendedProperty filter).

    The server-side filter guarantees the user's own (unmarked) events are never
    returned — the safety boundary for bulk delete.
    """
    resp = session.get(
        f"{CALENDAR_BASE}/calendars/primary/events",
        params={
            "timeMin": time_min,
            "timeMax": time_max,
            "singleEvents": "true",
            "maxResults": 250,
            "privateExtendedProperty": f"{CLUTCH_MARKER}=1",
        },
    )
    if resp.status_code != 200:
        raise HTTPException(502, f"events.list (clutch filter) failed ({resp.status_code}): {resp.text}")
    return resp.json().get("items", [])


def log_action(sub, action, args, result, undo) -> str:
    """Append an action_log record with enough info to reverse it later."""
    ref = db().collection(USERS).document(sub).collection(ACTION_LOG).document()
    ref.set(
        {
            "action": action,
            "args": args,
            "result": result,
            "undo": undo,
            "undone": False,
            "created_at": firestore.SERVER_TIMESTAMP,
        }
    )
    return ref.id


def _safe_parse_local(dt_str, tz: ZoneInfo) -> datetime | None:
    """Parse an ISO datetime into tz; return None if absent/unparseable."""
    if not dt_str:
        return None
    try:
        return _parse_local(str(dt_str), tz)
    except (ValueError, TypeError):
        return None


def _normalize_due(due_str, now: datetime, deadline_dt: datetime | None,
                   idx: int, total: int, tz: ZoneInfo) -> datetime:
    """Force a subtask due date into (now, deadline]; recompute if invalid.

    Never returns a past date. If the model's date is missing/past/after the
    deadline, distribute due dates evenly across the remaining window so the
    work is paced rather than invented.
    """
    d = _safe_parse_local(due_str, tz)
    if d is None or d < now or (deadline_dt and d > deadline_dt):
        if deadline_dt and deadline_dt > now:
            frac = (idx + 1) / (total + 1)  # +1 leaves a buffer before the deadline
            d = now + (deadline_dt - now) * frac
        else:
            d = now + timedelta(days=idx + 1)  # no usable deadline: space out daily
    return d


def decompose_goal(client, goal, now: datetime, deadline_dt: datetime | None) -> list[dict]:
    """One bounded Gemini call: goal -> list of subtask dicts (title/effort/due).

    The current date and deadline are passed in explicitly so the model dates
    relative to NOW; dues are still validated/clamped by the caller afterwards.
    """
    deadline_str = deadline_dt.isoformat() if deadline_dt else "unspecified"
    prompt = (
        f"The current date and time is {now.isoformat()}.\n"
        "Break this goal into concrete, time-estimated subtasks (typically 4-8; use "
        "fewer only for a genuinely small goal). Make the subtasks VARIED and "
        "granular: each one should be a DISTINCT phase or deliverable that moves the "
        "goal forward, covering it end-to-end. For example, a research report would "
        "decompose into steps like background research, outline, draft introduction, "
        "draft each major body section, draft conclusion, revise/edit, and "
        "format/citations — NOT one giant 'draft everything' step. Do NOT collapse "
        "the bulk of the work into a single dominant subtask that would just be "
        "repeated; split large work into separate, differently-named steps instead. "
        "Keep each subtask reasonably sized (roughly 30-120 minutes of effort) so it "
        "schedules cleanly while letting longer steps be chunked when needed. Return "
        "a JSON array of objects with keys: title (string), effort (integer "
        "minutes), due (ISO 8601 datetime string).\n"
        f"Every `due` MUST be strictly after {now.isoformat()} and on or before "
        f"the deadline {deadline_str}. Do NOT use any date from your training data "
        "or any date in the past — compute dates relative to the current date above.\n"
        f"Goal: {goal}\nDeadline: {deadline_str}"
    )
    cfg = types.GenerateContentConfig(response_mime_type="application/json", temperature=0.2)
    resp, _model, _log = generate_with_fallback(
        client, [types.Content(role="user", parts=[types.Part.from_text(text=prompt)])], cfg
    )
    try:
        items = json.loads(resp.text)
        return items[:8] if isinstance(items, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


# --- Lane B: confirm-first rescue drafts (never sent) -------------------------


def compose_rescue_draft(client, goal, unmet_portion, deadline, new_eta, recipient) -> dict:
    """One bounded Gemini call: context -> {subject, body} for a rescue message.

    Used when the agent (or executor) needs to materialize a draft from structured
    facts. Lane B output — proposed only, never sent. Returns a {subject, body}
    dict; falls back to a plain templated message if generation/parsing fails.
    """
    prompt = (
        "Write a SHORT, polite heads-up message because a deadline is at risk. It is "
        "either an extension request or a 'running behind' note. Be concise (3-5 "
        "sentences), professional, specific, and propose a concrete next step or new "
        "ETA. Do NOT invent facts beyond what is given. Return a JSON object with "
        'exactly two keys: "subject" (short string) and "body" (string).\n'
        f"Recipient: {recipient or 'the relevant person'}\n"
        f"Goal/deliverable at risk: {goal or '(unspecified)'}\n"
        f"Specific part that will NOT be done in time: {unmet_portion or '(unspecified)'}\n"
        f"Original deadline: {deadline or '(unspecified)'}\n"
        f"Realistic new ETA / extension to request: {new_eta or '(suggest a reasonable one)'}"
    )
    cfg = types.GenerateContentConfig(response_mime_type="application/json", temperature=0.4)
    try:
        resp, _model, _log = generate_with_fallback(
            client, [types.Content(role="user", parts=[types.Part.from_text(text=prompt)])], cfg
        )
        obj = json.loads(resp.text)
        if isinstance(obj, dict) and obj.get("body"):
            return {
                "subject": str(obj.get("subject") or f"Update on {goal or 'my deadline'}"),
                "body": str(obj["body"]),
            }
    except (HTTPException, json.JSONDecodeError, TypeError, AttributeError) as e:
        logger.warning("compose_rescue_draft fell back to template: %s", e)
    # Deterministic fallback so a draft is ALWAYS produced for an unmet deadline.
    subject = f"Running behind on {goal or 'a deadline'}"
    body = (
        f"Hi {recipient or '[recipient]'},\n\n"
        f"I want to flag early that {unmet_portion or 'part of this work'} likely will "
        f"not be finished by {deadline or 'the deadline'}. "
        f"I expect to have it done by {new_eta or '[proposed new time]'}. "
        "Please let me know if that works or if we should adjust scope.\n\n"
        "Thanks for understanding."
    )
    return {"subject": subject, "body": body}


def save_draft(sub, *, recipient, subject, body, goal, unmet_portion, source) -> str:
    """Persist a Lane B draft (status 'proposed') under users/{sub}/drafts."""
    ref = db().collection(USERS).document(sub).collection(DRAFTS).document()
    ref.set(
        {
            "channel": "message",      # generic; intentionally NOT email-send
            "recipient": recipient,
            "subject": subject,
            "body": body,
            "goal": goal,
            "unmet_portion": unmet_portion,
            "status": "proposed",      # proposed -> confirmed | dismissed
            "source": source,          # e.g. {"goal": ..., "deadline": ...}
            "sent": False,             # ALWAYS false — Clutch has no send capability
            "created_at": firestore.SERVER_TIMESTAMP,
            "updated_at": firestore.SERVER_TIMESTAMP,
        }
    )
    return ref.id


def _draft_view(doc_id: str, rec: dict) -> dict:
    """Shape a stored draft for API responses (datetimes -> ISO)."""
    return {
        "draft_id": doc_id,
        "status": rec.get("status"),
        "recipient": rec.get("recipient"),
        "subject": rec.get("subject"),
        "body": rec.get("body"),
        "goal": rec.get("goal"),
        "unmet_portion": rec.get("unmet_portion"),
        "sent": rec.get("sent", False),
        "created_at": rec.get("created_at").isoformat() if isinstance(rec.get("created_at"), datetime) else None,
        "updated_at": rec.get("updated_at").isoformat() if isinstance(rec.get("updated_at"), datetime) else None,
        "confirmed_at": rec.get("confirmed_at").isoformat() if isinstance(rec.get("confirmed_at"), datetime) else None,
        "dismissed_at": rec.get("dismissed_at").isoformat() if isinstance(rec.get("dismissed_at"), datetime) else None,
    }


def get_prefs(data: dict) -> dict:
    """Resolve scheduling prefs: per-user (Firestore) overriding module defaults."""
    p = (data or {}).get("prefs") or {}
    return {
        "work_tz": p.get("work_tz", WORK_TZ),
        "work_start_hour": int(p.get("work_start_hour", WORK_START_HOUR)),
        "work_end_hour": int(p.get("work_end_hour", WORK_END_HOUR)),
        "max_block_minutes": int(p.get("max_block_minutes", MAX_BLOCK_MINUTES)),
        "min_block_minutes": int(p.get("min_block_minutes", MIN_BLOCK_MINUTES)),
    }


def _parse_local(dt_str: str, tz: ZoneInfo) -> datetime:
    """Parse an ISO time into the working timezone (naive => assume local tz)."""
    d = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
    if d.tzinfo is None:
        d = d.replace(tzinfo=tz)
    return d.astimezone(tz)


def enforce_working_hours(start_iso: str, end_iso: str, prefs: dict) -> tuple[str, str, str | None]:
    """Clamp a focus block into local working hours and to the max block length.

    Deterministic guard (not a model request): returns (start, end, note) with the
    block forced inside [work_start, work_end] on the start day and <= max length.
    Raises 422 if no valid slot remains, so the model can re-propose.
    """
    tz = ZoneInfo(prefs["work_tz"])
    s = _parse_local(start_iso, tz)
    e = _parse_local(end_iso, tz)

    open_dt = s.replace(hour=prefs["work_start_hour"], minute=0, second=0, microsecond=0)
    close_dt = s.replace(hour=prefs["work_end_hour"], minute=0, second=0, microsecond=0)

    new_s = max(s, open_dt)
    new_e = min(e, close_dt, new_s + timedelta(minutes=prefs["max_block_minutes"]))

    if new_e <= new_s or (new_e - new_s) < timedelta(minutes=prefs["min_block_minutes"]):
        raise HTTPException(
            status_code=422,
            detail=(
                f"Proposed block {start_iso}–{end_iso} cannot fit working hours "
                f"{prefs['work_start_hour']:02d}:00–{prefs['work_end_hour']:02d}:00 "
                f"{prefs['work_tz']} (max {prefs['max_block_minutes']} min). "
                "Choose a slot fully inside working hours."
            ),
        )

    note = None
    if new_s != s or new_e != e:
        note = (
            f"clamped into working hours / {prefs['max_block_minutes']}-min cap: "
            f"{new_s.isoformat()} – {new_e.isoformat()}"
        )
    return new_s.isoformat(), new_e.isoformat(), note


def execute_lane_a(sub, data, session, client, name, args, prefs, run_deadline=None, run_goal=None) -> tuple[dict, dict]:
    """Execute one Lane A tool + write an action_log entry. Returns (result, record)."""
    tasks_coll = db().collection(USERS).document(sub).collection(TASKS)

    if name == "create_calendar_event":
        # Deterministically enforce working hours + max block length.
        start, end, adjusted = enforce_working_hours(args["start"], args["end"], prefs)
        # Uniform title that names the parent goal (display/labeling only).
        block_goal = args.get("goal") or run_goal
        title = _format_block_title(args.get("title", "Focus block"), block_goal)
        ev = _cal_create(
            session, title, start, end, args.get("description"), goal=block_goal,
        )
        result = {
            "event_id": ev.get("id"),
            "htmlLink": ev.get("htmlLink"),
            "title": title,
            "goal": block_goal,
            "start": start,
            "end": end,
            "adjusted": adjusted,
        }
        undo = {"type": "delete_event", "event_id": ev.get("id")}

    elif name == "reschedule_event":
        prev = _cal_get(session, args["event_id"])
        _cal_patch(
            session, args["event_id"],
            {"start": _event_time(args["new_start"]), "end": _event_time(args["new_end"])},
        )
        result = {"event_id": args["event_id"], "new_start": args["new_start"], "new_end": args["new_end"]}
        undo = {
            "type": "restore_event_time",
            "event_id": args["event_id"],
            "prev_start": prev.get("start"),
            "prev_end": prev.get("end"),
        }

    elif name == "break_down_task":
        # Anchor dates on NOW + the real deadline (model's deadline arg, else the
        # run-level deadline). All dues are validated/clamped into (now, deadline]
        # so a past or hallucinated date is never written.
        tz = ZoneInfo(prefs["work_tz"])
        now = datetime.now(tz)
        deadline_dt = _safe_parse_local(args.get("deadline") or run_deadline, tz)

        subtasks = decompose_goal(client, args["goal"], now, deadline_dt)
        total = len(subtasks)
        for i, st in enumerate(subtasks):
            st["due"] = _normalize_due(st.get("due"), now, deadline_dt, i, total, tz).isoformat()

        ids = []
        batch = db().batch()
        for st in subtasks:
            ref = tasks_coll.document()
            batch.set(
                ref,
                {
                    "title": st.get("title"),
                    "effort": st.get("effort"),
                    "due": st.get("due"),
                    "status": "todo",
                    "parent_goal": args["goal"],
                    "created_at": firestore.SERVER_TIMESTAMP,
                },
            )
            ids.append(ref.id)
        if ids:
            batch.commit()
        result = {"created_task_ids": ids, "subtasks": subtasks}
        undo = {"type": "delete_tasks", "task_ids": ids}

    elif name == "upsert_task":
        fields = {k: args[k] for k in ("title", "due", "effort", "status") if args.get(k) is not None}
        task_id = args.get("task_id")
        if task_id:
            snap = tasks_coll.document(task_id).get()
            prev = snap.to_dict() if snap.exists else None
            fields["updated_at"] = firestore.SERVER_TIMESTAMP
            tasks_coll.document(task_id).set(fields, merge=True)
            undo = (
                {"type": "delete_task", "task_id": task_id}
                if prev is None
                else {"type": "restore_task", "task_id": task_id, "prev_fields": prev}
            )
        else:
            ref = tasks_coll.document()
            task_id = ref.id
            fields.setdefault("status", "todo")
            fields["created_at"] = firestore.SERVER_TIMESTAMP
            ref.set(fields)
            undo = {"type": "delete_task", "task_id": task_id}
        result = {"task_id": task_id}

    elif name == "reprioritize":
        ids = args.get("ranked_task_ids", [])
        prev = {}
        batch = db().batch()
        for idx, tid in enumerate(ids):
            snap = tasks_coll.document(tid).get()
            prev[tid] = snap.to_dict().get("priority") if snap.exists else None
            batch.set(tasks_coll.document(tid), {"priority": idx}, merge=True)
        if ids:
            batch.commit()
        result = {"ordered": ids}
        undo = {"type": "restore_priorities", "prev": prev}

    else:
        raise HTTPException(500, f"Unhandled Lane A tool: {name}")

    action_id = log_action(sub, name, args, result, undo)
    record = {"action_id": action_id, "name": name, "args": args, "result": result}
    return result, record


def run_agent(sub: str, data: dict, goal: str | None, deadline: str | None, hours: int) -> dict:
    """Agent loop: perceive -> pre-rank -> plan with Gemini, EXECUTING Lane A.

    Lane A (calendar/Firestore writes) is executed and logged for undo. Lane B
    (draft_message) is captured as a proposal only. notify_user just surfaces a
    message. Hard-capped by step count and total actions.
    """
    client = _gemini_client()
    config = types.GenerateContentConfig(
        tools=[types.Tool(function_declarations=FUNCTION_DECLARATIONS)],
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        system_instruction=SYSTEM_PROMPT,
        temperature=0.2,
    )

    # One refreshed session reused for every Calendar write this run (refreshes +
    # persists the token if expired — satisfies the per-write refresh rule).
    creds = _ensure_valid(sub, _credentials_from_doc(data))
    session = AuthorizedSession(creds)
    prefs = get_prefs(data)

    # 1) Perceive deterministically, then pre-rank (auditable).
    perceived = build_snapshot(sub, data, hours, session=session)
    ranking = rank_tasks(perceived["tasks"])

    # 2) Seed the conversation with the goal + snapshot + ranking + policy.
    policy = (
        f"Scheduling policy (ENFORCED server-side): only book focus blocks between "
        f"{prefs['work_start_hour']:02d}:00 and {prefs['work_end_hour']:02d}:00 "
        f"{prefs['work_tz']}; max {prefs['max_block_minutes']} min per block; at most "
        f"{MAX_EVENTS_PER_RUN} events total this run (the run HALTS past that). Break "
        f"the goal into several DISTINCT, granular subtasks and book ONE block per "
        f"subtask, titled after it. Schedule the FULL estimated effort across the "
        f"available free days when it fits before the deadline — do not stop after a "
        f"single block while free time remains. Prefer creating more distinct "
        f"subtasks over repeating one subtask as '(Part 1)'…'(Part N)'; split a "
        f"single subtask across blocks only if its own effort exceeds the "
        f"{prefs['max_block_minutes']}-min cap, spreading those across different days. "
        f"Only when all usable free time before the deadline is booked and work still "
        f"remains is the goal infeasible. Blocks outside working hours are "
        f"rejected/clamped, so propose compliant times."
    )
    user_prompt = (
        f"Goal: {goal or '(no specific goal — review my schedule and improve it)'}\n"
        f"Deadline: {deadline or '(unspecified)'}\n\n"
        f"{policy}\n\n"
        f"Current schedule snapshot (window {perceived['window']['start']} → "
        f"{perceived['window']['end']}):\n"
        f"- events: {perceived['events']}\n"
        f"- busy: {perceived['busy']}\n"
        f"- existing subtasks (deterministically pre-ranked): {ranking}\n\n"
        "Take the actions needed via function calls."
    )
    contents = [types.Content(role="user", parts=[types.Part.from_text(text=user_prompt)])]

    executed_reads = [{"name": "get_schedule_snapshot", "args": {"hours": hours}, "trigger": "initial perceive"}]
    executed_actions: list[dict] = []
    proposed: list[dict] = []
    drafts: list[dict] = []
    notifications: list[dict] = []
    steps_log: list[dict] = []
    last_snapshot = perceived
    final_text = None
    events_created = 0      # dedicated counter for create_calendar_event
    halted = False          # set when a hard cap is hit; stops the run after the turn

    # 3) Plan/act/observe loop, hard-capped.
    for step in range(AGENT_STEP_CAP):
        resp, model, attempts = generate_with_fallback(client, contents, config)
        fcs = resp.function_calls or []
        steps_log.append({"step": step + 1, "model": model, "attempts": attempts,
                          "function_calls": [fc.name for fc in fcs]})

        if not fcs:
            final_text = resp.text
            break

        contents.append(resp.candidates[0].content)  # model's function-call turn
        tool_parts = []
        for fc in fcs:
            name = fc.name
            args = dict(fc.args) if fc.args else {}

            if name == "get_schedule_snapshot":  # read-only execute
                last_snapshot = build_snapshot(sub, data, hours, session=session)
                executed_reads.append({"name": name, "args": args, "trigger": "model-requested"})
                payload = {"snapshot": last_snapshot}

            elif name == "create_calendar_event":  # hard per-run event cap + halt
                if events_created >= MAX_EVENTS_PER_RUN or len(executed_actions) >= AGENT_MAX_ACTIONS:
                    halted = True
                    payload = {"status": "halted",
                               "note": f"per-run cap reached (max {MAX_EVENTS_PER_RUN} events); "
                                       "stop creating events."}
                else:
                    try:
                        result, record = execute_lane_a(
                            sub, data, session, client, name, args, prefs,
                            run_deadline=deadline, run_goal=goal,
                        )
                        executed_actions.append(record)
                        events_created += 1  # count only events that were actually created
                        payload = {"status": "executed", **result}
                    except Exception as e:  # feed the error back so the model can adapt
                        detail = e.detail if isinstance(e, HTTPException) else str(e)
                        logger.warning("create_calendar_event failed: %s", detail)
                        payload = {"status": "error",
                                   "detail": detail if isinstance(detail, (str, dict, list)) else str(detail)}

            elif name in LANE_A_TOOLS:  # other reversible writes (tasks) + log
                if len(executed_actions) >= AGENT_MAX_ACTIONS:
                    payload = {"status": "skipped", "note": f"action limit ({AGENT_MAX_ACTIONS}) reached"}
                else:
                    try:
                        result, record = execute_lane_a(
                            sub, data, session, client, name, args, prefs,
                            run_deadline=deadline, run_goal=goal,
                        )
                        executed_actions.append(record)
                        payload = {"status": "executed", **result}
                    except Exception as e:
                        detail = e.detail if isinstance(e, HTTPException) else str(e)
                        logger.warning("Lane A %s failed: %s", name, detail)
                        payload = {"status": "error",
                                   "detail": detail if isinstance(detail, (str, dict, list)) else str(detail)}

            elif name in LANE_B_PROPOSE:  # draft_message: materialize a draft, NEVER send
                d_goal = args.get("goal") or goal
                unmet = args.get("unmet_portion") or ""
                recipient = args.get("recipient") or "[recipient]"
                # Prefer a body the model already wrote; otherwise compose one with
                # Gemini from the structured context. Either way the draft is proposed.
                if args.get("body"):
                    subject = args.get("subject") or f"Update on {d_goal or 'my deadline'}"
                    body = args["body"]
                else:
                    composed = compose_rescue_draft(
                        client, d_goal, unmet, deadline, args.get("new_eta"), recipient
                    )
                    subject, body = composed["subject"], composed["body"]
                draft_id = save_draft(
                    sub, recipient=recipient, subject=subject, body=body,
                    goal=d_goal, unmet_portion=unmet,
                    source={"goal": goal, "deadline": deadline},
                )
                view = {"draft_id": draft_id, "recipient": recipient, "subject": subject,
                        "body": body, "goal": d_goal, "unmet_portion": unmet, "status": "proposed"}
                drafts.append(view)
                proposed.append({"name": name, "draft_id": draft_id})
                payload = {"status": "drafted_pending_confirmation",
                           "draft_id": draft_id, "recipient": recipient,
                           "subject": subject, "body": body,
                           "note": "Lane B: draft saved for the user to confirm/edit/dismiss. "
                                   "NOT sent (Clutch has no send capability)."}

            elif name == "notify_user":
                notifications.append({"message": args.get("message"), "urgency": args.get("urgency", "normal")})
                payload = {"status": "delivered"}

            else:
                payload = {"status": "unknown_tool"}

            tool_parts.append(types.Part.from_function_response(name=name, response=payload))
        contents.append(types.Content(role="tool", parts=tool_parts))

        if halted:  # a hard cap was hit this turn — stop the run now
            final_text = (f"Halted: per-run cap reached ({events_created} events, "
                          f"{len(executed_actions)} actions).")
            break
    else:
        final_text = f"Step cap ({AGENT_STEP_CAP}) reached before the model finished."

    return {
        "goal": goal,
        "deadline": deadline,
        "models": {"primary": PRIMARY_MODEL, "fallback": FALLBACK_MODEL},
        "caps": {"max_events_per_run": MAX_EVENTS_PER_RUN, "max_actions": AGENT_MAX_ACTIONS},
        "steps": steps_log,
        "snapshot": last_snapshot,
        "ranking": ranking,
        "executed_reads": executed_reads,
        "executed_actions": executed_actions,
        "events_created": events_created,
        "halted": halted,
        "proposed_writes_not_executed": proposed,
        "drafts": drafts,  # Lane B rescue messages, proposed (never sent)
        "notifications": notifications,
        "final_text": final_text,
    }


class PlanRequest(BaseModel):
    goal: str
    deadline: str | None = None
    hours: int = 48


def _require_user(request: Request) -> tuple[str, dict]:
    sub = request.cookies.get(UID_COOKIE)
    data = load_user(sub) if sub else None
    if not data:
        raise HTTPException(status_code=401, detail="Not logged in. Visit /login first.")
    return sub, data


class PrefsRequest(BaseModel):
    work_tz: str | None = None
    work_start_hour: int | None = None
    work_end_hour: int | None = None
    max_block_minutes: int | None = None
    min_block_minutes: int | None = None


@app.get("/prefs")
def get_prefs_route(request: Request):
    """Show the effective scheduling prefs (per-user overrides + defaults)."""
    _, data = _require_user(request)
    return get_prefs(data)


@app.post("/prefs")
def set_prefs_route(body: PrefsRequest, request: Request):
    """Update per-user scheduling prefs in Firestore (users/{sub}.prefs)."""
    sub, _ = _require_user(request)
    updates = body.model_dump(exclude_none=True)

    if "work_tz" in updates:
        try:
            ZoneInfo(updates["work_tz"])  # reject unknown timezones early
        except Exception:
            raise HTTPException(status_code=400, detail=f"Unknown timezone: {updates['work_tz']}")
    for h in ("work_start_hour", "work_end_hour"):
        if h in updates and not (0 <= updates[h] <= 24):
            raise HTTPException(status_code=400, detail=f"{h} must be between 0 and 24.")
    start_h = updates.get("work_start_hour", WORK_START_HOUR)
    end_h = updates.get("work_end_hour", WORK_END_HOUR)
    if start_h >= end_h:
        raise HTTPException(status_code=400, detail="work_start_hour must be before work_end_hour.")

    if updates:
        # Dotted paths update nested map fields without clobbering siblings.
        db().collection(USERS).document(sub).update({f"prefs.{k}": v for k, v in updates.items()})
    return get_prefs(load_user(sub))


@app.post("/agent/plan")
def agent_plan(body: PlanRequest, request: Request):
    """Plan for a specific goal+deadline. Read/propose only (no writes)."""
    if body.hours < 1 or body.hours > 24 * 30:
        raise HTTPException(status_code=400, detail="hours must be between 1 and 720.")
    sub, data = _require_user(request)
    return run_agent(sub, data, body.goal, body.deadline, body.hours)


@app.get("/agent/run")
def agent_run(
    request: Request,
    hours: int = 48,
    goal: str | None = None,
    deadline: str | None = None,
):
    """Review the schedule (optionally for a goal) and ACT (Lane A executed).

    Pass `deadline` as ISO 8601 (e.g. 2026-06-27T18:00:00+05:30) so subtask due
    dates are bounded by a real deadline. Recommended whenever `goal` is set.
    """
    if hours < 1 or hours > 24 * 30:
        raise HTTPException(status_code=400, detail="hours must be between 1 and 720.")
    if goal and not deadline:
        logger.warning("agent_run called with a goal but no deadline; dues will be paced, not bounded.")
    sub, data = _require_user(request)
    return run_agent(sub, data, goal, deadline, hours)


# --- Demo / guest mode (no OAuth, no Firestore, no real calendar writes) ------
# A PARALLEL path so judges can experience the REAL agent reasoning (Gemini
# breakdown, free-time weighing, block placement, working-hours enforcement, caps,
# Lane B rescue drafts) sandboxed against a seeded in-memory calendar. It REUSES
# the live reasoning/calendar helpers verbatim — only the calendar SOURCE (seed)
# and SINK (in-memory, returned to the UI) differ. None of the live functions
# (run_agent, execute_lane_a, build_snapshot, _cal_*) are modified or invoked with
# real credentials here.

# Fixed realistic student week (classes/labs), so the demo is consistent. Times
# are local working-tz; mornings, mid-afternoon gaps, evenings and weekends stay
# partly free so a multi-hour goal has room to schedule.
_DEMO_WEEK = {
    0: [("Data Structures Lecture", 10, 0, 11, 0), ("Operating Systems Lecture", 14, 0, 15, 30)],
    1: [("Physics Lab", 11, 0, 13, 0), ("Linear Algebra Tutorial", 15, 0, 16, 0)],
    2: [("Data Structures Lecture", 10, 0, 11, 0), ("Operating Systems Lecture", 14, 0, 15, 30)],
    3: [("Physics Lab", 11, 0, 13, 0), ("Linear Algebra Tutorial", 15, 0, 16, 0)],
    4: [("Data Structures Lecture", 10, 0, 11, 0), ("Career Workshop", 16, 0, 17, 0)],
    5: [("Football Practice", 17, 0, 18, 30)],
    6: [("Group Project Sync", 16, 0, 17, 0)],
}


def _demo_seed_events(now_local: datetime, tz: ZoneInfo) -> list[dict]:
    """Google-Calendar-shaped seed events for the next 7 days (fixed weekly pattern)."""
    events: list[dict] = []
    base = now_local.date()
    n = 0
    for off in range(7):
        day = base + timedelta(days=off)
        for (summary, sh, sm, eh, em) in _DEMO_WEEK.get(day.weekday(), []):
            s = datetime(day.year, day.month, day.day, sh, sm, tzinfo=tz)
            e = datetime(day.year, day.month, day.day, eh, em, tzinfo=tz)
            n += 1
            events.append({
                "id": f"seed-{n}",
                "summary": summary,
                "start": {"dateTime": s.isoformat()},
                "end": {"dateTime": e.isoformat()},
                "status": "confirmed",
            })
    return events


def _demo_parse(iso: str) -> datetime:
    d = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


class _DemoResponse:
    """Minimal stand-in for a requests.Response (status_code / .json() / .text)."""

    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        return self._payload

    @property
    def text(self) -> str:
        return json.dumps(self._payload)


class DemoSession:
    """Duck-typed stand-in for AuthorizedSession against a seeded in-memory calendar.

    Serves the SAME calendar helpers the live agent uses (_list_events, _freebusy,
    _cal_create/_cal_get/_cal_patch/_cal_delete) and captures 'booked' blocks in
    memory. No network, no Google API, no writes to any real calendar.
    """

    def __init__(self, seed_events: list[dict]):
        self.events = [dict(e) for e in seed_events]
        self._counter = 0

    @staticmethod
    def _bounds(ev: dict) -> tuple[datetime, datetime]:
        s = ev["start"].get("dateTime") or ev["start"].get("date")
        e = ev["end"].get("dateTime") or ev["end"].get("date")
        return _demo_parse(s), _demo_parse(e)

    def get(self, url, params=None, **_):
        params = params or {}
        if url.endswith("/events"):  # events.list
            tmin, tmax = _demo_parse(params["timeMin"]), _demo_parse(params["timeMax"])
            want_clutch = params.get("privateExtendedProperty") == f"{CLUTCH_MARKER}=1"
            items = []
            for ev in self.events:
                if want_clutch:
                    priv = (ev.get("extendedProperties", {}) or {}).get("private", {}) or {}
                    if priv.get(CLUTCH_MARKER) != "1":
                        continue
                s, e = self._bounds(ev)
                if e <= tmin or s >= tmax:
                    continue
                items.append(ev)
            items.sort(key=lambda ev: self._bounds(ev)[0])
            return _DemoResponse(200, {"items": items})
        eid = url.rsplit("/", 1)[-1]  # events.get
        for ev in self.events:
            if ev.get("id") == eid:
                return _DemoResponse(200, ev)
        return _DemoResponse(404, {"error": "not found"})

    def post(self, url, json=None, **_):
        body = json or {}
        if url.endswith("/freeBusy"):
            tmin, tmax = _demo_parse(body["timeMin"]), _demo_parse(body["timeMax"])
            busy = []
            for ev in self.events:
                s, e = self._bounds(ev)
                if e <= tmin or s >= tmax:
                    continue
                busy.append({
                    "start": ev["start"].get("dateTime") or ev["start"].get("date"),
                    "end": ev["end"].get("dateTime") or ev["end"].get("date"),
                })
            return _DemoResponse(200, {"calendars": {"primary": {"busy": busy}}})
        if url.endswith("/events"):  # events.insert
            self._counter += 1
            ev = dict(body)
            ev["id"] = f"demo-evt-{self._counter}"
            ev["htmlLink"] = "#demo"
            ev["status"] = "confirmed"
            self.events.append(ev)
            return _DemoResponse(200, ev)
        return _DemoResponse(400, {"error": "unsupported"})

    def patch(self, url, json=None, **_):
        eid = url.rsplit("/", 1)[-1]
        for ev in self.events:
            if ev.get("id") == eid:
                ev.update(json or {})
                return _DemoResponse(200, ev)
        return _DemoResponse(404, {"error": "not found"})

    def delete(self, url, **_):
        eid = url.rsplit("/", 1)[-1]
        self.events = [e for e in self.events if e.get("id") != eid]
        return _DemoResponse(204, {})


def _demo_execute(state: dict, client, name, args, prefs, run_deadline=None, run_goal=None) -> dict:
    """In-memory mirror of execute_lane_a's tool dispatch for demo mode.

    REUSES the same reasoning helpers (enforce_working_hours, _format_block_title,
    _cal_create against the DemoSession, decompose_goal, _normalize_due) — only the
    persistence is in-memory instead of Firestore. Returns the result payload.
    """
    session = state["session"]

    if name == "create_calendar_event":
        start, end, adjusted = enforce_working_hours(args["start"], args["end"], prefs)
        block_goal = args.get("goal") or run_goal
        title = _format_block_title(args.get("title", "Focus block"), block_goal)
        ev = _cal_create(session, title, start, end, args.get("description"), goal=block_goal)
        return {"event_id": ev.get("id"), "htmlLink": ev.get("htmlLink"), "title": title,
                "goal": block_goal, "start": start, "end": end, "adjusted": adjusted}

    if name == "reschedule_event":
        prev = _cal_get(session, args["event_id"])
        _cal_patch(session, args["event_id"],
                   {"start": _event_time(args["new_start"]), "end": _event_time(args["new_end"])})
        return {"event_id": args["event_id"], "new_start": args["new_start"],
                "new_end": args["new_end"], "_prev": prev.get("start")}

    if name == "break_down_task":
        tz = ZoneInfo(prefs["work_tz"])
        now = datetime.now(tz)
        deadline_dt = _safe_parse_local(args.get("deadline") or run_deadline, tz)
        subtasks = decompose_goal(client, args["goal"], now, deadline_dt)
        total = len(subtasks)
        ids = []
        for i, st in enumerate(subtasks):
            st["due"] = _normalize_due(st.get("due"), now, deadline_dt, i, total, tz).isoformat()
            tid = f"demo-task-{len(state['tasks']) + 1}"
            state["tasks"].append({"id": tid, "title": st.get("title"), "effort": st.get("effort"),
                                   "due": st["due"], "status": "todo", "parent_goal": args["goal"]})
            ids.append(tid)
        return {"created_task_ids": ids, "subtasks": subtasks}

    if name == "upsert_task":
        tid = args.get("task_id")
        fields = {k: v for k, v in args.items() if k != "task_id"}
        for t in state["tasks"]:
            if t["id"] == tid:
                t.update(fields)
                break
        else:
            tid = f"demo-task-{len(state['tasks']) + 1}"
            state["tasks"].append({"id": tid, "status": "todo", **fields})
        return {"task_id": tid}

    if name == "reprioritize":
        return {"ordered": args.get("ranked_task_ids", [])}

    raise HTTPException(500, f"Unhandled demo Lane A tool: {name}")


def run_agent_demo(goal: str | None, deadline: str | None, hours: int) -> dict:
    """Parallel agent loop for demo mode. Same orchestration + the SAME reasoning
    primitives as run_agent, but reads the seeded calendar and writes in-memory."""
    client = _gemini_client()
    config = types.GenerateContentConfig(
        tools=[types.Tool(function_declarations=FUNCTION_DECLARATIONS)],
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        system_instruction=SYSTEM_PROMPT,
        temperature=0.2,
    )
    prefs = get_prefs({})  # module defaults (no user doc)
    tz = ZoneInfo(prefs["work_tz"])
    session = DemoSession(_demo_seed_events(datetime.now(tz), tz))
    state = {"session": session, "tasks": []}

    # 1) Perceive (live read helpers against the demo session; tasks start empty).
    now = datetime.now(timezone.utc)
    end = now + timedelta(hours=hours)
    tmin, tmax = now.isoformat(), end.isoformat()

    def _snapshot() -> dict:
        events = _list_events(session, tmin, tmax)
        busy = _freebusy(session, tmin, tmax)
        tasks = _jsonify(state["tasks"])
        return {"window": {"start": tmin, "end": tmax, "hours": hours, "timezone": "UTC"},
                "counts": {"events": len(events), "busy_blocks": len(busy), "tasks": len(tasks)},
                "events": events, "busy": busy, "tasks": tasks}

    perceived = _snapshot()
    ranking = rank_tasks(perceived["tasks"])

    policy = (
        f"Scheduling policy (ENFORCED server-side): only book focus blocks between "
        f"{prefs['work_start_hour']:02d}:00 and {prefs['work_end_hour']:02d}:00 "
        f"{prefs['work_tz']}; max {prefs['max_block_minutes']} min per block; at most "
        f"{MAX_EVENTS_PER_RUN} events total this run (the run HALTS past that). Break "
        f"the goal into several DISTINCT, granular subtasks and book ONE block per "
        f"subtask, titled after it. Schedule the FULL estimated effort across the "
        f"available free days when it fits before the deadline — do not stop after a "
        f"single block while free time remains. Prefer creating more distinct "
        f"subtasks over repeating one subtask as '(Part 1)'…'(Part N)'; split a "
        f"single subtask across blocks only if its own effort exceeds the "
        f"{prefs['max_block_minutes']}-min cap, spreading those across different days. "
        f"Only when all usable free time before the deadline is booked and work still "
        f"remains is the goal infeasible. Blocks outside working hours are "
        f"rejected/clamped, so propose compliant times."
    )
    user_prompt = (
        f"Goal: {goal or '(no specific goal — review my schedule and improve it)'}\n"
        f"Deadline: {deadline or '(unspecified)'}\n\n"
        f"{policy}\n\n"
        f"Current schedule snapshot (window {perceived['window']['start']} → "
        f"{perceived['window']['end']}):\n"
        f"- events: {perceived['events']}\n"
        f"- busy: {perceived['busy']}\n"
        f"- existing subtasks (deterministically pre-ranked): {ranking}\n\n"
        "Take the actions needed via function calls."
    )
    contents = [types.Content(role="user", parts=[types.Part.from_text(text=user_prompt)])]

    executed_reads = [{"name": "get_schedule_snapshot", "args": {"hours": hours}, "trigger": "initial perceive"}]
    executed_actions: list[dict] = []
    proposed: list[dict] = []
    drafts: list[dict] = []
    notifications: list[dict] = []
    steps_log: list[dict] = []
    last_snapshot = perceived
    final_text = None
    events_created = 0
    halted = False

    for step in range(AGENT_STEP_CAP):
        resp, model, attempts = generate_with_fallback(client, contents, config)
        fcs = resp.function_calls or []
        steps_log.append({"step": step + 1, "model": model, "attempts": attempts,
                          "function_calls": [fc.name for fc in fcs]})

        if not fcs:
            final_text = resp.text
            break

        contents.append(resp.candidates[0].content)
        tool_parts = []
        for fc in fcs:
            name = fc.name
            args = dict(fc.args) if fc.args else {}

            if name == "get_schedule_snapshot":
                last_snapshot = _snapshot()
                executed_reads.append({"name": name, "args": args, "trigger": "model-requested"})
                payload = {"snapshot": last_snapshot}

            elif name == "create_calendar_event":
                if events_created >= MAX_EVENTS_PER_RUN or len(executed_actions) >= AGENT_MAX_ACTIONS:
                    halted = True
                    payload = {"status": "halted",
                               "note": f"per-run cap reached (max {MAX_EVENTS_PER_RUN} events); stop creating events."}
                else:
                    try:
                        result = _demo_execute(state, client, name, args, prefs,
                                               run_deadline=deadline, run_goal=goal)
                        executed_actions.append({"name": name, "args": args, "result": result})
                        events_created += 1
                        payload = {"status": "executed", **result}
                    except Exception as e:  # noqa: BLE001 — feed back so the model can adapt
                        payload = {"status": "error", "detail": str(e)}

            elif name in LANE_A_TOOLS:
                if len(executed_actions) >= AGENT_MAX_ACTIONS:
                    payload = {"status": "skipped", "note": f"action limit ({AGENT_MAX_ACTIONS}) reached"}
                else:
                    try:
                        result = _demo_execute(state, client, name, args, prefs,
                                               run_deadline=deadline, run_goal=goal)
                        executed_actions.append({"name": name, "args": args, "result": result})
                        payload = {"status": "executed", **result}
                    except Exception as e:  # noqa: BLE001
                        payload = {"status": "error", "detail": str(e)}

            elif name in LANE_B_PROPOSE:
                d_goal = args.get("goal") or goal
                unmet = args.get("unmet_portion") or ""
                recipient = args.get("recipient") or "[recipient]"
                if args.get("body"):
                    subject = args.get("subject") or f"Update on {d_goal or 'my deadline'}"
                    body = args["body"]
                else:
                    composed = compose_rescue_draft(client, d_goal, unmet, deadline, args.get("new_eta"), recipient)
                    subject, body = composed["subject"], composed["body"]
                draft_id = f"demo-draft-{len(drafts) + 1}"
                view = {"draft_id": draft_id, "recipient": recipient, "subject": subject,
                        "body": body, "goal": d_goal, "unmet_portion": unmet, "status": "proposed"}
                drafts.append(view)
                proposed.append({"name": name, "draft_id": draft_id})
                payload = {"status": "drafted_pending_confirmation", "draft_id": draft_id,
                           "recipient": recipient, "subject": subject, "body": body,
                           "note": "Lane B: demo draft (never sent, not persisted)."}

            elif name == "notify_user":
                notifications.append({"message": args.get("message"), "urgency": args.get("urgency", "normal")})
                payload = {"status": "delivered"}

            else:
                payload = {"status": "unknown_tool"}

            tool_parts.append(types.Part.from_function_response(name=name, response=payload))
        contents.append(types.Content(role="tool", parts=tool_parts))

        if halted:
            final_text = (f"Halted: per-run cap reached ({events_created} events, "
                          f"{len(executed_actions)} actions).")
            break
    else:
        final_text = f"Step cap ({AGENT_STEP_CAP}) reached before the model finished."

    # Build a UI timeline: seed (user) events + booked (Kairos) blocks, flagged.
    final_events = _list_events(session, tmin, tmax)
    goal_by_id, clutch_ids = {}, set()
    for ev in session.events:
        priv = (ev.get("extendedProperties", {}) or {}).get("private", {}) or {}
        if priv.get(CLUTCH_MARKER) == "1":
            clutch_ids.add(ev.get("id"))
            if priv.get("clutch_goal"):
                goal_by_id[ev.get("id")] = priv["clutch_goal"]
    timeline = []
    for ev in final_events:
        item = dict(ev)
        item["kairos"] = ev.get("id") in clutch_ids
        if item["kairos"] and goal_by_id.get(ev.get("id")):
            item["goal"] = goal_by_id[ev["id"]]
        timeline.append(item)

    return {
        "demo": True,
        "goal": goal,
        "deadline": deadline,
        "models": {"primary": PRIMARY_MODEL, "fallback": FALLBACK_MODEL},
        "caps": {"max_events_per_run": MAX_EVENTS_PER_RUN, "max_actions": AGENT_MAX_ACTIONS},
        "steps": steps_log,
        "snapshot": last_snapshot,
        "ranking": ranking,
        "executed_reads": executed_reads,
        "executed_actions": executed_actions,
        "events_created": events_created,
        "halted": halted,
        "proposed_writes_not_executed": proposed,
        "drafts": drafts,
        "notifications": notifications,
        "final_text": final_text,
        "timeline": timeline,
    }


class DemoRunRequest(BaseModel):
    goal: str | None = None
    deadline: str | None = None
    hours: int = 72


@app.post("/agent/demo/run")
def agent_demo_run(body: DemoRunRequest):
    """GUEST/DEMO mode: run the REAL agent reasoning against a seeded in-memory
    calendar. No OAuth, no Firestore user, no real calendar writes. Gemini still
    runs via Vertex (project credit). Returns the same shape as /agent/run plus a
    pre-built `timeline`. Live login/calendar/write paths are untouched."""
    if body.hours < 1 or body.hours > 24 * 30:
        raise HTTPException(status_code=400, detail="hours must be between 1 and 720.")
    return run_agent_demo(body.goal, body.deadline, body.hours)


# --- Undo (reverses logged actions; never guesses) ---------------------------


def _reverse_action(sub: str, session: AuthorizedSession, rec: dict) -> None:
    undo = rec.get("undo") or {}
    t = undo.get("type")
    tasks_coll = db().collection(USERS).document(sub).collection(TASKS)

    if t == "delete_event":
        _cal_delete(session, undo["event_id"])
    elif t == "restore_event_time":
        _cal_patch(session, undo["event_id"], {"start": undo["prev_start"], "end": undo["prev_end"]})
    elif t == "delete_task":
        tasks_coll.document(undo["task_id"]).delete()
    elif t == "delete_tasks":
        batch = db().batch()
        for tid in undo.get("task_ids", []):
            batch.delete(tasks_coll.document(tid))
        batch.commit()
    elif t == "restore_task":
        tasks_coll.document(undo["task_id"]).set(undo["prev_fields"])  # full replace
    elif t == "restore_priorities":
        batch = db().batch()
        for tid, pr in (undo.get("prev") or {}).items():
            batch.set(tasks_coll.document(tid), {"priority": pr}, merge=True)
        batch.commit()
    else:
        raise HTTPException(status_code=400, detail=f"Unknown or missing undo type: {t}")


def _do_undo(sub: str, data: dict, snap) -> dict:
    rec = snap.to_dict()
    if rec.get("undone"):
        raise HTTPException(status_code=409, detail="Action already undone.")
    creds = _ensure_valid(sub, _credentials_from_doc(data))
    session = AuthorizedSession(creds)
    _reverse_action(sub, session, rec)
    snap.reference.set({"undone": True, "undone_at": firestore.SERVER_TIMESTAMP}, merge=True)
    return {
        "undone": True,
        "action_id": snap.id,
        "action": rec.get("action"),
        "undo_type": (rec.get("undo") or {}).get("type"),
    }


@app.get("/agent/actions")
def agent_actions(request: Request, limit: int = 20):
    """List recent actions (newest first) with undo status."""
    sub, _ = _require_user(request)
    docs = (
        db().collection(USERS).document(sub).collection(ACTION_LOG)
        .order_by("created_at", direction=firestore.Query.DESCENDING)
        .limit(limit)
        .stream()
    )
    out = []
    for d in docs:
        r = d.to_dict()
        created = r.get("created_at")
        out.append({
            "action_id": d.id,
            "action": r.get("action"),
            "result": r.get("result"),
            "undone": r.get("undone", False),
            "created_at": created.isoformat() if created else None,
        })
    return {"actions": out}


@app.post("/agent/undo")
def agent_undo(request: Request):
    """Undo the most recent action that hasn't been undone yet."""
    sub, data = _require_user(request)
    docs = (
        db().collection(USERS).document(sub).collection(ACTION_LOG)
        .order_by("created_at", direction=firestore.Query.DESCENDING)
        .limit(25)
        .stream()
    )
    for d in docs:
        if not d.to_dict().get("undone"):
            return _do_undo(sub, data, d)
    raise HTTPException(status_code=404, detail="No action available to undo.")


@app.post("/agent/undo/{action_id}")
def agent_undo_by_id(action_id: str, request: Request):
    """Undo a specific logged action by id."""
    sub, data = _require_user(request)
    snap = db().collection(USERS).document(sub).collection(ACTION_LOG).document(action_id).get()
    if not snap.exists:
        raise HTTPException(status_code=404, detail="Action not found.")
    return _do_undo(sub, data, snap)


@app.post("/agent/undo-all")
def agent_undo_all(request: Request):
    """Bulk clear: delete ALL Clutch-created calendar events + clean up subtasks.

    SAFETY: only events carrying the clutch marker are deleted (server-side
    privateExtendedProperty filter, re-checked per event). The user's own real
    events are never touched. Also reverts task writes and marks the action log
    undone. Returns a summary.
    """
    sub, data = _require_user(request)
    creds = _ensure_valid(sub, _credentials_from_doc(data))
    session = AuthorizedSession(creds)

    now = datetime.now(timezone.utc)
    t_min = (now - timedelta(days=30)).isoformat()
    t_max = (now + timedelta(days=365)).isoformat()

    # 1) Delete ONLY clutch-marked events (double-checked before each delete).
    events_deleted = 0
    events_skipped_unmarked = 0
    for ev in _list_clutch_events(session, t_min, t_max):
        private = (ev.get("extendedProperties", {}) or {}).get("private", {}) or {}
        if private.get(CLUTCH_MARKER) != "1":
            events_skipped_unmarked += 1  # belt-and-suspenders: never delete unmarked
            continue
        _cal_delete(session, ev["id"])
        events_deleted += 1

    # 2) Walk the action log: clean up subtasks, revert reschedules, mark undone.
    tasks_deleted = 0
    reschedules_reverted = 0
    actions_marked = 0
    tasks_coll = db().collection(USERS).document(sub).collection(TASKS)
    for d in db().collection(USERS).document(sub).collection(ACTION_LOG).stream():
        rec = d.to_dict()
        if rec.get("undone"):
            continue
        undo = rec.get("undo") or {}
        t = undo.get("type")
        if t == "delete_tasks":
            for tid in undo.get("task_ids", []):
                tasks_coll.document(tid).delete()
                tasks_deleted += 1
        elif t == "delete_task":
            tasks_coll.document(undo["task_id"]).delete()
            tasks_deleted += 1
        elif t == "restore_event_time":
            try:
                _cal_patch(session, undo["event_id"], {"start": undo["prev_start"], "end": undo["prev_end"]})
                reschedules_reverted += 1
            except HTTPException:
                pass  # event may have been a clutch block already deleted in step 1
        elif t == "restore_task":
            tasks_coll.document(undo["task_id"]).set(undo["prev_fields"])
        elif t == "restore_priorities":
            for tid, pr in (undo.get("prev") or {}).items():
                tasks_coll.document(tid).set({"priority": pr}, merge=True)
        # delete_event handled by the marker scan in step 1
        d.reference.set({"undone": True, "undone_at": firestore.SERVER_TIMESTAMP}, merge=True)
        actions_marked += 1

    return {
        "events_deleted": events_deleted,
        "events_skipped_unmarked": events_skipped_unmarked,
        "tasks_deleted": tasks_deleted,
        "reschedules_reverted": reschedules_reverted,
        "actions_marked_undone": actions_marked,
    }


@app.post("/agent/undo-event/{event_id}")
def agent_undo_event(event_id: str, request: Request):
    """Targeted delete of ONE Clutch focus block by event id.

    SAFETY: reuses the exact marked-only guarantee as undo-all — we fetch the
    event and verify it carries the clutch marker before deleting. A user's own
    (unmarked) event is NEVER deleted; we 403 instead. Idempotent on already-gone.
    Also marks any matching action_log delete_event record undone so the receipt
    stays consistent. Does NOT touch scheduling/planning.
    """
    sub, data = _require_user(request)
    creds = _ensure_valid(sub, _credentials_from_doc(data))
    session = AuthorizedSession(creds)

    try:
        ev = _cal_get(session, event_id)
    except HTTPException as e:
        if e.status_code == 502 and "404" in str(e.detail):
            return {"deleted": False, "already_gone": True, "event_id": event_id}
        raise

    private = (ev.get("extendedProperties", {}) or {}).get("private", {}) or {}
    if private.get(CLUTCH_MARKER) != "1":
        # Refuse to delete the user's own real event — same boundary as undo-all.
        raise HTTPException(status_code=403, detail="Not a Kairos block — refusing to delete.")

    _cal_delete(session, event_id)

    # Keep the action log consistent: mark the matching create as undone.
    actions_marked = 0
    for d in db().collection(USERS).document(sub).collection(ACTION_LOG).stream():
        rec = d.to_dict()
        if rec.get("undone"):
            continue
        undo = rec.get("undo") or {}
        if undo.get("type") == "delete_event" and undo.get("event_id") == event_id:
            d.reference.set({"undone": True, "undone_at": firestore.SERVER_TIMESTAMP}, merge=True)
            actions_marked += 1

    return {"deleted": True, "event_id": event_id, "actions_marked_undone": actions_marked}


# --- Lane B drafts: list / confirm / edit / dismiss (confirm-first, no send) --


class DraftEditRequest(BaseModel):
    recipient: str | None = None
    subject: str | None = None
    body: str | None = None


def _get_draft_ref_or_404(sub: str, draft_id: str):
    ref = db().collection(USERS).document(sub).collection(DRAFTS).document(draft_id)
    snap = ref.get()
    if not snap.exists:
        raise HTTPException(status_code=404, detail="Draft not found.")
    return ref, snap.to_dict()


@app.get("/agent/drafts")
def agent_drafts(request: Request, status: str | None = None, limit: int = 20):
    """List Lane B rescue drafts (newest first). Optional ?status= filter."""
    sub, _ = _require_user(request)
    q = (
        db().collection(USERS).document(sub).collection(DRAFTS)
        .order_by("created_at", direction=firestore.Query.DESCENDING)
        .limit(limit)
    )
    out = [_draft_view(d.id, d.to_dict()) for d in q.stream()]
    if status:
        out = [d for d in out if d["status"] == status]
    return {"drafts": out}


@app.post("/agent/draft/{draft_id}/confirm")
def agent_draft_confirm(draft_id: str, request: Request):
    """Approve a draft. This ONLY marks it confirmed — it is NEVER sent.

    Clutch has no send capability (no Gmail-send scope), by design. Confirming
    records the user's approval so a human can copy/send it themselves.
    """
    sub, _ = _require_user(request)
    ref, rec = _get_draft_ref_or_404(sub, draft_id)
    if rec.get("status") == "dismissed":
        raise HTTPException(status_code=409, detail="Cannot confirm a dismissed draft.")
    ref.set(
        {"status": "confirmed", "sent": False,
         "confirmed_at": firestore.SERVER_TIMESTAMP, "updated_at": firestore.SERVER_TIMESTAMP},
        merge=True,
    )
    return {
        "draft": _draft_view(draft_id, ref.get().to_dict()),
        "note": "Marked approved. NOT sent — Clutch has no send capability (intentional).",
    }


@app.post("/agent/draft/{draft_id}/edit")
def agent_draft_edit(draft_id: str, body: DraftEditRequest, request: Request):
    """Edit a draft's recipient/subject/body. Keeps/returns it as 'proposed'."""
    sub, _ = _require_user(request)
    ref, rec = _get_draft_ref_or_404(sub, draft_id)
    if rec.get("status") == "dismissed":
        raise HTTPException(status_code=409, detail="Cannot edit a dismissed draft.")
    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=400, detail="Provide at least one of recipient/subject/body.")
    # An edit re-opens the draft for review (back to proposed) and clears approval.
    updates.update({"status": "proposed", "confirmed_at": None,
                    "updated_at": firestore.SERVER_TIMESTAMP})
    ref.set(updates, merge=True)
    return {"draft": _draft_view(draft_id, ref.get().to_dict())}


@app.post("/agent/draft/{draft_id}/dismiss")
def agent_draft_dismiss(draft_id: str, request: Request):
    """Dismiss (reject) a draft. It is kept for the record, marked dismissed."""
    sub, _ = _require_user(request)
    ref, _rec = _get_draft_ref_or_404(sub, draft_id)
    ref.set(
        {"status": "dismissed", "dismissed_at": firestore.SERVER_TIMESTAMP,
         "updated_at": firestore.SERVER_TIMESTAMP},
        merge=True,
    )
    return {"draft": _draft_view(draft_id, ref.get().to_dict())}
