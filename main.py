"""Clutch — FastAPI app.

Build step 3: Firestore as the persistent state store.

Direct Google OAuth 2.0 (design.md §5) with BOTH the in-flight OAuth verifier
and the durable user tokens now stored in Firestore (was in-memory + a local
JSON file). This makes the /login -> /oauth2callback handshake survive across
multiple Cloud Run instances, and makes tokens survive restarts/cold starts.

Firestore uses Application Default Credentials (no service-account key file).

Scope of THIS step: Firestore setup + moving verifier/token storage into it.
No Gemini agent loop or calendar-reading logic yet.

--------------------------------------------------------------------------------
Firestore data model (Native mode)
--------------------------------------------------------------------------------
Collection `oauth_states`  — ephemeral OAuth handshake state (one-time use)
  Document {state}                 # the OAuth `state` string Google echoes back
    code_verifier : str            # PKCE verifier generated during /login
    created_at    : timestamp      # server time; enables an optional TTL policy
  Deleted immediately after the token exchange. (Configure a Firestore TTL
  policy on `created_at` to also sweep abandoned handshakes — optional.)

Collection `users`         — durable per-user data, keyed by Google account id
  Document {sub}                   # Google `sub` = stable, never-reused user id
    email         : str
    name          : str
    token         : str            # current OAuth access token
    refresh_token : str            # durable refresh token (the §5 requirement)
    token_uri     : str
    client_id     : str
    client_secret : str
    scopes        : [str]
    updated_at    : timestamp
    # Placeholders for later steps (NOT created yet; Firestore makes collections
    # lazily). Planned subcollections under each user document:
    #   tasks/{taskId}     subtasks: title, effort, status, parent_goal, due, ...
    #   plan/{planId}      plan ledger: what was scheduled, why, outcome
    #   actions/{actionId} action log for undo / receipts
"""

import logging
import os
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
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

# Cookie holding the opaque user id (Google `sub`) so /me knows which user's
# tokens to read from Firestore. It is NOT a credential — the tokens stay
# server-side in Firestore; this only points at the right document.
UID_COOKIE = "clutch_uid"

# --- Gemini agent config (model names change often — edit them HERE) ----------
# Primary = fast/cheap current Flash; fallback = a DIFFERENT Gemini model used
# only on the primary's rate-limit (429) or timeout. Both kept all-Google.
PRIMARY_MODEL = "gemini-2.5-flash"
FALLBACK_MODEL = "gemini-2.5-flash-lite"
GEMINI_TIMEOUT_MS = 30_000          # per-call timeout; a timeout triggers fallback
AGENT_STEP_CAP = 8                  # max Gemini turns per run; never loop forever

# Tool names the agent is ALLOWED to actually execute in this build. Everything
# else (calendar/Firestore writes, drafts) is captured as a proposal only.
EXECUTABLE_TOOLS = {"get_schedule_snapshot"}

app = FastAPI(title="Clutch", description="The Last-Minute Life Saver")

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
def root():
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
        raise HTTPException(
            status_code=400,
            detail="OAuth state unrecognized or expired. Start again at /login.",
        )

    flow = _build_flow(state=state)
    flow.code_verifier = code_verifier
    # Exchange the authorization code (carried on the callback URL) for tokens.
    flow.fetch_token(authorization_response=str(request.url))
    creds = flow.credentials

    # Identify the user (sub/email/name) using the fresh access token.
    info = AuthorizedSession(creds).get(USERINFO_URL).json()
    sub = info.get("sub")
    if not sub:
        raise HTTPException(status_code=502, detail="Could not read Google user id.")

    save_user_tokens(sub, info.get("email"), info.get("name"), creds)

    resp = RedirectResponse("/me")
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


def build_snapshot(sub: str, data: dict, hours: int) -> dict:
    """get_schedule_snapshot: perceive calendar (events + free/busy) + tasks.

    Read-only. Returns calendar/task data only — never token values.
    """
    creds = _ensure_valid(sub, _credentials_from_doc(data))
    session = AuthorizedSession(creds)

    now = datetime.now(timezone.utc)
    end = now + timedelta(hours=hours)
    time_min, time_max = now.isoformat(), end.isoformat()

    events = _list_events(session, time_min, time_max)
    busy = _freebusy(session, time_min, time_max)
    tasks = load_tasks(sub)

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
        description="Compose a heads-up draft message (Lane B; never sent).",
        parameters_json_schema={
            "type": "object",
            "properties": {
                "recipient": {"type": "string"},
                "body": {"type": "string"},
            },
            "required": ["body"],
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
    "finishes their work before its deadline by planning concrete actions.\n"
    "Process: (1) Call get_schedule_snapshot first to perceive the calendar and "
    "tasks. (2) Reason about time remaining vs. work remaining. (3) Propose "
    "actions via function calls: break the goal into subtasks, book focus blocks "
    "in free time before the deadline, reschedule conflicts, and re-prioritize. "
    "Prefer concrete times that fit the user's actual free/busy. When the plan is "
    "complete, stop calling functions and reply with a one-paragraph summary of "
    "what you propose and why."
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
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise HTTPException(
            status_code=500, detail="Missing GEMINI_API_KEY. Set it in your .env file."
        )
    return genai.Client(
        api_key=key, http_options=types.HttpOptions(timeout=GEMINI_TIMEOUT_MS)
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


def run_agent(sub: str, data: dict, goal: str | None, deadline: str | None, hours: int) -> dict:
    """Read/propose agent loop: perceive -> pre-rank -> plan with Gemini.

    EXECUTES only get_schedule_snapshot (read-only). All write tool calls are
    captured as proposals and NOT executed in this build.
    """
    client = _gemini_client()
    config = types.GenerateContentConfig(
        tools=[types.Tool(function_declarations=FUNCTION_DECLARATIONS)],
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        system_instruction=SYSTEM_PROMPT,
        temperature=0.2,
    )

    # 1) Perceive deterministically, then pre-rank (auditable).
    perceived = build_snapshot(sub, data, hours)
    ranking = rank_tasks(perceived["tasks"])
    executed = [{"name": "get_schedule_snapshot", "args": {"hours": hours}, "trigger": "initial perceive"}]
    last_snapshot = perceived

    # 2) Seed the conversation with the goal + snapshot + ranking.
    user_prompt = (
        f"Goal: {goal or '(no specific goal — review my schedule and propose improvements)'}\n"
        f"Deadline: {deadline or '(unspecified)'}\n\n"
        f"Current schedule snapshot (window {perceived['window']['start']} → "
        f"{perceived['window']['end']}):\n"
        f"- events: {perceived['events']}\n"
        f"- busy: {perceived['busy']}\n"
        f"- existing subtasks (deterministically pre-ranked): {ranking}\n\n"
        "Propose the plan as function calls."
    )
    contents = [types.Content(role="user", parts=[types.Part.from_text(text=user_prompt)])]

    proposed: list[dict] = []
    steps_log: list[dict] = []
    final_text = None

    # 3) Plan/observe loop, hard-capped.
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
            args = dict(fc.args) if fc.args else {}
            if fc.name in EXECUTABLE_TOOLS:  # read-only execute
                last_snapshot = build_snapshot(sub, data, hours)
                executed.append({"name": fc.name, "args": args, "trigger": "model-requested"})
                tool_parts.append(
                    types.Part.from_function_response(name=fc.name, response={"snapshot": last_snapshot})
                )
            else:  # write/draft -> capture as proposal, DO NOT execute
                proposed.append({"name": fc.name, "args": args})
                tool_parts.append(
                    types.Part.from_function_response(
                        name=fc.name,
                        response={"status": "proposed_not_executed",
                                  "note": "dry-run: write actions are not executed in this build"},
                    )
                )
        contents.append(types.Content(role="tool", parts=tool_parts))
    else:
        final_text = f"Step cap ({AGENT_STEP_CAP}) reached before the model finished."

    return {
        "goal": goal,
        "deadline": deadline,
        "models": {"primary": PRIMARY_MODEL, "fallback": FALLBACK_MODEL},
        "steps": steps_log,
        "snapshot": last_snapshot,
        "ranking": ranking,
        "executed_read_only": executed,
        "proposed_writes_not_executed": proposed,
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


@app.post("/agent/plan")
def agent_plan(body: PlanRequest, request: Request):
    """Plan for a specific goal+deadline. Read/propose only (no writes)."""
    if body.hours < 1 or body.hours > 24 * 30:
        raise HTTPException(status_code=400, detail="hours must be between 1 and 720.")
    sub, data = _require_user(request)
    return run_agent(sub, data, body.goal, body.deadline, body.hours)


@app.get("/agent/run")
def agent_run(request: Request, hours: int = 48, goal: str | None = None):
    """Review the schedule (optionally for a goal) and propose. Read/propose only."""
    if hours < 1 or hours > 24 * 30:
        raise HTTPException(status_code=400, detail="hours must be between 1 and 720.")
    sub, data = _require_user(request)
    return run_agent(sub, data, goal, None, hours)
