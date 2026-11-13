# IdeaPulse simulation wiring

*Written 2026-11-09 after building and live-testing the MiroFish integration.*

> **Paused 2026-11-13.** `app.py simulate 3` was removed from the 9:00 cron line;
> the code, routes and the 🎭 button still work on demand. Reason: simulated
> reactions have never been compared with real outreach replies, so they must not
> gate ideas yet. Resume once a few ideas have real contacted → replied numbers to
> check the simulations against. To restore, add
> `; <python> app.py simulate 3 >> /tmp/ideapulse.log 2>&1` back before `; open …`.

---

## What runs, and when

Three stages, deliberately separate so a slow expensive step cannot break the others.

| Stage | Trigger | Writes | Time |
|---|---|---|---|
| Fetch | cron 09:00 | `items` rows | ~60s |
| Evaluate | cron 09:00, `limit=25` | `verdict`, `buildability`, `ttm_days` | ~20s/item |
| Simulate | cron 09:00, `limit=3` | `sim_path`, `sim_attempts`, `sim_error` | 6–11 min/item |

Cron line (all three stages, `;` separators so a failure in one does not skip the next):

```
0 9 * * * cd .../idea_pulse && python app.py fetch >> log 2>&1 && \
  python app.py evaluate 25 >> log 2>&1 ; \
  python app.py simulate 3 >> log 2>&1 ; \
  open http://localhost:8300
```

`fetch` and `evaluate` are chained with `&&` because evaluating before fetching is pointless. `simulate` and `open` use `;` because a failed simulation must not stop the dashboard from opening.

## Which ideas get simulated

`pending_simulations(limit=3)` selects on four conditions:

1. `verdict='BUILD'` — only high-conviction ideas
2. `source IN ('Reddit business ideas', 'Ask HN ideas', 'SomebodyMakeThis')` — **the whitelist**
3. `COALESCE(sim_attempts,0) < 2` — bounded retries
4. `sim_path IS NULL OR sim_path=''` — not already done

### Why the source whitelist exists

Measured on the live database: 13 `BUILD` verdicts, of which only **1** was an actual described problem. The rest were launch announcements and news:

```
karpathy/nanochat: Training-free language model modification
I just topped 60 paid users!
Outlook.com Is Quietly Rejecting Email from Domains Without DMARC
Show HN: Raw Screen Recordings into Cinematic Apple-Style Previews
```

Simulating "how would buyers react to this launch post" is meaningless — there is no idea in it. `PLAN_FIRST_DOLLAR.md` already diagnosed the cause: *"The sources are launch feeds, not pain feeds."* The whitelist is the cheapest correct filter and needs no extra model call.

Result: **13 BUILD → 1 eligible.** That is the whitelist working, not a bug.

## Failure behaviour

| Failure | Behaviour |
|---|---|
| MiroFish unreachable | `preflight()` fails; **0 attempts burned**; non-empty error; CLI exits 1 |
| Seed too thin (<3 named actors) | `BridgeError`; `sim_attempts` +1; `sim_error` records why |
| Report empty or truncated | Treated as failure; not cached; `sim_attempts` +1 |
| Whole pass exceeds `max_seconds` | Remaining items skipped; work already done is kept |
| Item reaches 2 attempts | Never selected again |

The attempt counter increments on **both** success and failure, so a permanently broken item stops consuming a 6-minute slot every morning.

## Cost control

- `limit=3` per run, so at most ~33 minutes of simulation per day
- `max_seconds=1800` wall-clock cap, **including failed attempts**
- Whitelist means the budget goes to ideas, not announcements

Current real cost: 1 eligible idea, so one simulation per day until more whitelisted items appear.

## Testing

```bash
cd ~/Project/Project_evaluators/idea_pulse
/usr/bin/python3 -m pytest tests/ -q          # 62 passed

# What would run, without spending money or time
/usr/bin/python3 app.py simulate --dry-run

# Manual trigger (respects the same caps)
/usr/bin/python3 app.py simulate 3
```

Tests never run a real simulation: `run_simulation` and `time.time` are monkeypatched.

## Known limitations

- **Report quality varies 1.5k–30k chars.** Depends on how well seed expansion names the market. The sanitizer strips leaked model reasoning, but cannot add substance.
- **MiroFish has `RunAtLoad: false`.** After a reboot it stays down until started; the 09:00 run then reports a clean error and burns no attempts. Run `mirofish_server.sh start` to restore.
- **Only 1 idea currently qualifies.** Raising the eligible count means fixing the source mix upstream (`PLAN_FIRST_DOLLAR.md` §1: Reddit has been dead since Oct 13), not loosening the whitelist.
- **No ranking feedback.** Simulation output does not yet influence `VERDICT_ORDER`. Deferred until there is enough data to know what signal to trust.
