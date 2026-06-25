"""Clutch — FastAPI app.

Build step 2: direct Google OAuth 2.0 (NOT Firebase Auth, per design.md §5).
Proves the login + Calendar-scope consent flow end to end.

Scope of THIS step: login only. Token storage is a local JSON file just to prove
the flow works; it moves to Firestore in the next step. No Firestore / Gemini /
agent loop here yet.
"""

import json
import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from google.auth.transport.requests import AuthorizedSession
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow

load_dotenv()

# --- OAuth configuration -----------------------------------------------------

# Minimum scopes per design.md / CLAUDE.md: calendar.events for the action
# surface, plus openid/email/profile for identity. NEVER a Gmail-send scope.
SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/calendar.events",
]

# Read secrets from the environment — never hardcode them.
CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")
REDIRECT_URI = os.environ.get(
    "OAUTH_REDIRECT_URI", "http://localhost:8080/oauth2callback"
)

# Google may return scopes in a different order / add `openid`; relax the check
# so the token exchange doesn't raise a spurious "scope has changed" warning.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

# google-auth-oauthlib refuses plain-HTTP redirects unless told otherwise. The
# localhost loopback redirect used for LOCAL testing is http, so allow it only
# in that case. (On Cloud Run the redirect is https and this stays off.)
if REDIRECT_URI.startswith("http://"):
    os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")

# Local token store — gitignored, replaced by Firestore next step.
TOKEN_FILE = Path(__file__).parent / ".tokens.json"

# In-flight OAuth handshakes: maps the OAuth `state` -> PKCE code_verifier.
# Google echoes `state` back on the callback URL, so we key on it instead of a
# session cookie (which the cross-site redirect from Google doesn't carry).
# Single-process local dev only; moves to Firestore with the rest of the token
# state in the next step.
_pending_verifiers: dict[str, str] = {}

app = FastAPI(title="Clutch", description="The Last-Minute Life Saver")


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


def _save_credentials(creds: Credentials) -> None:
    TOKEN_FILE.write_text(
        json.dumps(
            {
                "token": creds.token,
                "refresh_token": creds.refresh_token,
                "token_uri": creds.token_uri,
                "client_id": creds.client_id,
                "client_secret": creds.client_secret,
                "scopes": creds.scopes,
            },
            indent=2,
        )
    )


def _load_credentials() -> Credentials | None:
    if not TOKEN_FILE.exists():
        return None
    data = json.loads(TOKEN_FILE.read_text())
    return Credentials(**data)


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
    # Keep the auto-generated PKCE code_verifier, keyed by the state Google will
    # echo back, so the callback can reuse the exact same Flow config.
    _pending_verifiers[state] = flow.code_verifier
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
    # Pop (one-time use) the verifier saved during /login for this state.
    code_verifier = _pending_verifiers.pop(state, None) if state else None
    if not state or not code_verifier:
        raise HTTPException(
            status_code=400,
            detail="OAuth state unrecognized or expired. Start again at /login.",
        )

    flow = _build_flow(state=state)
    # Restore the PKCE verifier from /login — required for the token exchange.
    flow.code_verifier = code_verifier
    # Exchange the authorization code (carried on the callback URL) for tokens.
    flow.fetch_token(authorization_response=str(request.url))
    creds = flow.credentials
    _save_credentials(creds)

    return RedirectResponse("/me")


@app.get("/me")
def me():
    """Confirm login status and show the connected Google identity."""
    creds = _load_credentials()
    if creds is None:
        return JSONResponse(
            {"logged_in": False, "hint": "Visit /login to connect your Google account."}
        )

    # Use the access token to read the user's basic profile.
    session = AuthorizedSession(creds)
    resp = session.get("https://www.googleapis.com/oauth2/v3/userinfo")
    if resp.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"Failed to fetch userinfo ({resp.status_code}): {resp.text}",
        )
    info = resp.json()

    return {
        "logged_in": True,
        "email": info.get("email"),
        "name": info.get("name"),
        "has_refresh_token": bool(creds.refresh_token),
        "scopes": creds.scopes,
    }
