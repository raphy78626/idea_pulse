---
name: ideapulse
description: Query IdeaPulse for verified customer pains, first-dollar offers, and outreach prospects; stage pain authors as leads and track pipeline stages without auto-sending external messages.
version: 1.0.0
author: Rahil (raphy78626)
license: MIT
platforms: [macos]
metadata:
  hermes:
    tags: [ideapulse, startup, pains, outreach, revenue, leads]
    category: research
    related_skills: [rafi/mirofish, gemini]
---

# IdeaPulse Skill

Controls and queries Rafi's local **IdeaPulse** first-dollar engine at
`http://127.0.0.1:8300` (`/Users/rahil78626/Project/Project_evaluators/idea_pulse`)
through the headless CLI `~/bin/ideapulse`.

IdeaPulse runs as macOS LaunchAgent `com.rahil.ideapulse` (`/usr/bin/python3 app.py`,
`KeepAlive: true`). Never start `python app.py` manually — use `~/bin/ideapulse restart`
if the server needs a restart.

## When to Use

Load this skill whenever the user asks about:
- Morning brief / pipeline status ("how is IdeaPulse doing", "check my outreach pipeline", `/ideapulse`)
- Verified buyer pains ("what pains did we harvest", "find QuickBooks / Shopify / Xero pains", `/ideapulse_pains`)
- First-customer offers ("what can I sell this week", "top GO offers", `/ideapulse_money`)
- Staging leads or updating outreach stages ("add pain 8087 as a lead", "mark prospect 4 contacted", "who is due for follow-up today", `/ideapulse_outreach`)
- Running ingestion, extraction, or first-currency evaluation (`harvest`, `extract`, `eval-money`)

## Hard Guardrails

1. **Stage only — NEVER auto-send.** `add-lead` stages a lead in `/outreach` with a draft message for Rafi to review and send manually. Never auto-post Reddit comments/DMs or auto-send WhatsApp messages.
2. **Never treat `BUILDER` posts as customer leads.** `pain_extract.py` separates `PAIN` (real buyer) from `BUILDER` (rival building/selling a fix). `add-lead` enforces this and exits with code 2 on non-`PAIN` records.
3. **Never treat MiroFish simulation as revenue validation.** Simulations generate hypotheses and objections only; real validation requires contacted -> replied -> paid evidence in `/outreach`.
4. **Never launch a second server process.** Always restart via `~/bin/ideapulse restart` (`launchctl kickstart -k`), never `python app.py &` or `nohup`.

## Quick Reference (`~/bin/ideapulse`)

Add `--json` to any command when you need machine-readable output for filtering or synthesis.

| Intent | Command |
|---|---|
| Morning first-dollar brief | `~/bin/ideapulse brief` |
| Server health + DB counts | `~/bin/ideapulse status` |
| List verified buyer pains | `~/bin/ideapulse pains --limit 10` |
| Filter pains by named tool | `~/bin/ideapulse pains --tool shopify --limit 10` |
| Inspect rival builders | `~/bin/ideapulse pains --status builder --limit 10` |
| Top "Sell now" (GO) offers | `~/bin/ideapulse money --verdict go --order quickest --limit 10` |
| Verified pains on /money | `~/bin/ideapulse money --lane pains --limit 10` |
| Outreach pipeline & drafts | `~/bin/ideapulse outreach` |
| Follow-ups due today | `~/bin/ideapulse outreach --due` |
| Stage verified pain as lead | `~/bin/ideapulse add-lead <item_id> [--followup YYYY-MM-DD]` |
| Advance prospect stage | `~/bin/ideapulse mark <prospect_id> <new\|contacted\|replied\|call\|won\|dead>` |
| Score pending pains for ₹1 | `~/bin/ideapulse eval-money --limit 6` |
| Harvest new Reddit pains | `~/bin/ideapulse harvest` |
| Extract structured pains | `~/bin/ideapulse extract --limit 20` |
| Safe LaunchAgent restart | `~/bin/ideapulse restart` |

## Workflows

### 1. Morning First-Dollar Check (`/ideapulse`)
1. Run `~/bin/ideapulse brief`.
2. Report:
   - Outreach quota (`new / contacted / replied / won`, plus follow-ups due today).
   - Top 3 unclaimed verified pains with explicit pay/cost signals.
   - Top 3 `GO` offers.
3. Suggest one concrete next action (either follow up on existing `new`/due prospects or stage one pain via `add-lead`).

### 2. Turn a Verified Pain into a Staged Lead
1. Find candidate pains: `~/bin/ideapulse pains --limit 15 --json` (optionally `--tool <name>`).
2. Confirm `status == "PAIN"` and `in_outreach == false` with a verbatim quote and clear pay/cost signal.
3. Stage idempotently: `~/bin/ideapulse add-lead <item_id>`.
4. Return the staged prospect ID, thread URL, draft reply, and `http://127.0.0.1:8300/outreach` so Rafi can review and send manually.

### 3. Score Fresh Pains for First Payment
1. Run `~/bin/ideapulse eval-money --limit 6` (uses free OpenRouter models with local Ollama fallback; automatically feeds persona, job, workaround, cost/pay signals, and verified quote into scoring, skipping `BUILDER` rows).
2. Review top results with `~/bin/ideapulse money --lane pains --verdict go`.

## Web UI Pages

- Dashboard: `http://127.0.0.1:8300/`
- Verified Pains Casebook: `http://127.0.0.1:8300/pains`
- First Customer Offer Board: `http://127.0.0.1:8300/money` (and `/money?lane=pains`)
- Outreach Pipeline: `http://127.0.0.1:8300/outreach`

## Verification

Run `~/bin/ideapulse status` — expect `Server: UP (http://127.0.0.1:8300)` and non-zero counts for `Items`, `Pains`, and `Outreach`.
