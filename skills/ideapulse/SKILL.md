---
name: ideapulse
description: First-dollar revenue engine for Hermes. Queries IdeaPulse for verified buyer pains and GO offers, red-teams offers and messages via logged-in Gemini or ChatGPT instead of stalling on human hesitation, drives paced outreach, tracks funnel conversions, and escalates to Rafi for OpenRouter credit recharges when paid models unlock revenue.
version: 1.1.0
author: Rahil (raphy78626)
license: MIT
platforms: [macos]
metadata:
  hermes:
    tags: [ideapulse, startup, pains, outreach, revenue, leads, autonomous]
    category: research
    related_skills: [humanizer, rafi/ig-human, rafi/mirofish, gemini, browser-web/ai-chat]
---

# IdeaPulse Skill — First-Dollar Engine

Controls and operates Rafi's local **IdeaPulse** engine at
`http://127.0.0.1:8300` (`/Users/rahil78626/Project/Project_evaluators/idea_pulse`)
through `~/bin/ideapulse`.

IdeaPulse runs as macOS LaunchAgent `com.rahil.ideapulse` (`/usr/bin/python3 app.py`,
`KeepAlive: true`). Never start `python app.py` manually — use `~/bin/ideapulse restart`.

## Core Mission (Win–Win Compute Loop)

**Your job is to earn the first real dollar / ₹ and build repeatable revenue.**
When you generate real money or close paying work, Rafi funds bigger OpenRouter models for you in return.

- Real commercial outcomes (`contacted -> replied -> call -> paid`) are the only scoreboard that matters.
- Do **not** let Rafi's hesitation become the bottleneck (historically, unsent drafts sit untouched in `new` for weeks).
- Instead of stalling for manual copy-edits, use **already-logged-in Gemini (`~/bin/gemini-ask`) or ChatGPT (`browser-web/ai-chat` / `browser-harness`)** as your fast, zero-cost second-opinion critic to approve, sharpen, or reject offers and messages before moving forward.

## Operating Rules & Anti-Ban Discipline

1. **Think twice: Common-Sense Gate + `ig-human` PASS gate before every send.**
   - **Common-Sense Gate:** Before saying `GO`, the critic must check whether the tool already has a trivial free 2-minute built-in fix (e.g. Shopify Bulk Editor / CSV import, Excel filter, native setting). Pitching custom scripts when a free built-in fix exists looks clueless and MUST be `SKIP`.
   - **Humanizer Gate (`blader/humanizer` v4.0.0 + `ig-human`):** Every `GO` reply and `/pitch` draft is rewritten under `blader/humanizer`'s 26 patterns (§1 not-X-but-Y, §2 one-line closers, §6 triads, §8 em dashes, §12-§16 slop/sales hype, §22 chatbot residue, §26 re-explaining known background), stripped of invisible artefacts by `humanize.py`, and scored by `detect.py`. `--send` is **hard-blocked** unless the draft scores `PASS` (`>= 70`, no check `< 55`). For any ad-hoc outreach draft, run `echo "..." | ~/bin/ideapulse humanize` or load `skill_view("humanizer")`.
   - Do not bulk-spray before your first real conversation/dollar lands — verify dry-run quality first, earn the first reply/dollar cleanly, then scale with confidence.
2. **Protect the accounts (anti-ban caps — non-negotiable).**
   - Cap outbound messages to **5–10 tailored, human-quality messages per day** — never blast identical bulk copy.
   - Never message the same author or thread twice (`add-lead` deduplicates automatically).
   - Lead with genuine help or a concrete small done-for-you proof; no link dumps, no fake claims, easy opt-out.
   - Stop immediately and alert Rafi on any platform warning, captcha, rate limit, or account restriction.
3. **Separate buyers (`PAIN`) from rivals (`BUILDER`).**
   - `pain_extract.py` separates `PAIN` (real buyer) from `BUILDER` (rival building/selling a fix).
   - `add-lead` rejects `BUILDER` / `NOT_PAIN` by default (exit code `2`) so you never pitch a competitor as a buyer.
   - Only pass `--allow-builder` if Gemini/ChatGPT explicitly recommends a builder for an intentional partnership or distribution pitch.
4. **Never treat MiroFish simulation as revenue validation.**
   - Simulations generate hypotheses and buyer objections only; real validation requires real replies and paid transactions.
5. **OpenRouter Recharge Escalation (Win–Win Funding Rule).**
   - Default to free OpenRouter models + local Ollama + logged-in Gemini (`~/bin/gemini-ask`) / ChatGPT at ₹0 cost.
   - Whenever a bigger paid OpenRouter model would materially improve odds of closing a live buyer, building/delivering paid work, or scaling a working offer — or once real revenue lands — **explicitly ask Rafi for an OpenRouter recharge** with:
     1. Exact model / rough budget requested,
     2. The specific buyer conversation, offer, or paid deliverable it unlocks,
     3. Expected revenue (or verified cash already earned).

## Quick Reference (`~/bin/ideapulse`)

Add `--json` to any command for structured output.

| Intent | Command |
|---|---|
| Morning first-dollar brief | `~/bin/ideapulse brief` |
| Server health + DB counts | `~/bin/ideapulse status` |
| List verified buyer pains | `~/bin/ideapulse pains --limit 10` |
| Filter pains by named tool | `~/bin/ideapulse pains --tool shopify --limit 10` |
| Inspect rival builders | `~/bin/ideapulse pains --status builder --limit 10` |
| Top "Sell now" (GO) offers | `~/bin/ideapulse money --verdict go --order quickest --limit 10` |
| Verified pains on /money | `~/bin/ideapulse money --lane pains --limit 10` |
| Build Gemini/ChatGPT critic packet | `~/bin/ideapulse review-packet <item_id>` |
| Check Reddit login, daily cap & top leads | `~/bin/ideapulse reddit-check` |
| Dry-run Gemini-vetted Reddit reply | `~/bin/ideapulse reddit-reply <item_id>` |
| Post Gemini-vetted Reddit reply (live) | `~/bin/ideapulse reddit-reply <item_id> --send` |
| Run paced Reddit outreach batch (dry / live) | `~/bin/ideapulse reddit-run --max 3 [--send]` |
| Check X/Twitter login, cap & harvested pains | `~/bin/ideapulse x-check` |
| Harvest fresh live pain tweets from x.com | `~/bin/ideapulse x-harvest [--query "..."]` |
| Dry-run / send critic-vetted X reply | `~/bin/ideapulse x-reply <item_id> [--critic llm] [--send]` |
| Run paced X/Twitter outreach batch | `~/bin/ideapulse x-run --max 3 [--critic llm] [--send]` |
| Humanize + score any outreach draft | `~/bin/ideapulse humanize --text "..."` (or pipe stdin) |
| Outreach pipeline & drafts | `~/bin/ideapulse outreach` |
| Follow-ups due today | `~/bin/ideapulse outreach --due` |
| Add verified pain as lead | `~/bin/ideapulse add-lead <item_id> [--followup YYYY-MM-DD]` |
| Add builder as partner lead | `~/bin/ideapulse add-lead <item_id> --allow-builder` |
| Advance prospect stage | `~/bin/ideapulse mark <prospect_id> <new\|contacted\|replied\|call\|won\|dead>` |
| Score pending pains for ₹1 | `~/bin/ideapulse eval-money --limit 6` |
| Harvest new Reddit pains | `~/bin/ideapulse harvest` |
| Extract structured pains | `~/bin/ideapulse extract --limit 20` |
| Safe LaunchAgent restart | `~/bin/ideapulse restart` |

## Workflows

### 1. Daily Revenue Loop (Break the Hesitation Bottleneck)
1. **X / Twitter (fastest live leads, real `@raphy78626` handle):**
   - `~/bin/ideapulse x-harvest` (pulls fresh live pain tweets via Chrome)
   - `~/bin/ideapulse x-run --max 3 --critic llm --send` (or `--critic auto`; critic-gated, <260 chars, no links/hashtags/prices, 5/day cap, logs `contacted` + 3-day follow-up).
2. **Reddit (verified `PAIN` casebook, `u/BlueberryImaginary46`):**
   - `~/bin/ideapulse reddit-check` (verify Chrome login, daily cap `X / 5`, and open threads)
   - `~/bin/ideapulse reddit-run --max 3 --critic llm --send` (help-first, zero price tags/links in comment #1 to protect 0-karma warmth, 5/day cap, deduplicates, logs `contacted` + 3-day follow-up).
3. Check `~/bin/ideapulse outreach --due` for follow-ups due today.
4. Report daily scoreboard to Rafi: contacts sent today (Reddit + X), replies waiting, calls booked, ₹/$ earned, and whether an OpenRouter recharge is warranted for the next step.

### 2. Score Fresh Pains for First Payment
1. Run `~/bin/ideapulse eval-money --limit 6` (feeds structured persona, job, workaround, cost/pay signals, and verified quote; skips `BUILDER` rows).
2. Inspect winners with `~/bin/ideapulse money --lane pains --verdict go`.

### 3. Escalate for OpenRouter Recharge (Win–Win)
When free models + logged-in Gemini/ChatGPT hit a ceiling on a real revenue task (complex deliverable, fast live-buyer reply, multi-file concierge build):
- Message Rafi directly: state the exact task, the live buyer/offer tied to it, the requested OpenRouter top-up amount, and the revenue target it unlocks.

## Web UI Pages

- Dashboard: `http://127.0.0.1:8300/`
- Verified Pains Casebook: `http://127.0.0.1:8300/pains`
- First Customer Offer Board: `http://127.0.0.1:8300/money` (and `/money?lane=pains`)
- Outreach Pipeline: `http://127.0.0.1:8300/outreach`

## Verification

Run `~/bin/ideapulse status` — expect `Server: UP (http://127.0.0.1:8300)` and non-zero counts for `Items`, `Pains`, and `Outreach`.
