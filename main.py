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

import os
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from google.auth.transport.requests import AuthorizedSession
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.cloud import firestore
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow

load_dotenv()

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


@app.get("/snapshot")
def snapshot(request: Request, hours: int = 48):
    """get_schedule_snapshot: perceive calendar (events + free/busy) + tasks.

    Read-only. Returns calendar/task data only — never token values.
    """
    if hours < 1 or hours > 24 * 30:
        raise HTTPException(status_code=400, detail="hours must be between 1 and 720.")

    sub = request.cookies.get(UID_COOKIE)
    data = load_user(sub) if sub else None
    if not data:
        raise HTTPException(status_code=401, detail="Not logged in. Visit /login first.")

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
