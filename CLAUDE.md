# Project: Kairos — The Last-Minute Life Saver (Vibe2Ship hackathon)

Goal: build and ship a working AI productivity agent, with design.md as the
living source-of-truth spec.

Problem statement: "The Last-Minute Life Saver" — an AI productivity companion
that PROACTIVELY plans, prioritizes, and AUTONOMOUSLY acts to help users finish
tasks before deadlines (NOT passive reminders).

## Build & deploy constraints
- Local development; deployed via `gcloud`.
- Deploy requirement: the solution must be **deployed on Google Cloud Platform**
  (Cloud Run). AI Studio is optional, not mandatory — organizer confirmed manual
  deploy is acceptable as long as it lands on GCP. (Keep this clarification IN
  WRITING before final submit.)
- Open-source libraries allowed with proper credit.
- We wire up auth / OAuth scopes / Firestore / Cloud Scheduler ourselves in code.

## Stack decisions (committed)
- Backend: Python + FastAPI on Cloud Run.
- State + task store: **Firestore** (single source of truth for tasks, plan
  ledger, action log, prefs, cursor, OAuth tokens).
- Auth: **direct Google OAuth 2.0** (google-auth / google-auth-oauthlib), refresh
  tokens stored in Firestore. NOT Firebase Auth — we need durable server-side
  tokens for background (Cloud Scheduler) agent runs, which is OAuth 2.0's strength.
- LLM: Gemini via **Vertex AI over Application Default Credentials** (NOT an AI
  Studio API key) — bills the GCP project's credit. Primary `gemini-2.5-flash`,
  fallback `gemini-2.5-flash-lite` (retry-once-then-failover). Region via
  `VERTEX_LOCATION` (default `global`). Function calling. `GEMINI_API_KEY` is no
  longer the default path.

## Tooling decisions (and why) — do not re-litigate
- **Google Tasks dropped as a dependency.** Its API `due` is date-only and it
  duplicates Calendar + Firestore. Subtasks live in Firestore instead (real
  timestamps, full schema control). Optional one-way mirror into Google Tasks is
  a STRETCH nicety only, not MVP.
- **Google Calendar API stays** — it is the product's action surface ("the
  save"). Highest-friction tool but irreplaceable; pay the OAuth cost.
- Cloud Run, Firestore, Gemini, Cloud Scheduler all kept (load-bearing).
- **Google Maps platform** added (all built): **Routes API** (commute, server-side
  key), **Geocoding API** (reverse-geocode picked pins to SHORT labels, server-side
  key), **Places API (New)** (demo nearby venues, server-side key), and **Maps
  JavaScript API** (goal-location pin picker, BROWSER key). The server-side
  `MAPS_API_KEY` (Routes + Geocoding + Places) is NEVER exposed to the browser; the
  picker uses a SEPARATE referrer-restricted `MAPS_BROWSER_KEY` and hides if unset.
  **Web Speech API** (browser) added for voice input.

## Implemented so far
- Cloud Run deploy path proven.
- Direct Google OAuth 2.0 login (`/login`, `/oauth2callback`, `/me`); PKCE
  verifier keyed by OAuth `state` (works across instances).
- Firestore persistence (ADC, no key file): `users/{sub}` (tokens + `prefs`),
  `oauth_states`, subcollections `tasks` + `action_log`. Tokens auto-refresh and
  write back.
- Read path `/snapshot` (`get_schedule_snapshot`): Calendar events + free/busy + tasks.
- Gemini function-calling agent loop (`/agent/run`, `/agent/plan`) on **Vertex AI**;
  deterministic pre-ranker; step cap 24.
- Lane A executed (create/reschedule events; `break_down_task`/`upsert_task`/
  `reprioritize` → Firestore). `notify_user` surfaces deadline-feasibility warnings.
- Lane B IMPLEMENTED (`draft_message`, confirm-first): on deadline-infeasibility
  the agent drafts a context-aware rescue/extension message (Gemini), persists it
  to a Firestore `drafts` subcollection, and exposes `GET /agent/drafts` +
  `POST /agent/draft/{id}/confirm|edit|dismiss`. Confirm only MARKS it approved
  (`sent=false`); edit reopens to `proposed`; dismiss marks dismissed. There is
  NO send capability and NO Gmail scope — by design. Drafts are tied to their task
  (captured with the history entry, shown in task detail); deleting a task clears
  its UNCONFIRMED drafts from the main area; APPROVED drafts persist.
- Action log + single undo (`/agent/undo`, `/agent/undo/{id}`), per-block dismiss
  (`/agent/undo-event/{id}`), and bulk `/agent/undo-all` (delete ONLY
  `clutch`-marked events — never the user's own).
- Guards: deterministic working-hours enforcement (08:00–22:00 Asia/Kolkata
  default, per-user prefs) + max 2h block; hard caps `MAX_EVENTS_PER_RUN=8`
  (halts the run) and `AGENT_MAX_ACTIONS=20` (step cap 24); subtask due-dates
  normalized to (now, deadline].
- Humane scheduling (prompt-steered): when the deadline has runway the agent
  spreads focus blocks across days with comfortable breaks (~30-min gaps + a
  midday meal break); when the deadline is tight it packs densely. Fitting the
  whole effort before the deadline always takes priority over break length.
- Voice input (frontend, Web Speech API): goal/deadline dictation with best-effort
  natural-language deadline parsing; graceful fallback to typing when unsupported.
- Google Maps commute times (Routes API): `POST /commute` computes drive time from
  the user's location to a located event; the Maps key is server-side only and
  never reaches the browser. Travel time renders inline on located events.
- Demo / guest mode (parallel path; live agent untouched): real agent reasoning on
  a seeded in-memory sample calendar, no OAuth/Firestore/real writes. Seed events are
  PROFESSIONAL (meetings/calls/reviews) placed at REAL venues near the viewer via the
  Places API, with commute from the viewer's location. GUARANTEED FALLBACK to a fixed
  Bhubaneswar professional seed + fixed origin if geolocation/Places/key is missing —
  the demo always populates, never errors. `GET /agent/demo/seed` (optional viewer
  `lat`/`lng`), `POST /agent/demo/run` (optional `viewer_lat`/`viewer_lng`).
- Main-page schedule preview: a compact "Upcoming events" list (next 2–3 events)
  with a "View full schedule" link to the complete timeline; works in both the
  real app and demo.
- Task history: persisted (Firestore `task_history` subcollection in real mode;
  in-memory session-only in demo). List page grouped by creation date, newest/oldest
  sort + goal search; per-task delete (×) and clear-all (history-only — calendar
  blocks STAY); task-detail page with scoped undo-all + per-block delete.
- Reusable confirm modal: undo-all, per-block dismiss, delete-task, and clear-history
  all route through one confirm popup (Yes / No / × close; backdrop + Esc cancel) —
  no inline confirmations.
- Optional per-goal location (additive; DISPLAY METADATA ONLY — never affects
  scheduling, subtasks, caps, working hours, or the clutch/undo logic): interactive
  Maps JavaScript pin picker (with a current-location marker), reverse-geocoded SHORT
  label (Geocoding API), commute from the origin captured at goal-creation (Routes
  API), and a keyless "See route" Google Maps directions link. Stamped on created
  events via `clutch_loc_*`/`clutch_origin_*` private props. Endpoints `GET
  /maps/config`, `POST /geocode/reverse`. No location ⇒ exactly the prior behavior.
- Parallelized calendar inserts: focus-block `events.insert` run concurrently for
  faster booking; agent reasoning, ordering, and per-run caps unchanged.
- Responsive mobile layout (CSS-only): stacks layouts, reflows header/cards, fits
  modals + map picker to the viewport, no horizontal overflow. No JS/logic changes.
- Known imperfection: block sizing is effort-PROMPT-steered, not deterministically
  minute-accounted; the hard guarantee is the ≤8-events/run cap.

## Rubric (design every decision against this)
Problem Solving & Impact 20, Agentic Depth 20, Innovation 20,
Usage of Google Technologies 15, Product Experience 10, Tech Implementation 10,
Completeness 5.
"Usage of Google Technologies" rewards correct, substantive use — not tool count.

## Hard rules
- Deadline June 29, 2:00 PM. MVP spine FIRST, demo-proven (~June 27), THEN stretch.
- Stretch status: voice input and Maps commute are DONE (see "Implemented so
  far"); Cloud Scheduler background cron remains the one outstanding stretch item.
- Minimum OAuth scopes only: `calendar.events` + `calendar.freebusy` (verified:
  `freebusy.query` is NOT authorized by `calendar.events`), plus
  `openid`/`email`/`profile` for identity; add `tasks` only for the optional S4
  mirror. NEVER request a Gmail-send scope.
- Do NOT invent API capabilities — if uncertain, say so and verify before relying.

## Verified constraints to respect
- Google Tasks `due` is DATE-ONLY (only relevant if we add the optional mirror).
- Cloud Run is stateless/ephemeral across cold starts — Firestore state is mandatory.

Truthfulness rule: mark any unverified technical claim rather than asserting it.
