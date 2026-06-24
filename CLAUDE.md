# Project: Clutch — The Last-Minute Life Saver (Vibe2Ship hackathon)

Goal: build and ship a working AI productivity agent, with design.md as the
living source-of-truth spec.

Problem statement: "The Last-Minute Life Saver" — an AI productivity companion
that PROACTIVELY plans, prioritizes, and AUTONOMOUSLY acts to help users finish
tasks before deadlines (NOT passive reminders).

## Build & deploy constraints
- Builder: **Claude Code in VS Code** (local development). NOT AI Studio Build Mode.
- Deploy requirement: the solution must be **deployed on Google Cloud Platform**
  (Cloud Run). AI Studio is optional, not mandatory — organizer confirmed
  AI Studio / Antigravity / manual deploy are all acceptable as long as it lands
  on GCP. (Keep this clarification IN WRITING before final submit.)
- Open-source libraries allowed with proper credit.
- We wire up auth / OAuth scopes / Firestore / Cloud Scheduler ourselves in code.

## Stack decisions (committed)
- Backend: Python + FastAPI on Cloud Run.
- State + task store: **Firestore** (single source of truth for tasks, plan
  ledger, action log, prefs, cursor, OAuth tokens).
- Auth: **direct Google OAuth 2.0** (google-auth / Authlib), refresh tokens
  stored in Firestore. NOT Firebase Auth — we need durable server-side tokens
  for background (Cloud Scheduler) agent runs, which is OAuth 2.0's strength.
- LLM: Gemini with function calling.

## Tooling decisions (and why) — do not re-litigate
- **Google Tasks dropped as a dependency.** Its API `due` is date-only and it
  duplicates Calendar + Firestore. Subtasks live in Firestore instead (real
  timestamps, full schema control). Optional one-way mirror into Google Tasks is
  a STRETCH nicety only, not MVP.
- **Google Calendar API stays** — it is the product's action surface ("the
  save"). Highest-friction tool but irreplaceable; pay the OAuth cost.
- Cloud Run, Firestore, Gemini, Cloud Scheduler all kept (load-bearing).

## Rubric (design every decision against this)
Problem Solving & Impact 20, Agentic Depth 20, Innovation 20,
Usage of Google Technologies 15, Product Experience 10, Tech Implementation 10,
Completeness 5.
"Usage of Google Technologies" rewards correct, substantive use — not tool count.

## Hard rules
- Deadline June 29, 2:00 PM. MVP spine FIRST, demo-proven (~June 27), THEN stretch.
- Stretch order is fixed: (1) Cloud Scheduler background cron, (2) voice input,
  (3) Maps commute. Do not build any stretch item before the spine works.
- Minimum OAuth scopes only: `calendar.events`, `tasks` (or just `calendar.events`
  if Tasks mirror is dropped). NEVER request a Gmail-send scope.
- Do NOT invent API capabilities — if uncertain, say so and verify before relying.

## Verified constraints to respect
- Google Tasks `due` is DATE-ONLY (only relevant if we add the optional mirror).
- Cloud Run is stateless/ephemeral across cold starts — Firestore state is mandatory.

Truthfulness rule: mark any unverified technical claim rather than asserting it.
