# Clutch — The Last-Minute Life Saver

*An AI productivity companion that proactively plans, prioritizes, and autonomously acts so you finish before the deadline — not a reminder app.*

> **Design provenance:** synthesized by the team lead from three specialist reviews (Product Architect, Agentic Systems Engineer, Google Integration Specialist). Original technical claims were marked **CONFIRMED** or **UNVERIFIED** per the project truthfulness rule, with the caveat that none of the specialists had live web access (cutoff Jan 2026).
>
> **Verification pass (added post-design, with live docs):** §2 of the doc's two load-bearing UNVERIFIED items have now been checked against current Google documentation. Results are folded into §5, §6, §9, and a new §11. Each verified claim cites whether it came from an authoritative Google source or a secondary source. **One item (Cloud Scheduler auto-provisioning) remains genuinely unverified — the absence of confirmation is not proof of absence, so it is still planned around, not assumed.**

---

## 1. Problem statement & target user

**Problem.** People miss deadlines not because they forget them, but because they fail to *act in time*: they don't break big work into doable steps, don't reserve time for it, and don't re-plan when life collides with the plan. Existing tools (reminders, to-do lists, calendars) are **passive** — they surface information and wait for the human to do all the work. The moment of failure is precisely the moment the user is too overloaded to manage their own schedule.

**Target user.** A busy student or early-career knowledge worker juggling 5–15 concurrent obligations (assignments, deliverables, errands) with hard deadlines, who already lives in Google Calendar and Google Tasks and routinely finds themselves scrambling at the last minute.

**Insight.** The leverage is not *better reminding* — it's **the app doing the planning work the user has no time to do**: decomposing, time-blocking, and rescuing the schedule when it breaks.

---

## 2. Solution overview (one paragraph)

Clutch is a proactive agent that takes a stated goal ("finish the slide deck by Friday 5pm"), uses Gemini to **break it into time-estimated subtasks**, reads your calendar's free/busy, and **autonomously books focus blocks** on your real Google Calendar to guarantee the work fits before the deadline — writing real Google Tasks for the checklist. It runs on a schedule (and on key events), so when a new meeting collides with your plan or a task slips, it **re-plans and rebooks on its own**, then shows you a one-line receipt of what it changed with an undo. It acts by default on your own planning surface (reversible) and asks first only for anything outbound or irreversible. That re-plan-under-pressure behavior — "the save" — is the product's reason to exist and its core differentiator.

---

## 3. Core agentic behavior — how it plans, prioritizes, and executes autonomously

This is the differentiator and the highest-weighted scoring surface (Agentic Depth 20). The agent runs a **perceive → plan → prioritize → act → observe** loop, proactively, without being asked.

### The loop (one "tick")
1. **Perceive** — Load world state: Google Calendar events + free/busy, Google Tasks with due dates, and the agent's own memory (prior plans, prior actions, learned preferences). Compute, per task, *time remaining vs. work remaining*.
2. **Prioritize** — A deterministic pre-ranker scores every open task: `urgency = f(deadline_proximity, estimated_effort, dependencies)`. The ranked list is fed to Gemini so priority is auditable and the model isn't inventing order from nothing.
3. **Plan** — A single Gemini call with the full function/tool schema attached. The prompt frames Gemini as a planner ("given these tasks, deadlines, and free/busy, what must happen in the next N hours?"). Gemini responds with **function calls**, not prose.
4. **Act** — Execute the returned calls, split into two risk lanes (below).
5. **Observe** — Record what was done, the API results, and write back to memory. The next tick perceives the consequences ("I blocked time for the deck yesterday — was it done?").

### The decision rule for acting *without being asked*
The agent acts autonomously when a task crosses a risk-tiered threshold:
- `deadline − now < buffer_for(estimated_effort)` **and** no calendar block exists → **create a focus block**.
- A scheduled block was missed or a new event collides with it → **reschedule it**.
These are reversible and internal to the user's own planning surface, so they happen automatically; the agent then notifies with a receipt.

### Human-in-the-loop boundary (judgment about autonomy = points)
- **Lane A — auto-execute, no confirmation:** actions touching only the user's *own* reversible planning surface — `create_calendar_event` (focus block), `reschedule_task`, `break_down_task`, internal re-prioritization. The agent acts, then surfaces *"I moved your 3pm prep to 5pm because your dentist ran over — Undo."*
- **Lane B — propose-and-confirm:** anything **outbound or irreversible** — `draft_message` to a third party, deleting events. The agent prepares it and waits for one tap.

This explicit split is itself an Agentic-Depth argument: the agent demonstrates *judgment about when autonomy is appropriate*, not "do whatever the model says."

### Proactive trigger (what makes it proactive, not a chatbot)
- **Production:** a scheduled trigger fires the agent on a cron (a morning planning run + periodic checks). **[STILL UNVERIFIED — see §9.1 / §11]** whether AI Studio Build Mode itself provisions Cloud Scheduler; treated as a documented one-time manual step.
- **Demo & event-driven:** a synchronous trigger ("Run agent now," or fired when a user adds a tight-deadline task / a colliding event appears) so "the save" is watchable live in 3 minutes — *inject a conflicting meeting → watch Clutch rebook the focus block.* **This is the primary demo mechanism and does not depend on any unverified cron.**

---

## 4. Feature list

Each feature is autonomy-first (the agent *does* the thing). Buildability = realism for AI Studio Build Mode → Cloud Run.

| # | Feature | What the agent autonomously DOES | Rubric criterion served | Google tech | Buildability |
|---|---------|----------------------------------|--------------------------|-------------|--------------|
| 1 | **Goal capture + auto-decompose** | User states a goal; Gemini breaks it into time-estimated subtasks and **writes them as real Google Tasks** with due dates — no manual entry. | Agentic Depth, Problem Solving | Gemini function-calling, Google Tasks API (`tasks.insert`) | Medium |
| 2 | **Autonomous schedule placement** | Reads free/busy, finds open slots, and **creates Calendar focus blocks** to reserve the time. It books, not suggests. | Agentic Depth (core), Innovation | Gemini function-calling, Calendar API (`freebusy.query` + `events.insert`) | Medium |
| 3 | **Re-plan on conflict — "the save"** | When a deadline is at risk (collision / slipped task), **autonomously moves and rebooks** events and re-sequences tasks, then reports the change. The money shot. | Agentic Depth, Innovation, Problem Solving | Calendar API (`events.patch`), Gemini reasoning loop | Medium–Hard |
| 4 | **Triage & prioritize** | Ranks open tasks by deadline pressure + effort and **commits "what to do now"** to the list. | Agentic Depth, Product Experience | Gemini function-calling, Google Tasks API (`tasks.patch`) | Easy–Medium |
| 5 | **Draft-the-rescue-message** *(Lane B, confirm)* | When a deadline genuinely can't be met, **drafts the extension/heads-up message** for the user to send with one tap. Draft-only — no automated send. | Innovation, Product Experience | Gemini generation (no Gmail-send scope) | Easy |
| S1 | **Voice goal capture** *(stretch)* | Speak a goal; transcribed into feature 1's pipeline. | Product Experience, Innovation | Web Speech API (browser, free) | Easy–Medium |
| S2 | **Commute-aware "leave now"** *(stretch)* | If a fixed event needs travel, auto-adjusts the surrounding schedule. | Innovation | Maps Routes/Places API | Medium |

---

## 5. Architecture *(REVISED after verification — now Firebase-native)*

> **What changed and why.** The original architecture assumed raw Firestore + a hand-rolled OAuth flow + a Cloud Run agent loop, all feared to be manual GCP console work. Current Google docs show AI Studio Build Mode natively provisions **Firebase Authentication (Google Sign-In), Firestore, and Google Workspace OAuth (Calendar, Tasks, etc.)** automatically. The architecture below is rewritten to use that native scaffolding, because building *with* it (rather than against it) is what the AI Studio agent will actually produce.
>
> Source for the capability: AI Studio Build mode docs list "Firebase Firestore and Authentication: automatically provision and set up… the agent handles the entire setup process and even writes the code" and "Google Workspace integrations: Connect your app to… Calendar, and more. AI Studio handles all the OAuth configuration automatically." (ai.google.dev/gemini-api/docs/aistudio-build-mode — authoritative.) No-billing provisioning confirmed by the Google Cloud "Starter Tier for Google AI Studio" blog (cloud.google.com — authoritative).

```
                ┌──────────────────────────────────────────────┐
   Scheduled     │            Cloud Run service (deploy)         │
   trigger       │  (AI Studio Build Mode app, stateless)        │
  [UNVERIFIED:   │                                              │
   auto-cron] +  │   ┌────────────┐   plan    ┌──────────────┐  │
  in-app "Run    │   │  Agent     │──────────▶│   Gemini     │  │
  now" / events  │   │  loop      │◀──────────│  (function-   │  │
  (CONFIRMED     │   │ perceive→  │  function │   calling)    │  │
   demo path)    │   │ prioritize │   calls   └──────────────┘  │
                 │   │ →plan→act  │                              │
                 │   │ →observe   │── execute tool calls ──┐     │
                 │   └─────┬──────┘                        │     │
                 │         │ load/save state               ▼     │
                 │   ┌─────▼──────┐         ┌───────────────────┐│
                 │   │ Firestore  │         │ Google Workspace  ││
                 │   │ (plans,    │         │ APIs via Firebase ││
                 │   │ action log,│         │ Auth OAuth scopes ││
                 │   │ prefs,     │         │ Calendar + Tasks  ││
                 │   │ cursor)    │         └───────────────────┘│
                 │   └────────────┘                              │
                 │   Firebase Auth (Google Sign-In) issues the   │
                 │   user identity + OAuth consent for scopes.   │
                 └──────────────────────────────────────────────┘
```

**Identity & OAuth (REVISED).** Use **Firebase Authentication with Google Sign-In**. The same sign-in flow requests the OAuth **scopes** for Calendar and Tasks. Per the Starter Tier docs, once a user logs in, the app can request OAuth access scopes to interact with their Calendar/Gmail/Sheets data — and this works on the Starter Tier **without a billing account**. This replaces the original design's manual "store OAuth refresh tokens in Firestore" plan: token handling rides on the Firebase Auth / Google Sign-In integration the agent generates. *(Practical caveat: exactly how refresh tokens are persisted and reused for server-side background calls is the part most likely to need hand-adjustment — see §9.1.)*

**Agent loop.** A bounded single-agent function-calling loop (deliberately *not* a multi-agent ReAct planner — won't be demo-stable in 6 days): Gemini decides → calls one of the functions below → loop observes the result → continues until done or a **step cap (~8 calls)**. Server-side logic may land in a Firebase Callable/Cloud Function or in the Cloud Run service depending on what the agent scaffolds; both are acceptable. *(Secondary sources note background-job/multi-step server logic is the weakest area of the generated output, so expect to hand-tune this — see §9.)*

**Gemini function/tool schema** (declared to Gemini; executed server-side):

| Function | Params | Effect | API dependency |
|----------|--------|--------|----------------|
| `get_schedule_snapshot` | `window_start, window_end` | Perceive: read calendar + tasks + free/busy | Calendar `events.list`/`freebusy.query`, Tasks `tasks.list` — **CONFIRMED** |
| `create_calendar_event` | `title, start, end, description` | Book a focus block (Lane A) | Calendar `events.insert` — **CONFIRMED** |
| `reschedule_event` | `event_id, new_start, new_end, reason` | Move a block (Lane A) | Calendar `events.patch` — **CONFIRMED** |
| `break_down_task` | `goal, deadline` → `subtasks[]` | Decompose goal; write subtasks | Gemini (internal) + Tasks `tasks.insert` — **CONFIRMED API; Tasks `due` is date-only, see §9.2** |
| `upsert_task` | `task_id?, title, due, status` | Create/update/complete a task | Tasks `tasks.insert`/`tasks.patch` — **CONFIRMED** |
| `reprioritize` | `ranked_task_ids[]` | Commit new ordering / "now" pick | Tasks `tasks.patch` — **CONFIRMED** |
| `draft_message` | `recipient, body` | Compose extension/heads-up draft (Lane B, no send) | Internal (no Gmail scope) — **CONFIRMED** |
| `notify_user` | `message, urgency` | Surface the agent's receipt/nudge | App-internal — **CONFIRMED** |

**State (Firestore).** Cloud Run is stateless and ephemeral across cold starts (**CONFIRMED**), so persistence is external and mandatory. Stored: the **plan ledger** (what was scheduled + why + outcome), the **action log** (for undo + receipts), **learned preferences** (working hours, typical durations), and a **last-tick cursor** (so the same trigger isn't re-acted on). **REVISED:** Firestore provisioning is now **CONFIRMED auto-provisioned** by the AI Studio agent (ai.google.dev Build mode docs). **Free-tier limit to design around:** the free Spark tier is ~50,000 reads / 20,000 writes per day, and all AI-Studio-provisioned Firestore DBs share one quota group that pauses until ~midnight Pacific if exhausted (secondary sources, consistent with Google's published Firestore free quotas — treat numbers as approximate, verify in console). **Design implication:** do not read/write Firestore on every loop tick unnecessarily; batch state writes.

---

## 6. Google technologies utilized (explicit) *(REVISED status column)*

| Technology | How Clutch uses it | Status (post-verification) |
|------------|--------------------|----------------------------|
| **Gemini API — function calling** | The brain. Plans, prioritizes, and emits structured tool calls that drive every autonomous action. | **CONFIRMED**, free tier (rate-limited) |
| **Google Calendar API** | `freebusy.query` to find open time; `events.insert` to book focus blocks; `events.patch` to rebook on conflict. The autonomous-action surface. | **CONFIRMED**; OAuth scope handled by AI Studio Workspace integration (was "needs manual OAuth" — now auto-configured per Build mode docs) |
| **Google Tasks API** | `tasks.insert`/`tasks.patch` to write decomposed subtasks and commit priority. Checklist layer. | **CONFIRMED** API; `due` is **date-only — VERIFIED** (Google Tasks API reference: "the time portion of the timestamp is discarded… It isn't possible to read or write the time that a task is due via the API.") |
| **Firebase Authentication (Google Sign-In)** | User identity + the OAuth consent that grants Calendar/Tasks scopes. | **CONFIRMED auto-provisioned** by AI Studio agent (Build mode docs, authoritative) |
| **Cloud Run** | Hosts the AI Studio Build Mode app + agent loop. | **CONFIRMED** deploy target of Build Mode |
| **Firestore** | Persistent agent memory (plan ledger, action log, prefs, cursor). | **CONFIRMED auto-provisioned** by AI Studio agent; shared free-tier quota (design around it) |
| **Cloud Scheduler** | Cron that makes the agent *proactive* in production. | **EXISTS (confirmed)**; **auto-wiring by Build Mode STILL UNVERIFIED** — no doc found either way. Plan: synchronous trigger for demo, cron documented as manual `gcloud` step. |
| **Web Speech API** *(stretch)* | Browser-side voice capture, free, no key. | **CONFIRMED** exists; cross-browser support uneven (demo in Chrome) |
| **Maps Platform (Routes/Places)** *(stretch)* | Commute-aware schedule adjustment. | **CONFIRMED** exists; **requires a billing account** (free tier is a monthly credit, not free) |

---

## 7. UI / screens (so the AI Studio agent knows what to render)

1. **Today / Command screen (home).** Top: "Clutch is on it" status + the agent's latest **receipt feed** ("Booked 2–4pm for deck · Undo"). Center: today's timeline (calendar blocks the agent created, color-coded as agent-made vs. user-made). A prominent **"Run agent now"** button (demo + manual trigger).
2. **Goal capture.** A single input ("What needs to get done, and by when?") with a mic button (stretch). On submit → shows the agent decomposing live (subtasks streaming in) then "Scheduled ✓".
3. **The Save (conflict) view.** Triggered when a collision is detected: a before/after diff of the schedule with the agent's one-line reasoning and **Undo / Keep**.
4. **Priority queue.** Ranked "what to do now" list, each item showing why it's ranked there (deadline + effort).
5. **Confirm tray (Lane B).** Slide-up card for outbound drafts: the drafted message + **Send / Edit / Dismiss**.
6. **Connect account / onboarding.** Google Sign-In (Firebase Auth) consent for Calendar + Tasks scopes.

Visual tone: calm, single-accent, "the assistant already handled it" — receipts over alarms.

---

## 8. Scope cut line — MVP vs stretch (June 29)

**MVP (the demoable spine — must work live against a sandbox Google account):**
- Features **1, 2, 3, 4** — capture → decompose → auto-schedule → defend the deadline. Plus feature **5** (draft-the-rescue) since it's cheap and pure Gemini.
- The loop, Gemini function-calling, Firestore state, Firebase Auth + Calendar/Tasks scopes on a single test account, and a **synchronous trigger** for the live "save."

**Cut line.** Do **not** build anything below until the spine is demo-proven (target ~June 27):
- **S1 Voice** — input sugar, not autonomy; first to demote. Web Speech only (no Cloud Speech infra).
- **S2 Maps commute** — needs billing; thin autonomy payoff; optional Innovation flourish only.
- **Automated Cloud Scheduler cron** — if Build Mode can't wire it (unverified), ship with the synchronous/event trigger and document the cron as a one-time manual `gcloud` step.

**Hard rule:** depth over breadth. One deep autonomous act demoed flawlessly beats five shallow features. The classic hackathon loss is a wide app that can't show a single genuine "save."

---

## 9. Honest risks & uncertainties *(REVISED with verification outcomes)*

> Per the truthfulness rule. Items now carry their verification result.

1. **Original BIGGEST RISK (OAuth + Firestore + Auth auto-wiring) — LARGELY RESOLVED.** Current Google Build mode docs confirm AI Studio auto-provisions Firebase Auth, Firestore, and Google Workspace OAuth (incl. Calendar), and the Starter Tier confirms this works with no billing account. **Residual risk (real, narrower):** server-side *background* use of the user's OAuth token — i.e., the agent calling Calendar on the user's behalf when the user isn't actively in the session — is the part least confirmed by docs. The native flow is clearly designed for in-session, user-present OAuth calls. **Mitigations retained:** (a) demo on a single **pre-authorized test account**; (b) drive the live "save" with a **synchronous, user-present trigger** (sidesteps the background-token question entirely); (c) **graceful degradation** — if server-side background writes can't be wired in 6 days, the in-session "Run agent now" path still demonstrates full autonomy.
2. **Google Tasks `due` is date-only — VERIFIED TRUE.** Confirmed against the official Tasks API reference (the time portion is discarded; you cannot read/write a due *time* via the API). The design's existing mitigation is correct and now load-bearing: **Calendar events are the time-of-day scheduling primitive; Tasks is only the date-level checklist layer.**
3. **Cloud Run is stateless/ephemeral (CONFIRMED).** External state (Firestore) is mandatory — no filesystem/in-memory persistence across cold starts.
4. **Cloud Scheduler auto-provisioning — STILL UNVERIFIED.** No documentation found confirming *or* denying that Build Mode wires up Cloud Scheduler. Secondary sources note background jobs are the weakest area of AI-Studio-generated apps, which is weak evidence it's *not* automatic. **Plan unchanged:** synchronous trigger for the demo; cron treated as a manual step. Do not build the demo around background proactivity.
5. **Firestore free-tier shared quota (treat as approximate).** ~50k reads / 20k writes per day, shared across AI-Studio DBs, pausing until ~midnight PT if exhausted. Batch state writes; don't hammer Firestore per tick. (Verify exact numbers in the Firebase console before relying on them.)
6. **Web Speech API not stable cross-browser (CONFIRMED caveat).** Voice (stretch) needs a text fallback; demo in Chrome.
7. **Maps Platform requires a billing account (CONFIRMED).** "Free tier" is a monthly credit. S2 stays optional.
8. **Source-quality caveat.** Some verification detail (Firebase backend shape, free-tier numbers) came from secondary blogs. Those were used only where they agree with the two authoritative Google sources (official Build mode docs + Google Cloud Starter Tier blog). The two authoritative claims — auto-provisioned Auth/Firestore/Workspace-OAuth, and no-billing Starter Tier — are the ones the design now leans on.

---

## 10. Adversarial pass (lead's final review + verification adjustment)

### Self-score against the rubric
The lead's original estimate was ~81/100. The two biggest point-suppressors were the OAuth/Firestore feasibility risk (held Tech Implementation at 7 and Google Tech at 13) and the unverified Tasks semantics. With OAuth/Firestore now confirmed auto-provisioned and Tasks-date-only confirmed-and-handled, those two criteria are better supported than the conservative estimate assumed. A defensible revised self-estimate is Tech Implementation ~8 and Google Tech ~14, i.e. **~83/100** — *with the honest reminder that a self-score is a sanity check, not an objective measure, and the judges' rubric interpretation is unknown.*

| Criterion | Weight | Orig. | Revised | Reasoning for change |
|-----------|--------|-------|---------|----------------------|
| Problem Solving & Impact | 20 | 17 | 17 | unchanged |
| Agentic Depth | 20 | 17 | 17 | unchanged; still capped by demo-visible proactivity |
| Innovation | 20 | 14 | 14 | unchanged |
| Usage of Google Technologies | 15 | 13 | 14 | OAuth/Firestore/Auth now confirmed native, not manual |
| Product Experience | 10 | 8 | 8 | unchanged |
| Tech Implementation | 10 | 7 | 8 | biggest feasibility risk substantially cleared |
| Completeness | 5 | 5 | 5 | unchanged |
| **Total** | **100** | **~81** | **~83** | |

### 3 weakest features → fixes (unchanged, still valid)
1. **S1 Voice** — demoted to stretch, Web Speech only with text fallback, never on the MVP critical path.
2. **S2 Maps commute** — cut from MVP to optional stretch with explicit billing caveat.
3. **F5 Draft-the-rescue** — kept as Lane-B draft-only (no Gmail-send scope); cheap pure-Gemini initiative beat.

### Biggest infeasibility risk → status
**Was:** OAuth + Scheduler + Firestore auto-wiring (UNVERIFIED). **Now:** Auth + Firestore + Workspace-OAuth **verified auto-provisioned**; only **Cloud Scheduler auto-wiring and server-side background token use** remain open, both fully mitigated by the synchronous user-present trigger that drives the demo.

---

## 11. AI Studio Build Mode — build prompt (ready to paste)

> Paste this into a **new AI Studio Build Mode app**. It is written to make the agent wire up the verified-native pieces (Firebase Auth + Workspace OAuth + Firestore) explicitly, so they're configured on the first pass. Build the MVP spine first; add stretch items only after the spine demos.

```
Build a web app called "Clutch" — a proactive AI productivity agent (not a
reminder app). Use Firebase as the backend.

CORE BEHAVIOR:
A user states a goal with a deadline (e.g. "finish slide deck by Friday 5pm").
The app uses Gemini with function calling to:
  1. Break the goal into time-estimated subtasks and write them as real Google
     Tasks (note: Google Tasks due dates are DATE-ONLY — store time-of-day info
     in the Calendar events, not in Tasks).
  2. Read the user's Google Calendar free/busy, find open slots, and create
     Calendar "focus block" events to reserve time before the deadline. It
     BOOKS the time, it does not just suggest.
  3. When a new event collides with a focus block, or a task slips,
     automatically reschedule/rebook the affected events and show a one-line
     "receipt" of what changed, with an Undo button. This re-plan-on-conflict
     behavior is the core feature — make it demoable via a "Run agent now"
     button and by injecting a conflicting meeting.

AUTONOMY RULES:
  - Auto-execute (no confirmation) for reversible actions on the user's OWN
    calendar/tasks: creating focus blocks, rescheduling, re-prioritizing. Show
    a receipt + Undo after acting.
  - Require one-tap confirmation only for outbound/irreversible actions (e.g.
    drafting a message to a third party — draft only, never auto-send; do NOT
    request Gmail send scope).

BACKEND / AUTH (please set these up):
  - Use Firebase Authentication with Google Sign-In.
  - Request Google OAuth scopes for Google Calendar (read free/busy + create
    and patch events) and Google Tasks (read + insert + patch).
  - Use Firestore to persist: the plan ledger (what was scheduled and why),
    an action log (for Undo and receipts), user preferences (working hours,
    typical task durations), and a last-run cursor. Batch writes — do not
    write to Firestore on every loop iteration.

GEMINI:
  - Use Gemini function calling. Declare these functions and execute them
    server-side: get_schedule_snapshot, create_calendar_event,
    reschedule_event, break_down_task, upsert_task, reprioritize,
    draft_message, notify_user.
  - Before calling Gemini, deterministically pre-rank open tasks by
    deadline-proximity + effort, and pass that ranking in, so priority is
    auditable.
  - Cap the agent at ~8 function calls per run.

UI (build these screens):
  1. Home: "Clutch is on it" status, a receipt feed with Undo, today's timeline
     (color-code agent-created blocks vs user blocks), and a prominent
     "Run agent now" button.
  2. Goal capture: one input ("What needs done, and by when?"), shows subtasks
     streaming in, then "Scheduled ✓".
  3. The Save view: before/after schedule diff with the agent's one-line reason
     and Undo / Keep.
  4. Priority queue: ranked "what to do now", each item showing why.
  5. Confirm tray: slide-up card for outbound drafts (Send / Edit / Dismiss).
  6. Onboarding: Google Sign-In consent for Calendar + Tasks.

Visual tone: calm, single accent color, "the assistant already handled it" —
receipts over alarms.

Do NOT build voice input or Maps/commute features yet — those are later stretch
additions. Build and make the above spine work first.
```

**After the agent generates this:** verify in the Firebase console that (a) Google Sign-In is enabled, (b) the Calendar + Tasks OAuth scopes are actually being requested, and (c) Firestore was provisioned. If the agent's first pass requests Gmail-send or any scope you didn't ask for, tell it to remove it. If server-side background calls to Calendar fail, fall back to the in-session "Run agent now" path for the demo (per §9.1).

---

*Verification pass complete. Open item that genuinely remains: whether AI Studio Build Mode auto-provisions Cloud Scheduler (planned around, not assumed). Everything else in the original UNVERIFIED set is now checked against current Google documentation.*
