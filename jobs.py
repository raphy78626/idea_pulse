"""Background jobs with progress and ETA, for IdeaPulse's slow operations.

The expensive calls (deep evaluate, evaluate-many, fetch, scrape, simulate)
block for 1-3 minutes. A browser can't show progress on a single blocking
request, so each one is moved into a thread here and the page polls /jobs/<id>.

Usage:
    jid = jobs.start("Deep evaluate", total=3, fn=lambda tick, label: work(tick, label))
    # then GET /jobs/<jid> -> {"state": "running", "done": 1, "total": 3,
    #                          "pct": 33, "eta": 84.0, "eta_confidence": "fair", ...}

`total=0` means indeterminate: the UI shows stripes instead of a filled bar.

ETA design
----------
Per-item times in this app are wildly uneven: a free-model evaluation is 5-20s
when a model answers and 90s+ when the chain has to fall back, and a MiroFish
simulation spends 2 minutes in one stage and 4 in the next. So:

  - ETA is computed from observed throughput (done / elapsed), which
    self-corrects as the job runs, rather than from a fixed guess.
  - It is withheld (None) until at least two items are done, because a single
    sample is a guess dressed up as a number.
  - Confidence is reported explicitly: "low" (<3 samples), "fair" (<8), "good".
  - The first item of a job is excluded from the rate until the second
    completes, since warm-up (model discovery, graph load) inflates it.

Reporting a wrong ETA is worse than reporting none, so the UI only shows one
when `eta` is not None and labels it as an estimate.
"""
import threading
import time
import uuid

_lock = threading.Lock()
_jobs = {}
KEEP_S = 600        # how long a finished job stays pollable
MAX_JOBS = 40       # cap so a long session can't grow unbounded

# ETA needs this many completed steps before it is shown at all.
MIN_SAMPLES = 2
# Smoothing: weight given to the newest interval. Lower = steadier ETA.
ALPHA = 0.4


def start(label, total, fn, expect_s=None):
    """Run fn(tick, label) in a daemon thread. Returns the job id.

    tick(n) advances the counter; label(text) replaces the status line.
    Whatever fn returns is exposed as result (stringified for safety).

    expect_s: optional (low, high) typical duration in seconds. Used for
    single-shot jobs (deep eval, one simulation) where a per-step rate cannot
    produce an ETA — the UI shows "typically 40-90s" against elapsed instead.
    """
    jid = uuid.uuid4().hex[:12]
    job = {
        "id": jid, "label": label, "done": 0, "total": max(0, int(total)),
        "state": "running", "message": "", "started": time.time(),
        "ended": 0.0, "result": "", "expect_s": expect_s,
        # timing for ETA
        "step_started": time.time(),   # when the current step began
        "interval": 0.0,               # smoothed seconds per step
        "samples": 0,                  # completed steps with a usable interval
    }
    with _lock:
        _jobs[jid] = job
        _prune_locked()

    def tick(n=1):
        now = time.time()
        with _lock:
            if job["total"]:
                new_done = min(job["total"], job["done"] + n)
                steps = new_done - job["done"]
                job["done"] = new_done
            else:
                steps = n
                job["done"] += n
            if steps > 0:
                # one interval per tick call, regardless of how many steps it moved
                dt = now - job["step_started"]
                job["step_started"] = now
                per_step = dt / steps
                if job["samples"] == 0:
                    # first interval includes warm-up; seed with it but do not
                    # trust it as a stable rate yet
                    job["interval"] = per_step
                else:
                    job["interval"] = (ALPHA * per_step
                                       + (1 - ALPHA) * job["interval"])
                job["samples"] += steps

    def set_label(text):
        with _lock:
            job["label"] = str(text)[:180]

    def run():
        try:
            res = fn(tick, set_label)
            with _lock:
                job["state"] = "done"
                job["result"] = "" if res is None else str(res)[:400]
                if job["total"]:
                    job["done"] = job["total"]
        except Exception as e:                      # never kill the server thread
            with _lock:
                job["state"] = "error"
                job["message"] = f"{type(e).__name__}: {e}"[:300]
        finally:
            with _lock:
                job["ended"] = time.time()

    threading.Thread(target=run, daemon=True, name=f"job-{jid}").start()
    return jid


def _prune_locked():
    now = time.time()
    dead = [k for k, j in _jobs.items()
            if j["ended"] and now - j["ended"] > KEEP_S]
    for k in dead:
        _jobs.pop(k, None)
    if len(_jobs) > MAX_JOBS:
        finished = sorted((j["ended"] or now, k) for k, j in _jobs.items() if j["ended"])
        for _, k in finished[:len(_jobs) - MAX_JOBS]:
            _jobs.pop(k, None)


def get(jid):
    with _lock:
        j = _jobs.get(jid)
        if not j:
            return None
        out = dict(j)
    now = time.time()
    elapsed = (out["ended"] or now) - out["started"]
    out["elapsed"] = round(elapsed, 1)
    total, done = out["total"], out["done"]
    out["pct"] = round(done * 100 / total) if total else 0

    # ETA: only when we have a real rate to extrapolate from.
    eta = None
    confidence = "none"
    remaining = max(0, total - done) if total else 0
    if (out["state"] == "running" and total and remaining
            and out["samples"] >= MIN_SAMPLES and out["interval"] > 0):
        eta = out["interval"] * remaining          # smoothed per-step interval
        # bound it by observed throughput so one slow step cannot triple it
        throughput_eta = (elapsed / done) * remaining if done else None
        if throughput_eta and eta > throughput_eta * 2.5:
            eta = throughput_eta
        eta = round(eta, 1)
        confidence = ("low" if out["samples"] < 3 else
                      "fair" if out["samples"] < 8 else "good")
    out["eta"] = eta
    out["eta_confidence"] = confidence
    out["remaining"] = remaining
    out["expect_s"] = out.get("expect_s")
    # For single-shot jobs (no per-step rate available) fall back to the
    # declared typical range, but only while running and only as a range.
    if eta is None and out["state"] == "running" and out["expect_s"]:
        low, high = out["expect_s"]
        out["eta_range"] = [low, high]
    else:
        out["eta_range"] = None
    return out


def running():
    with _lock:
        return [dict(j) for j in _jobs.values() if j["state"] == "running"]


# ---------------------------------------------------------------- front-end
# One copy of the widget, injected into every page so behaviour is identical.

WIDGET = """
<div id="jobbox" style="display:none">
  <div class="jb-head"><span id="jb-label">working…</span><span id="jb-pct"></span></div>
  <div class="jb-track"><div class="jb-fill" id="jb-fill"></div></div>
  <div class="jb-foot">
    <span id="jb-count"></span>
    <span id="jb-times"><span id="jb-eta"></span><span id="jb-time"></span></span>
  </div>
</div>
<style>
#jobbox{position:fixed;right:18px;bottom:18px;z-index:9999;width:330px;max-width:calc(100vw - 36px);
  background:#111b21;border:1px solid #2a3c47;border-radius:13px;padding:13px 15px;
  box-shadow:0 10px 34px rgba(0,0,0,.55);font-family:-apple-system,'Segoe UI',Roboto,sans-serif}
#jobbox.err{border-color:#7f1d1d}
.jb-head{display:flex;justify-content:space-between;gap:10px;font-size:13px;color:#e9edef;font-weight:600}
#jb-pct{color:#4ade80;font-variant-numeric:tabular-nums}
#jobbox.err #jb-pct{color:#f87171}
.jb-track{height:7px;background:#0b141a;border:1px solid #1d2a33;border-radius:5px;margin:9px 0 7px;overflow:hidden}
.jb-fill{height:100%;width:0;background:linear-gradient(90deg,#10b981,#4ade80);
  transition:width .35s ease;border-radius:5px}
#jobbox.indet .jb-fill{width:100%!important;
  background:repeating-linear-gradient(90deg,#10b981 0 14px,#0b141a 14px 28px);
  animation:jbslide 1s linear infinite}
@keyframes jbslide{from{background-position:0 0}to{background-position:28px 0}}
.jb-foot{display:flex;justify-content:space-between;font-size:11.5px;color:#667781;
  font-variant-numeric:tabular-nums;gap:8px}
#jb-times{display:flex;gap:8px;align-items:center}
#jb-eta{color:#7dd3fc;font-weight:600}
#jb-eta.low{color:#94a3b8;font-weight:500}
#jb-eta.fair{color:#7dd3fc}
#jb-eta.good{color:#4ade80;font-weight:700}
#jobbox.done .jb-fill{background:#4ade80}
#jobbox.done{border-color:#14532d}
</style>
<script>
(function(){
  const box = document.getElementById('jobbox');
  let timer = null, started = 0, onDone = null;
  function fmt(s){
    s = Math.max(0, s);
    if (s < 60) return Math.round(s) + 's';
    const m = Math.floor(s / 60), r = Math.round(s % 60);
    return m + 'm ' + String(r).padStart(2,'0') + 's';
  }
  window.JobBar = {
    watch(jid, doneCb){
      onDone = doneCb || null;
      started = Date.now() / 1000;
      box.style.display = 'block';
      box.className = '';
      document.getElementById('jb-fill').style.width = '0%';
      document.getElementById('jb-label').textContent = 'starting…';
      document.getElementById('jb-pct').textContent = '';
      document.getElementById('jb-count').textContent = '';
      document.getElementById('jb-eta').textContent = '';
      document.getElementById('jb-time').textContent = '';
      clearInterval(timer);
      timer = setInterval(() => poll(jid), 600);
      poll(jid);
    },
    hide(){ clearInterval(timer); box.style.display = 'none'; }
  };
  async function poll(jid){
    let j;
    try { j = await (await fetch('/jobs/' + jid)).json(); }
    catch { return; }
    if (j.state === 'missing'){ JobBar.hide(); return; }
    const indet = !j.total;
    const running = j.state === 'running';
    box.classList.toggle('indet', indet && running);
    document.getElementById('jb-label').textContent = j.label || 'working…';
    document.getElementById('jb-pct').textContent = indet ? '' : j.pct + '%';
    if (!indet) document.getElementById('jb-fill').style.width = j.pct + '%';
    document.getElementById('jb-count').textContent =
      indet ? '' : (j.done + ' / ' + j.total);

    // ETA: only shown when the server has a rate worth trusting.
    const etaEl = document.getElementById('jb-eta');
    if (running && j.eta != null && j.remaining > 0){
      etaEl.textContent = '~' + fmt(j.eta) + ' left';
      etaEl.className = j.eta_confidence || 'low';
      etaEl.title = j.eta_confidence === 'good'
        ? 'Estimate from ' + j.done + ' completed steps'
        : 'Rough estimate — confidence ' + (j.eta_confidence || 'low');
    } else if (running && j.eta_range){
      // single-shot job: a declared range beats a fake precision
      const lo = fmt(j.eta_range[0]), hi = fmt(j.eta_range[1]);
      etaEl.textContent = 'usually ' + lo + '–' + hi;
      etaEl.className = 'low';
      etaEl.title = 'Typical duration for this operation, not a live estimate';
    } else if (running && !indet){
      etaEl.textContent = 'estimating…';
      etaEl.className = 'low';
      etaEl.title = 'Needs a couple of finished steps before it can estimate';
    } else {
      etaEl.textContent = '';
    }
    document.getElementById('jb-time').textContent = fmt(j.elapsed || 0);

    if (j.state === 'done' || j.state === 'error'){
      clearInterval(timer);
      const bad = j.state === 'error';
      box.className = bad ? 'err' : 'done';
      document.getElementById('jb-label').textContent =
        bad ? ('failed — ' + (j.message || 'unknown')) : (j.label || 'done');
      document.getElementById('jb-pct').textContent = bad ? '✕' : '✓';
      document.getElementById('jb-eta').textContent = '';
      if (onDone) onDone(j);
      setTimeout(() => { if (box.classList.contains('done') || box.classList.contains('err')) JobBar.hide(); },
                 bad ? 8000 : 2500);
    }
  }
})();
</script>
"""


def async_route_body(jid):
    return {"job": jid}
