# Model Comparison Sheet — Free / Cheap vs Flagship (via OpenRouter, 2026)

Prices are per million tokens on your OpenRouter account (current platform rates).
Tested live on IdeaPulse's idea-evaluation task (structured JSON verdict).

## Tier 1 — FREE (daily bulk, unlimited pipeline)

| Model | In / MTok | Out / MTok | Verdict quality | Real use |
|-------|-----------|-----------|-----------------|----------|
| **minimax/minimax-m3** | $0 | $0 | ★★★★ near-flagship on JSON | ⭐ IdeaPulse bulk eval, explain |
| minimax/minimax-m2.7 | $0 | $0 | ★★★ good, sometimes rate-limited | fallback in eval chain |
| google/gemini-3.1-flash-lite | $0 | $0 | ★★★ fast, concise | nutshells, simple classify |
| qwen/qwen3.6-flash | $0 | $0 | ★★★ (occasional empty reply on some endpoints) | classify only |
| z-ai/glm-5.3-flash | $0 | $0 | ★★★★ very strong free | eval + summaries |

## Tier 2 — CHEAP (near-flagship, pennies)

| Model | In / MTok | Out / MTok | Notes |
|-------|-----------|-----------|-------|
| google/gemini-3.5-flash | $0 | ~$0.01 | flagship-grade speed/quality, near-free |
| openai/gpt-5.4-mini | $0 | ~$0.01 | OpenAI mini tier, solid structured output |
| openai/gpt-5.4-nano | $0 | $0 | ultra-cheap, weaker reasoning |
| qwen/qwen3.6-27b | $0 | $0 | strong open-weight |
| deepseek/deepseek-v4-flash | $0 | $0 | (verify endpoint; flash line = cheap) |

## Tier 3 — FLAGSHIP (expensive, use sparingly)

| Model | In / MTok | Out / MTok | When to pay |
|-------|-----------|-----------|-------------|
| openai/gpt-5.4-pro | ~$0.03 | ~$0.18 | final gut-check, contracts, complex plans |
| anthropic/claude-sonnet-4.6 | $0 | ~$0.02 | best writing/tone, code review |
| anthropic/claude-opus-4.8 | ~$0.01 | ~$0.03 | hardest reasoning, agentic coding |
| google/gemini-3-pro-preview | — | — | multimodal depth |

## Live A/B on one idea ("SQL schema → ER diagram tool")

| Model | Verdict | Quality of reasoning |
|-------|---------|----------------------|
| **minimax-m3 (FREE)** | WATCH 8/10 ~7d | nuanced: buildable, distribution unclear |
| gpt-5.4-pro (FLAG) | SKIP 9/10 ~7d | decisive: crowded market |
| claude-sonnet-4.6 (FLAG) | SKIP 9/10 ~14d | clean, same conclusion |

**Takeaway:** on structured tasks (JSON eval, classify, summarize), a good free model is
**indistinguishable from flagship** for ~$0. Flagships differ on *judgment depth* (spotting
"crowded market") and *tone*, not on format. Pay for flagships on: final GO/NO-GO calls,
anything going to a client, or hard reasoning — not on bulk scanning.

## The rubric (when to use what)

1. **Bulk daily scan** (IdeaPulse eval, 25+/day) → `minimax-m3` free. Cost: $0.
2. **Nutshell / explain / classify** → free flash-tier. Cost: $0.
3. **Deep evaluation reports** → cheap tier (`gemini-3.5-flash` or `gpt-5.4-mini`). Cost: cents.
4. **Final decision before building** → 1 flagship call (`claude-sonnet-4.6` or `gpt-5.4-pro`). Cost: <$0.02.
5. **Client-facing writing / contracts** → flagship, always.
6. **LeadPilot live chat** → local `qwen3.5-hermes:9b` (₹0, private, no API).

Rule of thumb: **free for volume, cheap for quality, flagship for judgment.**
