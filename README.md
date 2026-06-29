# Kairos — The Last-Minute Life Saver

*Kairos (καιρός) — the ancient Greek word for the **decisive, opportune moment to act**.*

An autonomous AI productivity agent that doesn't just remind you about deadlines — it does the planning work you have no time for: it breaks your goal into steps, weighs the time you have against the work left, and **books focus blocks on your real Google Calendar** so the work actually fits before the deadline.

**🔗 Live demo:** https://clutch-477737446663.asia-south1.run.app

---

## The problem

People miss deadlines not because they forget them, but because they fail to *act in time*. They don't break big work into doable steps, don't reserve time for it, and don't re-plan when life collides with the plan. Reminders, to-do lists, and calendars are all **passive** — they surface information and then wait for an already-overloaded human to do the actual scheduling. The moment of failure is exactly the moment you're too buried to manage your own schedule.

## What Kairos does

Kairos is a **proactive, autonomous agent**. You give it a goal and a deadline ("finish the research report by Friday 5pm"); it then:

1. **Perceives** your real Google Calendar — events plus free/busy windows.
2. **Decomposes** the goal into granular, time-estimated subtasks (via Gemini).
3. **Weighs** time-remaining against work-remaining for each task.
4. **Autonomously books** focus blocks into your open time so the work fits before the deadline — it *books*, it doesn't merely suggest.
5. If a deadline is **genuinely infeasible**, it **drafts** a context-aware heads-up / extension message for you to review — it never sends it. There is no Gmail send scope by design; you copy and send it yourself.

Every autonomous change comes with a one-line receipt and an undo.

---

## Key features

- **Agentic loop** — a bounded perceive → prioritize → plan → act → observe loop. A deterministic pre-ranker scores tasks by deadline pressure and effort; Gemini (function-calling) decides what to do; the loop executes and observes the result.
- **Lane A — autonomous calendar writes.** Reads free/busy, breaks the goal into varied granular subtasks, and creates focus blocks on your calendar without asking — these are reversible actions on your own planning surface.
- **Humane scheduling.** When the deadline has runway, Kairos spreads focus blocks across multiple days with comfortable breaks (≈30-minute gaps and a midday meal break) instead of cramming; when the deadline is tight it packs densely to fit the work. Fitting the whole effort before the deadline always takes priority over break length.
- **Safety guards built in:**
  - **Deletes only Kairos-marked events** — undo and undo-all re-check a server-side marker per event, so your own calendar entries are never touched.
  - **Single undo, bulk undo-all, and per-block dismiss** for fine-grained control.
  - **Working-hours enforcement** — blocks land only between 08:00–22:00 (Asia/Kolkata default, per-user prefs), with a max block size.
  - **Runaway cap** — a hard ceiling of **8 events per run** that halts the agent, plus an action budget.
  - **Due-date normalization** — subtask deadlines are clamped to the `(now, deadline]` window.
- **Lane B — confirm-first rescue drafts, tied to their task.** When the deadline truly can't be met, Kairos drafts an extension/heads-up message and saves it for you to **confirm / edit / dismiss**. Confirming only *marks it approved* — **no send capability and no Gmail scope**, by design. You copy and send it yourself. Each task's drafts are captured with its history entry and shown in that task's detail view; deleting a task clears its unconfirmed drafts from the main area, while approved drafts persist on the **Approved** page.
- **Task history.** Past runs are saved (Firestore in the live app; session-only in demo) and listed on a **Task History** page grouped by creation date, with newest/oldest sort and goal search. Each entry has a per-task delete (×) and there's a clear-all — both remove the task **from history only; your calendar blocks stay**. Open a task to see its focus blocks with a scoped **undo-all** and per-block delete.
- **Optional goal location.** Each goal can carry an optional location, set with an interactive **Google Maps** pin picker (which also shows a "you are here" marker for your current location). Blocks for a located goal display a short place label, the **commute time** from the origin captured when the goal was created, and a **"See route"** button that opens Google Maps directions. Location is **display metadata only — it does not affect scheduling, subtasks, or any caps**, and it's entirely optional (no location ⇒ blocks render exactly as before).
- **Demo / guest mode.** Judges and first-time visitors can run the **real agent reasoning** against a **seeded sample calendar** with **no login** — no OAuth, no real calendar writes. The seeded week shows professional sample events (meetings, calls, reviews) placed at **real venues near you** via the **Google Places API**, with commute times from your location; the agent's actual Gemini planning then adds focus blocks. If geolocation, Places, or the Maps key is unavailable, it **falls back** to a fixed Bhubaneswar professional seed so the demo always populates. Results are sandboxed in-memory.
- **Schedule preview on the main page.** The home screen shows a compact **"Upcoming events"** list (the next 2–3 events) with a **"View full schedule"** link to the complete timeline (date-range pickers, all events) — in both the real app and demo.
- **Voice input.** Dictate your goal (and deadline) by voice using the browser's **Web Speech API** — speech is transcribed straight into the goal field, with best-effort natural-language deadline parsing. Degrades gracefully to typing where speech isn't supported.
- **Commute times.** For calendar events that have a location, Kairos shows the live driving time to get there via the **Google Maps Routes API** (the API key stays server-side and is never exposed to the browser).
- **Consistent confirm dialogs.** Destructive actions — undo-all, per-block dismiss, delete-task, and clear-history — all use a single reusable confirm popup (Yes / No / × close, dismissable by backdrop click or Esc) instead of inline confirmations.
- **Faster booking.** Focus-block inserts to Google Calendar run **concurrently**, so booking a multi-block plan is quicker. The agent's reasoning and the per-run caps are unchanged.
- **Responsive layout.** The UI adapts to phone-width screens — layouts stack, the header and cards reflow, modals and the map picker fit the viewport, with no sideways scrolling.
- **Light / dark themes** — a warm "summer" light theme and a full dark theme.

---

## How it works

**Perceive** — The agent loads world state: Google Calendar events and free/busy, plus the subtask list from Firestore. It computes, per task, *time remaining vs. work remaining*.

**Plan** — A deterministic pre-ranker scores open tasks (`urgency ≈ f(deadline proximity, effort)`) so prioritization is auditable. That ranked context is handed to **Gemini with the full tool schema attached**; Gemini responds with **function calls**, not prose.

**Act** — The returned calls are executed server-side and split into two risk lanes: **Lane A** (reversible writes on the user's own surface — create/reschedule blocks, decompose tasks) runs automatically; **Lane B** (anything outbound — the rescue message) is drafted and held for explicit confirmation. Each action is logged with reversal info to power undo and the one-line receipt.

This Lane A / Lane B split is the core of the agent's judgment: it acts on its own where actions are safe and reversible, and asks first where they aren't.

---

## Tech stack & Google technologies

Kairos is a single **FastAPI (Python)** service that serves a single-page frontend *and* the agent endpoints. Google technologies are load-bearing throughout:

| Technology | Role in Kairos |
|---|---|
| **Google Gemini via Vertex AI** | The agent's brain — plans, prioritizes, emits structured function calls, and composes the Lane B rescue draft. Accessed through **Vertex AI over Application Default Credentials** (bills the GCP project), `gemini-2.5-flash` primary with `gemini-2.5-flash-lite` fallback. Not an AI Studio API key. |
| **Google Calendar API** | The action surface — `freebusy.query` to find open time, `events.insert` to book focus blocks, `events.patch` to rebook, `events.delete` for undo. |
| **Firestore** | Single source of truth — subtasks, plan/action log, user prefs, and OAuth refresh tokens. Used directly over ADC. |
| **Cloud Run** | Hosts the FastAPI app + agent loop. The GCP deploy target. |
| **Google OAuth 2.0** | Direct OAuth (google-auth / google-auth-oauthlib, PKCE) for Calendar access. Refresh tokens stored in Firestore for durable, server-side agent runs. Minimum scopes only: `calendar.events` + `calendar.freebusy` + `openid`/`email`/`profile`. **Never a Gmail-send scope.** |
| **Google Maps Routes API** | Computes live driving time from the user's location to located events. Called server-side via a dedicated `/commute` endpoint so the server-side Maps key never reaches the browser; the front end renders the returned travel time inline. |
| **Google Maps JavaScript API** | Powers the interactive pin picker for the optional goal location (drop/drag a destination pin, with a current-location marker). Loaded in the browser with a **separate, referrer-restricted** browser key — never the server-side key. If that key is unset, the picker simply hides. |
| **Google Geocoding API** | Reverse-geocodes a picked pin into a **short place label** (area/place name, not the full address) for display on blocks. Called server-side with the server-side Maps key. |
| **Google Places API (New)** | Finds **real venues near the viewer** to populate the demo's sample calendar (`places:searchNearby`). Called server-side with the server-side Maps key; falls back to a fixed Bhubaneswar seed on any failure. |

Frontend: a single static `index.html` (vanilla HTML/CSS/JS) served directly by FastAPI. Voice input uses the browser-native **Web Speech API** (no key, no external service).

---

## Architecture

A single **Cloud Run** service runs the FastAPI app, which serves both the SPA and the agent API endpoints. The agent loop calls **Gemini (via Vertex AI)** for planning and executes the returned tool calls against the **Google Calendar API** (with the user's OAuth credentials) and **Firestore** (state + tokens). On Cloud Run, Vertex AI and Firestore are reached via Application Default Credentials / the service account; locally, via `gcloud auth application-default login`. Cloud Run is stateless across cold starts, so Firestore holds all durable state.

```
                 ┌─────────────── Cloud Run service ───────────────┐
                 │                FastAPI app                       │
   Browser ────▶ │  ┌──────────┐  serves SPA + /agent/* endpoints   │
   (SPA)         │  │  Agent   │── plan ──▶  Gemini (Vertex AI,      │
                 │  │  loop    │◀─ calls ──   function-calling)      │
                 │  │ perceive→│                                     │
                 │  │ plan→act │── execute ──▶ Google Calendar API   │
                 │  │ →observe │── state ────▶ Firestore (tasks,     │
                 │  └──────────┘                log, prefs, tokens)  │
                 └─────────────────────────────────────────────────┘
```

---

## Running locally

**Prerequisites**
- Python 3.11+ and `pip`
- A Google Cloud project with **Firestore** and the **Vertex AI** and **Google Calendar** APIs enabled
- *(Optional, for commute + location + demo-nearby features)* on the **server-side** Maps key, enable the **Routes API**, **Geocoding API**, and **Places API (New)**; for the location pin picker, a **separate, referrer-restricted** browser key with the **Maps JavaScript API** enabled
- The [`gcloud` CLI](https://cloud.google.com/sdk/docs/install)
- An OAuth 2.0 **Web application** client ID (Google Cloud Console → APIs & Services → Credentials)

**1. Install dependencies**
```bash
pip install -r requirements.txt
```

**2. Configure environment**
```bash
cp .env.example .env
```
Fill in `.env` with your real values:

| Variable | Purpose |
|---|---|
| `GOOGLE_CLIENT_ID` | OAuth 2.0 client ID (`...apps.googleusercontent.com`) |
| `GOOGLE_CLIENT_SECRET` | OAuth 2.0 client secret |
| `OAUTH_REDIRECT_URI` | Must exactly match an authorized redirect URI on the OAuth client (local default: `http://localhost:8080/oauth2callback`) |
| `GOOGLE_CLOUD_PROJECT` | GCP project that owns Firestore and bills Vertex AI |
| `VERTEX_LOCATION` | Vertex AI region for Gemini (default `global`) |
| `MAPS_API_KEY` | *(Optional)* **Server-side only** Maps key — used for the Routes API (commute), Geocoding API (reverse-geocode short labels), and Places API (New) (demo nearby venues). Never sent to the browser. Omit to disable those features. |
| `MAPS_BROWSER_KEY` | *(Optional)* **Separate, referrer-restricted** browser key with the **Maps JavaScript API** enabled — exposed to the browser only for the goal-location pin picker. If unset, the location picker simply hides; everything else works unchanged. Do **not** reuse the server-side `MAPS_API_KEY` here. |

`.env` is gitignored — never commit real secrets.

**3. Authenticate Application Default Credentials** (so Firestore and Vertex AI work without a key file):
```bash
gcloud auth application-default login
```

**4. Run the app**
```bash
uvicorn main:app --host 0.0.0.0 --port 8080
```
Then open http://localhost:8080.

---

## Demo access (for judges)

The OAuth consent screen is in **testing mode** — Google requires verification for sensitive Calendar scopes, which takes longer than the hackathon window. So **live sign-in with your own Google account is limited to approved test users.** Two ways to try it:

- **No-login DEMO mode (recommended):** click **"Try the demo"** on the landing page. The real agent reasoning runs against a seeded sample calendar — no login, no real calendar writes.
- **Use your own calendar:** email the developer at **soumyanayak123456789@gmail.com** to be added as an OAuth test user, then sign in normally.

---

## Known limitations & future work

Reported honestly:

- **Same-day packing.** Focus blocks can cluster into a single day; the agent steers block sizing by effort prompt rather than inserting breaks or load-balancing across multiple days. **Planned:** deterministic break-insertion and multi-day load balancing.
- **Block sizing is effort-prompt-steered, not minute-accounted.** The hard guarantee is the ≤8-events-per-run cap; exact total-minutes ≈ sum-of-efforts is not enforced.
- **Planned ops:** GitHub-triggered continuous deployment to Cloud Run.
