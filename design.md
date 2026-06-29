# Kairos — The Last-Minute Life Saver

*An AI productivity companion that proactively plans, prioritizes, and autonomously acts so you finish before the deadline — not a reminder app.*

> **Design provenance:** synthesized by the team lead from three specialist reviews. Technical claims marked **CONFIRMED** or **UNVERIFIED** per the project truthfulness rule.
>
> **Build/deploy:** Per organizer clarification, **Google AI Studio is NOT mandatory**; the only deploy requirement is **Google Cloud Platform (Cloud Run)**. Deployed via `gcloud`. *(Keep the organizer clarification in writing before the irreversible final submit.)*
>
> **Tooling revision (this version):** Two practicality-driven changes. (1) **Google Tasks dropped as a core dependency** — its date-only API forced a workaround and duplicated Calendar + Firestore; subtasks now live in **Firestore**. Mirroring into the real Google Tasks app is an optional stretch nicety. (2) **Auth committed to direct Google OAuth 2.0** (not Firebase Auth) because the agent must act on Calendar in the background (Cloud Scheduler), which needs durable server-side refresh tokens. Maps and voice remain wanted features, sequenced as stretch (see §8).
>
> **Build update (as implemented):** Gemini is accessed via **Vertex AI over Application Default Credentials** (project `project-2fa6ca07-83db-4010-9a4`; region via `VERTEX_LOCATION`, default `global`) — **not** an AI Studio API key — so usage bills the GCP project's credit. Models: `gemini-2.5-flash` primary, `gemini-2.5-flash-lite` fallback (retry-once-then-failover). Lane A actions execute with an action log, single **undo**, and bulk **undo-all** (clutch-marked events only). Deterministic **working-hours enforcement** (08:00–22:00 Asia/Kolkata, per-user prefs in Firestore) + max 2h block, hard per-run caps (**≤8 events — halts the run**; `AGENT_MAX_ACTIONS=20`, step cap 24), and subtask **due-date normalization** to (now, deadline] are all in place.
>
> **Post-MVP additions (this version):** persisted **task history** (Firestore in the live app; session-only in demo) with a list page (grouped by creation date, newest/oldest sort, goal search), per-task delete + clear-all (history-only; calendar blocks stay), and a task-detail view with scoped undo-all + per-block delete; **rescue drafts tied to their task** (shown in detail; a task's unconfirmed drafts clear when the task is deleted; approved drafts persist); a **reusable confirm modal** for all destructive actions; an **optional per-goal location** (interactive Google Maps pin picker with a current-location marker; blocks show a short place label + commute + a "See route" button) that is **display metadata only and never affects scheduling**; a **demo seed of professional events at real nearby venues** via the Places API (with a guaranteed fixed-Bhubaneswar fallback); **parallelized calendar inserts** (faster booking, reasoning unchanged); and a **responsive mobile layout**. New Google APIs used: **Maps JavaScript API** (picker), **Geocoding API** (short labels), **Places API (New)** (demo nearby). The server-side `MAPS_API_KEY` (Routes + Geocoding + Places) is never exposed; the picker uses a **separate referrer-restricted `MAPS_BROWSER_KEY`** and hides if it's unset.

---

## 1. Problem statement & target user

**Problem.** People miss deadlines not because they forget them, but because they fail to *act in time*: they don't break big work into doable steps, don't reserve time for it, and don't re-plan when life collides with the plan. Existing tools (reminders, to-do lists, calendars) are **passive** — they surface information and wait for the human to do all the work. The moment of failure is precisely the moment the user is too overloaded to manage their own schedule.

**Target user.** A busy student or early-career knowledge worker juggling 5–15 concurrent obligations with hard deadlines, who already lives in Google Calendar and routinely scrambles at the last minute.

**Insight.** The leverage is not *better reminding* — it's **the app doing the planning work the user has no time to do**: decomposing, time-blocking, and rescuing the schedule when it breaks.

---

## 2. Solution overview (one paragraph)

Kairos is a proactive agent that takes a stated goal ("finish the slide deck by Friday 5pm"), uses Gemini to **break it into time-estimated subtasks** (stored in Firestore), reads your calendar's free/busy, and **autonomously books focus blocks** on your real Google Calendar to guarantee the work fits before the deadline. It runs on a schedule (and on key events), so when a new meeting collides with your plan or a task slips, it **re-plans and rebooks on its own**, then shows you a one-line receipt of what it changed with an undo. It acts by default on your own planning surface (reversible) and asks first only for anything outbound or irreversible. That re-plan-under-pressure behavior — "the save" — is the product's reason to exist and its core differentiator.

---

## 3. Core agentic behavior — how it plans, prioritizes, and executes autonomously

Highest-weighted scoring surface (Agentic Depth 20). The agent runs a **perceive → plan → prioritize → act → observe** loop, proactively.

### The loop (one "tick")
1. **Perceive** — Load world state: Google Calendar events + free/busy, the subtask list from Firestore, and the agent's own memory (prior plans, actions, learned preferences). Compute, per task, *time remaining vs. work remaining*.
2. **Prioritize** — A deterministic pre-ranker scores every open task: `urgency = f(deadline_proximity, estimated_effort, dependencies)`. The ranked list is fed to Gemini so priority is auditable.
3. **Plan** — A single Gemini call with the full function/tool schema attached, framing Gemini as a planner. Gemini responds with **function calls**, not prose.
4. **Act** — Execute the returned calls, split into two risk lanes (below).
5. **Observe** — Record what was done, the API results, and write back to memory. The next tick perceives the consequences.

### The decision rule for acting *without being asked*
- `deadline − now < buffer_for(estimated_effort)` **and** no calendar block exists → **create a focus block**.
- A scheduled block was missed or a new event collides with it → **reschedule it**.
Reversible and internal to the user's own planning surface, so they happen automatically; the agent then notifies with a receipt.

### Human-in-the-loop boundary
- **Lane A — auto-execute, no confirmation:** actions on the user's *own* reversible surface — `create_calendar_event`, `reschedule_event`, `break_down_task`, internal re-prioritization. Acts, then surfaces *"I moved your 3pm prep to 5pm because your dentist ran over — Undo."*
- **Lane B — propose-and-confirm (IMPLEMENTED for `draft_message`):** anything **outbound or irreversible**. On deadline-infeasibility the agent drafts a context-aware rescue/extension message (Gemini), persists it to Firestore (`drafts` subcollection), and surfaces it for the user to **confirm / edit / dismiss**. Confirm only **marks it approved (`sent=false`)** — there is **no send capability and no Gmail scope**, by design. (Event-deletion as a Lane B action is not yet built.)

This split is itself an Agentic-Depth argument: judgment about *when* autonomy is appropriate.

### Proactive trigger
- **Production:** **Cloud Scheduler** fires the agent on a cron (morning planning run + periodic checks). Buildable directly via `gcloud scheduler jobs create http` hitting an authenticated Cloud Run endpoint, using stored OAuth refresh tokens. True background proactivity is in scope.
- **Demo & event-driven:** a synchronous "Run agent now" trigger so "the save" is watchable live in 3 minutes — *inject a conflicting meeting → watch Kairos rebook.* Primary demo mechanism; needs only in-session tokens, so it de-risks the demo regardless of the background-token work.

---

## 4. Feature list

Autonomy-first. Buildability = realism for an app on Cloud Run.

| # | Feature | What the agent autonomously DOES | Rubric criterion | Google tech | Buildability |
|---|---------|----------------------------------|------------------|-------------|--------------|
| 1 | **Goal capture + auto-decompose** | Gemini breaks a goal into time-estimated subtasks and **writes them to Firestore** — no manual entry. | Agentic Depth, Problem Solving | Gemini function-calling | Medium |
| 2 | **Autonomous schedule placement** | Reads free/busy, finds open slots, **creates Calendar focus blocks**. Books, not suggests. | Agentic Depth (core), Innovation | Gemini FC, Calendar API (`freebusy.query` + `events.insert`) | Medium |
| 3 | **Re-plan on conflict — "the save"** | On collision/slip, **autonomously moves and rebooks** events and re-sequences subtasks, then reports. The money shot. | Agentic Depth, Innovation, Problem Solving | Calendar API (`events.patch`), Gemini loop | Medium–Hard |
| 4 | **Triage & prioritize** | Ranks open subtasks by deadline pressure + effort and **commits "what to do now."** | Agentic Depth, Product Experience | Gemini FC, Firestore | Easy–Medium |
| 5 | **Draft-the-rescue-message** *(Lane B, confirm)* — **IMPLEMENTED** | When a deadline can't be met, **drafts a context-aware heads-up / extension message** and saves it for the user to **confirm / edit / dismiss**. Draft-only; never sent. | Innovation, Product Experience | Gemini generation (no Gmail-send scope) | **IMPLEMENTED** |
| 6 | **Humane scheduling** — **IMPLEMENTED** | Spreads focus blocks across days with comfortable breaks when the deadline has runway; packs densely when it's tight. Fitting the work before the deadline always wins over break length. | Product Experience, Agentic Depth | Gemini loop (prompt-steered) | **IMPLEMENTED** |
| 7 | **Demo / guest mode** — **IMPLEMENTED** | Real agent reasoning on a seeded in-memory sample calendar with no login (no OAuth/Firestore/real writes). Seeded week shows professional events at **real venues near the viewer** (Places API) with commute from the viewer's location; the agent then adds focus blocks. Guaranteed fallback to a fixed Bhubaneswar professional seed if geolocation/Places/key is unavailable. | Product Experience, Innovation | Gemini via Vertex AI (sandboxed) + Places API | **IMPLEMENTED** |
| 8 | **Main-page schedule preview** — **IMPLEMENTED** | Compact "Upcoming events" list (next 2–3) with a "View full schedule" link to the complete timeline. | Product Experience | — (frontend) | **IMPLEMENTED** |
| 9 | **Task history** — **IMPLEMENTED** | Persists past runs (Firestore live; session-only in demo); list page grouped by creation date with newest/oldest sort + goal search; per-task delete + clear-all (history-only — calendar blocks stay); task-detail view of that run's blocks with scoped undo-all + per-block delete. Rescue drafts are tied to their task and shown in detail. | Product Experience, Completeness | Firestore | **IMPLEMENTED** |
| 10 | **Optional goal location** — **IMPLEMENTED** | Optional per-goal location via an interactive Google Maps pin picker (with a current-location marker); located blocks show a short place label, commute from the captured creation origin, and a "See route" button. **Display metadata only — does not affect scheduling, subtasks, or caps.** | Product Experience, Innovation, Google Tech | Maps JavaScript API (picker) + Geocoding API (short label) + Routes API (commute) | **IMPLEMENTED** |
| 11 | **Reusable confirm modal** — **IMPLEMENTED** | All destructive actions (undo-all, per-block dismiss, delete-task, clear-history) route through one confirm popup (Yes / No / × close; backdrop + Esc cancel). | Product Experience | — (frontend) | **IMPLEMENTED** |
| 12 | **Parallelized calendar writes** — **IMPLEMENTED** | Focus-block inserts run concurrently for faster booking; agent reasoning and per-run caps unchanged. | Tech Implementation | Calendar API (`events.insert`, concurrent) | **IMPLEMENTED** |
| 13 | **Responsive mobile layout** — **IMPLEMENTED** | UI adapts to phone widths — layouts stack, header/cards reflow, modals + map picker fit the viewport, no sideways scroll. | Product Experience | — (frontend/CSS) | **IMPLEMENTED** |
| S1 | **Cloud Scheduler background cron** *(stretch — outstanding)* | Agent runs unattended (morning planning run) with no user click. | Agentic Depth, Innovation | Cloud Scheduler + Cloud Run | Medium |
| S2 | **Voice goal/deadline capture** — **IMPLEMENTED** | Speak a goal (and deadline); transcribed into the goal field with best-effort natural-language deadline parsing; falls back to typing when unsupported. | Product Experience, Innovation | Web Speech API (browser, free) | **IMPLEMENTED** |
| S3 | **Commute times to located events** — **IMPLEMENTED** | Shows live drive time from the user's location to each located event (most visible in demo, with seeded Bhubaneswar locations). Key is server-side only. | Innovation, Google Tech | Maps Routes API (`/commute`, server-side key) | **IMPLEMENTED** |
| S4 | **Google Tasks mirror** *(optional nicety)* | One-way mirror of Firestore subtasks into the user's real Google Tasks app, so output shows up in a Google surface they already use. | Product Experience, Google Tech | Google Tasks API (`tasks.insert`) | Easy |

---

## 5. Architecture *(self-built, deployed to Cloud Run)*

```
                ┌──────────────────────────────────────────────┐
   Cloud         │            Cloud Run service (our app)        │
  Scheduler ───▶ │            (deployed via gcloud)             │
  (cron job) +   │                                              │
  in-app "Run    │   ┌────────────┐   plan    ┌──────────────┐  │
  now" / events  │   │  Agent     │──────────▶│   Gemini     │  │
                 │   │  loop      │◀──────────│  (function-   │  │
                 │   │ perceive→  │  function │   calling)    │  │
                 │   │ prioritize │   calls   └──────────────┘  │
                 │   │ →plan→act  │                              │
                 │   │ →observe   │── execute tool calls ──┐     │
                 │   └─────┬──────┘                        │     │
                 │         │ load/save state               ▼     │
                 │   ┌─────▼──────┐         ┌───────────────────┐│
                 │   │ Firestore  │         │ Google Calendar   ││
                 │   │ (TASKS,    │         │ API (OAuth 2.0    ││
                 │   │ plans,     │         │ user creds)       ││
                 │   │ action log,│         └───────────────────┘│
                 │   │ prefs,     │                              │
                 │   │ cursor,    │  Auth: direct Google OAuth 2.0│
                 │   │ OAuth tok) │  (google-auth / Authlib);     │
                 │   └────────────┘  refresh tokens in Firestore  │
                 └──────────────────────────────────────────────┘
```

**Identity & OAuth (committed: direct OAuth 2.0).** Use Google OAuth 2.0 via `google-auth` / `google-auth-oauthlib` (PKCE). The consent flow grants `calendar.events` **plus `calendar.freebusy`** — *verified*: `freebusy.query` is NOT authorized by `calendar.events` — and `openid`/`email`/`profile` for identity (and `tasks` only if the optional S4 mirror is built). **Refresh tokens are stored in Firestore** so the agent can make server-side Calendar calls during background (Cloud Scheduler) runs when the user isn't present — this is the capability Firebase Auth makes awkward and is why we don't use it. Request the **minimum scopes**; **never** request a Gmail-send scope (rescue message is draft-only).

**Agent loop.** A bounded single-agent function-calling loop (not a multi-agent ReAct planner — won't be demo-stable in the timeline): Gemini (**via Vertex AI over ADC**) decides → calls a function → loop observes → continues until done or the **step cap (24 turns)**, with hard per-run write caps (**≤8 calendar events — halts the run** — and `AGENT_MAX_ACTIONS=20`). Runs inside the Cloud Run FastAPI service.

**Gemini function/tool schema** (declared to Gemini; executed server-side):

| Function | Params | Effect | Dependency |
|----------|--------|--------|------------|
| `get_schedule_snapshot` | `window_start, window_end` | Perceive: read calendar free/busy + Firestore subtasks | Calendar `events.list`/`freebusy.query` + Firestore — **CONFIRMED** |
| `create_calendar_event` | `title, start, end, description` | Book a focus block (Lane A) | Calendar `events.insert` — **CONFIRMED** |
| `reschedule_event` | `event_id, new_start, new_end, reason` | Move a block (Lane A) | Calendar `events.patch` — **CONFIRMED** |
| `break_down_task` | `goal, deadline` → `subtasks[]` | Decompose goal; write subtasks to Firestore | Gemini + Firestore write — **CONFIRMED** |
| `upsert_task` | `task_id?, title, due, effort, status` | Create/update/complete a subtask in Firestore | Firestore write — **CONFIRMED** (real timestamps, no date-only limit) |
| `reprioritize` | `ranked_task_ids[]` | Commit new ordering / "now" pick in Firestore | Firestore write — **CONFIRMED** |
| `draft_message` | `recipient, goal, unmet_portion, new_eta, subject?, body?` | Draft a rescue/extension message on infeasibility; persist to Firestore `drafts` (Lane B, confirm-first, never sent) | Gemini generation + Firestore (no Gmail scope) — **IMPLEMENTED** |
| `notify_user` | `message, urgency` | Surface the agent's receipt/nudge | App-internal — **CONFIRMED** |

**Implemented guards (Lane A).** `create_calendar_event` / `reschedule_event` call the live Calendar API; `break_down_task` / `upsert_task` / `reprioritize` write to Firestore. Each executed action writes an `action_log` record with reversal info, enabling single **undo** (`/agent/undo`, `/agent/undo/{id}`) and bulk **undo-all** (`/agent/undo-all`), which deletes **only `clutch`-marked events** (server-side `privateExtendedProperty` filter, re-checked per event) so the user's own events are never touched. `create_calendar_event` is deterministically constrained to **working hours** (08:00–22:00 Asia/Kolkata default; per-user prefs in Firestore via `/prefs`) and a **max 2h block**; `break_down_task` subtask due-dates are normalized to (now, deadline]. `draft_message` (Lane B) now drafts a context-aware rescue/extension message on deadline-infeasibility and persists it to a Firestore `drafts` subcollection under a **confirm-first** flow (`GET /agent/drafts`; `POST /agent/draft/{id}/confirm|edit|dismiss`) — confirm only marks it approved (`sent=false`), with **no send capability and no Gmail scope**; `notify_user` surfaces deadline-feasibility warnings. Scheduling block-sizing is effort-PROMPT-steered (not minute-accounted) — the hard guarantee is the ≤8-events/run cap.

> **Why Firestore for tasks, not Google Tasks:** Google Tasks' `due` field is date-only (the API discards time-of-day), it duplicates data already in Calendar (time blocks) and Firestore (plan ledger), and it adds an extra OAuth scope and moving part. Firestore gives full schema control, real timestamps, effort estimates, and lives next to the plan ledger. The only thing Google Tasks offered — visibility in the user's real Google Tasks app — is preserved as the optional one-way mirror (S4).

**State (Firestore).** Cloud Run is stateless/ephemeral across cold starts (**CONFIRMED**), so external state is mandatory. Stored: **subtasks** (title, effort, status, parent goal, timestamps), the **plan ledger** (what was scheduled + why + outcome), the **action log** (undo + receipts), **learned preferences**, a **last-tick cursor**, and **OAuth refresh tokens**. Free tier ~50k reads / 20k writes per day (verify in console); batch writes, don't write per tick.

---

## 6. Google technologies utilized (explicit)

| Technology | How Kairos uses it | Status |
|------------|--------------------|--------|
| **Gemini via Vertex AI — function calling** | The brain. Plans, prioritizes, emits structured tool calls driving every autonomous action, and composes the **Lane B rescue draft** (confirm-first, never sent). Accessed through **Vertex AI over ADC** (bills the GCP project), models `gemini-2.5-flash` + `gemini-2.5-flash-lite` fallback. | **IMPLEMENTED**; Vertex AI on project credit, **not** an AI Studio API key |
| **Google Calendar API** | `freebusy.query` to find open time; `events.insert` to book focus blocks; `events.patch` to rebook; `events.delete` for undo. The autonomous-action surface. | **IMPLEMENTED**; OAuth scopes `calendar.events` + `calendar.freebusy` |
| **Cloud Run** | Hosts the app + agent loop. The GCP deploy target satisfying the requirement. | **CONFIRMED**; `gcloud run deploy` (skeleton deployed) |
| **Firestore** | Source of truth: subtasks, action log, prefs, OAuth tokens (plan ledger + cursor planned). | **IMPLEMENTED**; provisioned + used directly (ADC) |
| **Google Maps Routes API** | Live drive time from an origin to located events, via a server-side `/commute` endpoint (the key never reaches the browser). Powers commute on located goal blocks and on demo events. | **IMPLEMENTED**; `MAPS_API_KEY` server-side only |
| **Google Maps JavaScript API** | Interactive pin picker for the optional goal location (drop/drag a destination pin; shows a current-location marker). | **IMPLEMENTED**; uses a **separate referrer-restricted `MAPS_BROWSER_KEY`** in the browser — never the server key; picker hides if unset |
| **Google Geocoding API** | Reverse-geocodes a picked pin into a **short** place label (area/place name, not full address) for display. | **IMPLEMENTED**; server-side via `MAPS_API_KEY` |
| **Google Places API (New)** | `places:searchNearby` to seed the demo with **real venues near the viewer** for realistic locations + commute. | **IMPLEMENTED**; server-side via `MAPS_API_KEY`; guaranteed fallback seed |
| **Web Speech API** | Browser-side voice capture for goal/deadline dictation (best-effort NL deadline parsing), free, no key; graceful fallback to typing. | **IMPLEMENTED**; cross-browser uneven (best in Chrome, text fallback) |
| **Cloud Scheduler** *(stretch — outstanding)* | Cron for unattended proactive runs (S1). | **CONFIRMED + buildable** by us |
| **Google Tasks API** *(optional nicety, S4)* | One-way mirror of Firestore subtasks into the user's real Google Tasks app. | **CONFIRMED**; `due` date-only (fine for a mirror); extra scope `tasks` |

This is a substantial, genuine Google footprint — Gemini + Calendar + Cloud Run + Firestore are all load-bearing, with the **Maps platform (Routes + JavaScript + Geocoding + Places)**, Web Speech, and (stretch) Cloud Scheduler and the optional Tasks mirror as real (not decorative) additions. The "Usage of Google Technologies" criterion rewards correct use over tool count; each is chosen for genuine fit.

---

## 7. UI / screens

1. **Today / Command screen (home).** "Kairos is on it" status + the agent's **receipt feed** ("Booked 2–4pm for deck · Undo"). Today's timeline (agent-made vs. user-made blocks color-coded). A prominent **"Run agent now"** button.
2. **Goal capture.** One input ("What needs to get done, and by when?") with a mic button (stretch S2). On submit → subtasks stream in → "Scheduled ✓".
3. **The Save (conflict) view.** Before/after schedule diff with the agent's one-line reasoning and **Undo / Keep**.
4. **Priority queue.** Ranked "what to do now" list, each item showing why (deadline + effort).
5. **Confirm tray (Lane B).** Slide-up card for outbound drafts: **Confirm / Edit / Dismiss** (confirm marks approved only — never sends; no Gmail scope). A task's drafts also appear in its task-detail view.
6. **Connect account / onboarding.** Google OAuth consent for Calendar.
7. **Task History.** List of past runs grouped by creation date (newest/oldest sort, goal search), with per-task delete (×) and clear-all (history-only — calendar blocks stay), plus a **task-detail** page showing that run's focus blocks with scoped undo-all + per-block delete and the task's rescue drafts.
8. **Goal location picker.** An optional interactive Google Maps modal (drop/drag a destination pin + a current-location marker) reachable from the command card; located blocks then show a short place label, commute, and a "See route" button.

Visual tone: calm, single-accent, "the assistant already handled it" — receipts over alarms. The whole UI is **responsive** down to phone widths, and destructive actions go through one **reusable confirm modal** (Yes / No / × close; backdrop + Esc cancel).

---

## 8. Scope cut line — MVP vs stretch (June 29)

**MVP (the demoable spine — must work live against a sandbox Google account):**
- Features **1, 2, 3, 4** + **5** (draft-the-rescue — **IMPLEMENTED**: confirm-first, never sent).
- The loop, Gemini function-calling, **Firestore as task + state store**, **direct OAuth 2.0** for Calendar on a single test account, a Cloud Run deploy, and a **synchronous trigger** for the live "save."

**Stretch — build ONLY after the spine is demo-proven (~June 27), in this fixed order:**
1. **S1 — Cloud Scheduler background cron.** Strengthens Agentic Depth (true unattended proactivity). Add first because it's the highest-value stretch and reuses the stored OAuth tokens you already built.
2. **S2 — Voice goal capture.** Cheap, free, no billing. ~1 hour. Demo in Chrome with a text fallback. *(You want this in the product — it lands here.)*
3. **S3 — Maps commute.** *(You want this too — but it's last for honest reasons: it requires enabling a billing account, and commute time is tangential to the deadline-defending core. Add only if real time remains and you've enabled billing.)*
4. **S4 — Google Tasks mirror** *(optional, anytime after spine).* One-way push of Firestore subtasks into the real Google Tasks app if you want output visible in a Google surface.

**Hard rule:** depth over breadth. One deep autonomous "save" demoed flawlessly beats five shallow features. Do not let any stretch item consume time the spine needed.

---

## 9. Honest risks & uncertainties

1. **Build/deploy compliance hinges on a verbal clarification.** Organizer said GCP/Cloud Run deploy is the requirement, AI Studio optional. **Get it in writing before the irreversible final submit.** Highest-priority non-technical item.
2. **OAuth implementation — RESOLVED (implemented).** Direct OAuth 2.0 with PKCE + refresh-token storage in Firestore is built and working; tokens auto-refresh and persist. The synchronous "Run agent now" path runs on a single test account.
3. **Google OAuth consent screen / verification.** Calendar scopes hit the unverified-app screen until verified (which takes time we don't have). **Mitigation:** demo on a single **pre-authorized test account** added as a test user on the consent screen.
4. **Cloud Run is stateless/ephemeral (CONFIRMED).** Firestore state mandatory.
5. **Firestore free-tier quota (approximate).** ~50k reads / 20k writes per day; batch writes; verify in console.
6. **Web Speech API not stable cross-browser (CONFIRMED).** Voice (S2) needs a text fallback; demo in Chrome.
7. **Maps Platform requires a billing account (CONFIRMED).** S3 gated on enabling billing; stays last.
8. **Dropping Google Tasks slightly reduces the raw Google-API count.** Mitigation: the remaining Google footprint (Gemini + Calendar + Cloud Run + Firestore + Scheduler) is still strong and *genuine*; the optional S4 mirror can add Tasks back if a higher count is wanted. Net: fewer date-only headaches, simpler MVP.
9. **Gemini billing — RESOLVED.** Gemini runs via **Vertex AI over ADC**, billing the GCP project's trial credit (no AI Studio prepay / API key). Region defaults to `global`; `asia-south1` availability for `gemini-2.5-flash` is **unverified**, which is why `global` is the default (`VERTEX_LOCATION` overrides it).
10. **Known imperfection (scheduling).** Block sizing is **effort-PROMPT-steered**, not deterministically minute-accounted; the hard guarantee is the **≤8-events/run cap (halts)** plus `AGENT_MAX_ACTIONS=20`. A run can't exceed 8 blocks, but exact total-minutes ≈ sum-of-efforts is not enforced.
11. **Rough edge (cosmetic).** Subtask `due` timestamps carry **microsecond precision** (e.g. `...:12.345678+05:30`) because they're derived from `datetime` arithmetic; harmless but ugly in API output. Not fixed — would just round/truncate to the minute.
12. **Rough edge (past-deadline UX).** A deadline already **in the past** makes `break_down_task` return **empty subtasks**, surfacing a confusing *"couldn't break down the goal"* message instead of either gracefully drafting a rescue or clearly stating *"that deadline has already passed."* Not fixed yet; the right fix is an explicit past-deadline branch.

---

## 10. Adversarial pass (lead's final review)

### Self-score against the rubric
Self-scores are a sanity check, not an objective measure. The Tasks→Firestore swap removes a workaround and simplifies the MVP (helps Completeness/Tech Implementation and reduces failure surface); committing to OAuth 2.0 aligns auth with the background-proactivity feature. Net estimate ~**83/100**, with remaining uncertainty being our implementation speed.

| Criterion | Weight | Est. | Reasoning |
|-----------|--------|------|-----------|
| Problem Solving & Impact | 20 | 17 | Real, sharply-scoped pain; agent does the work the user can't. |
| Agentic Depth | 20 | 17–18 | Genuine loop + risk-tiered autonomy; background cron (S1) nudges up if shipped. |
| Innovation | 20 | 14 | "The save" is the novel hook; crowded space. |
| Usage of Google Technologies | 15 | 14 | Heavy + correct (Gemini FC + Calendar + Cloud Run + Firestore + Scheduler). |
| Product Experience | 10 | 8 | Receipts-over-alarms UX coherent and demoable. |
| Tech Implementation | 10 | 8 | Simpler stack after Tasks→Firestore; soft spot is OAuth/cron execution time. |
| Completeness | 5 | 5 | All sections, scope line, honest risks. |
| **Total** | **100** | **~83** | |

### Tooling decisions recap
- **Google Tasks → Firestore** (core). Removes date-only workaround + duplication. Tasks available as optional mirror (S4).
- **Firebase Auth → direct Google OAuth 2.0** (committed). Needed for durable server-side tokens for background runs.
- **Calendar, Gemini, Cloud Run, Firestore, Cloud Scheduler** — kept; load-bearing.
- **Voice (S2) and Maps (S3)** — kept as wanted stretch, sequenced after the spine; Maps last due to billing + tangential fit.

---

## 11. Implementation plan & build order

> Deploy to Cloud Run. MVP spine first; the optional Cloud Scheduler cron and Tasks mirror remain after the spine demos.

**Stack:** Python + FastAPI backend (agent loop + Gemini function-calling + Calendar calls), single static HTML/JS frontend served by FastAPI, Firestore for all state + tasks, Cloud Run for deploy, direct Google OAuth 2.0 for auth.

**Build order:**
1. **Scaffold + deploy a hello-world to Cloud Run first** (`gcloud run deploy`). Prove the one hard requirement before any logic. *(Done.)*
2. **Direct Google OAuth 2.0** for `calendar.events`, single test account added as an OAuth test user. Store tokens in Firestore. *(Done.)*
3. **Firestore data model:** subtasks (title, effort, status, parent goal, timestamps), plan ledger, action log, prefs, cursor. *(Done.)*
4. **Read path:** `get_schedule_snapshot` — Calendar free/busy + Firestore subtasks. *(Done.)*
5. **Gemini function-calling loop** with the 8-function schema (§5), step-capped, deterministic pre-ranker feeding the model. *(Done.)*
6. **Write path, Lane A:** `create_calendar_event`, `reschedule_event`, `upsert_task`, `reprioritize` — with action log + Undo. *(Done.)*
7. **"The save":** detect collision/slip → autonomously rebook → surface before/after receipt; synchronous "Run agent now" trigger. *(Done.)*
8. **Lane B:** `draft_message` (draft-only, no Gmail scope) + confirm tray. *(Done.)*
9. **Done since:** humane scheduling, voice input (Web Speech), Maps commute times (Routes API), demo/guest mode, main-page schedule preview, **task history + task detail** (Firestore live / session demo), **rescue drafts tied to their task**, a **reusable confirm modal**, **optional per-goal location** (Maps JavaScript pin picker + Geocoding short labels + Routes commute + "See route"; display-only), **demo seed at real nearby venues** (Places API, with fallback), **parallelized calendar inserts**, and a **responsive mobile layout**.
10. **Outstanding:** S1 Cloud Scheduler cron; S4 optional Tasks mirror.

**Guardrails (mirror in CLAUDE.md):**
- Minimum OAuth scopes (`calendar.events`; add `tasks` only for S4). Never Gmail-send.
- Tasks are Firestore objects with real timestamps — no date-only constraint in the core.
- Batch Firestore writes; don't write per tick.
- Verify each Google API call against the real test account; don't assume API shapes — check docs.
- **Maps keys are split:** the server-side `MAPS_API_KEY` (Routes + Geocoding + Places (New)) is **never** exposed to the browser; the pin picker uses a **separate referrer-restricted `MAPS_BROWSER_KEY`** (Maps JavaScript API) and the feature hides cleanly if it's unset. Goal location is display-only — it must never feed scheduling, subtasks, or caps.

**Demo-day check:** the live "save" runs on the synchronous trigger without depending on the background cron. Inject a conflicting meeting → watch Kairos rebook → show receipt + Undo.

---

*Tooling revised for practicality: Firestore replaces Google Tasks as the core store; direct OAuth 2.0 committed for background proactivity. Built since the MVP spine (see §4/§6): voice input, the Maps platform (commute + optional location pin picker + short-label geocoding + demo nearby venues), task history + task detail, task-tied rescue drafts, a reusable confirm modal, parallelized calendar writes, and a responsive mobile layout. The Cloud Scheduler cron is the one outstanding stretch item. Remaining open item is non-technical: secure the organizer's GCP-deploy clarification in writing before final submit.*
