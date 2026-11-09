"""Tests for mirofish_bridge. No network, no model, no subprocess."""
import json

import mirofish_bridge


MIROFISH_REPORT = """# Future Forecast Report: Market Reaction

> The simulation predicts a polarized response.

## The Announced Idea and the Initial State

Apollo and ZoomInfo respond defensively.

## Key Forecasted Trends and Strategic Risks

Vendor lock-in is the central objection buyers raise.
"""


def test_accepts_mirofish_report_without_final_call_heading():
    # the deep-eval helper requires '## Final call'; MiroFish reports never have it
    assert mirofish_bridge.report_is_complete(MIROFISH_REPORT) is True


def test_rejects_empty_report():
    assert mirofish_bridge.report_is_complete("") is False


def test_rejects_short_report():
    assert mirofish_bridge.report_is_complete("# Hi\n\ntoo short") is False


def test_rejects_report_cut_off_mid_sentence():
    body = "# Report\n\n" + ("Sentence one is fine. " * 20) + "and then it stops mid"
    assert mirofish_bridge.report_is_complete(body) is False


def test_seed_includes_title_meta_and_source():
    seed = mirofish_bridge.build_seed("My idea", "some context", "Reddit")
    assert "My idea" in seed
    assert "some context" in seed
    assert "Reddit" in seed


def test_seed_unescapes_html_entities():
    seed = mirofish_bridge.build_seed("AT&amp;T app", "&quot;quoted&quot;", "HN")
    assert "AT&T app" in seed
    assert '"quoted"' in seed
    assert "&amp;" not in seed


def test_seed_tolerates_missing_meta():
    seed = mirofish_bridge.build_seed("Just a title", None, "Reddit")
    assert "Just a title" in seed
    assert len(seed) > 20


def test_seed_truncates_absurd_titles():
    seed = mirofish_bridge.build_seed("x" * 5000, "", "Reddit")
    assert len(seed) < 6000


def test_seed_includes_expansion_when_provided():
    seed = mirofish_bridge.build_seed("T", "m", "Reddit", expansion="Apollo, Clay, ZoomInfo")
    assert "Apollo, Clay, ZoomInfo" in seed


def test_seed_omits_expansion_header_when_absent():
    seed = mirofish_bridge.build_seed("T", "m", "Reddit")
    assert "Market participants" not in seed


def test_seed_includes_evaluation_excerpt():
    seed = mirofish_bridge.build_seed("T", "m", "Reddit", evaluation="Verdict: SKIP 2/10")
    assert "SKIP 2/10" in seed


GOOD_EXPANSION = """Problem: mid-market RevOps teams rebuild prospect lists every campaign.
Apollo.io - incumbent contact-data vendor.
Clay - newer enrichment workflow tool.
ZoomInfo - enterprise contact provider.
Mid-market RevOps managers - the buyer persona."""


def test_expand_idea_returns_text(monkeypatch):
    monkeypatch.setattr(mirofish_bridge, "_llm_chat", lambda s, u, **k: (GOOD_EXPANSION, "test-model"))
    text, model = mirofish_bridge.expand_idea("list building idea", "context")
    assert "Apollo.io" in text
    assert model == "test-model"


def test_expand_idea_falls_back_to_empty_on_llm_failure(monkeypatch):
    monkeypatch.setattr(mirofish_bridge, "_llm_chat", lambda s, u, **k: ("", "no model"))
    text, model = mirofish_bridge.expand_idea("t", "m")
    assert text == ""
    assert "no model" in model


def test_count_named_actors_counts_participant_lines():
    assert mirofish_bridge.count_named_actors(GOOD_EXPANSION) >= 3


def test_count_named_actors_handles_lowercase_words_in_names():
    text = "Market participants:\nRevenue operations manager - buyer\nHead of sales - buyer\nIT security reviewer - critic"
    assert mirofish_bridge.count_named_actors(text) == 3


def test_count_named_actors_handles_hyphenated_names():
    text = "Market participants:\nLinkedIn Sales Navigator - incumbent\nApollo.io - competitor\nOutreach - competitor"
    assert mirofish_bridge.count_named_actors(text) == 3


def test_count_named_actors_ignores_problem_and_header_lines():
    text = ("Problem: mid-market RevOps teams rebuild prospect lists every campaign.\n"
            "Market participants:\n"
            "Apollo.io - competitor\n")
    assert mirofish_bridge.count_named_actors(text) == 1


def test_count_named_actors_ignores_prose_only():
    prose = "the problem is that teams spend a week rebuilding lists every campaign"
    assert mirofish_bridge.count_named_actors(prose) == 0


def test_quality_gate_passes_with_enough_actors():
    ok, reason = mirofish_bridge.check_seed_quality(GOOD_EXPANSION)
    assert ok is True
    assert reason == ""


def test_quality_gate_rejects_thin_seed():
    ok, reason = mirofish_bridge.check_seed_quality("just some prose about a problem")
    assert ok is False
    assert "named" in reason.lower()


def test_cli_path_points_at_foresight_skill():
    p = mirofish_bridge.cli_path()
    assert p.name == "mirofish.py"
    assert "foresight" in str(p)


def test_parse_envelope_reads_success():
    out = json.dumps({"ok": True, "project_id": "proj_1"})
    data = mirofish_bridge.parse_envelope(out)
    assert data["project_id"] == "proj_1"


def test_parse_envelope_raises_on_ok_false():
    out = json.dumps({"ok": False, "error": "cannot reach MiroFish at http://x"})
    try:
        mirofish_bridge.parse_envelope(out)
    except mirofish_bridge.BridgeError as e:
        assert "cannot reach MiroFish" in str(e)
    else:
        raise AssertionError("expected BridgeError")


def test_parse_envelope_raises_on_garbage():
    try:
        mirofish_bridge.parse_envelope("not json at all")
    except mirofish_bridge.BridgeError as e:
        assert "unreadable" in str(e).lower()
    else:
        raise AssertionError("expected BridgeError")


def test_parse_envelope_ignores_leading_stderr_noise():
    out = "some warning line\n" + json.dumps({"ok": True, "characters": 5})
    assert mirofish_bridge.parse_envelope(out)["characters"] == 5


def test_run_stage_always_passes_lang_en(monkeypatch):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd

        class R:
            returncode = 0
            stdout = json.dumps({"ok": True})
            stderr = ""
        return R()

    monkeypatch.setattr(mirofish_bridge.subprocess, "run", fake_run)
    mirofish_bridge.run_stage(["health"])
    assert "--lang" in seen["cmd"]
    assert "en" in seen["cmd"]


def test_preflight_reports_server_down_with_remedy(monkeypatch):
    def boom(args, timeout=0):
        raise mirofish_bridge.BridgeError("cannot reach MiroFish at http://127.0.0.1:5001")
    monkeypatch.setattr(mirofish_bridge, "run_stage", boom)
    ok, hint = mirofish_bridge.preflight()
    assert ok is False
    assert "mirofish_server.sh" in hint


def test_preflight_passes_when_health_ok(monkeypatch):
    monkeypatch.setattr(mirofish_bridge, "run_stage", lambda args, timeout=0: {"ok": True})
    ok, hint = mirofish_bridge.preflight()
    assert ok is True
    assert hint == ""


def test_run_simulation_reports_progress_stages(monkeypatch, tmp_path):
    calls = []

    def fake_run(args, timeout=0):
        calls.append(args[0])
        if args[0] == "ontology":
            return {"ok": True, "project_id": "proj_x"}
        if args[0] == "build":
            return {"ok": True, "graph_id": "g1", "node_count": 6}
        if args[0] == "simulate":
            return {"ok": True, "simulation_id": "sim_x", "rounds_run": 12}
        if args[0] == "report":
            return {"ok": True, "saved_to": str(tmp_path / "r.md"), "characters": 5000}
        return {"ok": True}

    monkeypatch.setattr(mirofish_bridge, "run_stage", fake_run)
    monkeypatch.setattr(mirofish_bridge, "expand_idea", lambda t, m: ("Apollo.io - vendor\nClay - tool\nZoomInfo - vendor", "m"))
    steps = []
    result = mirofish_bridge.run_simulation(
        "title", "meta", "Reddit",
        on_stage=lambda pct, label: steps.append(pct),
        out_path=tmp_path / "r.md")
    assert result["simulation_id"] == "sim_x"
    # health runs first as the preflight check
    assert calls == ["health", "ontology", "build", "simulate", "report"]
    assert steps == sorted(steps), "progress must never go backwards"
    assert steps[-1] == 100


def test_run_simulation_refuses_thin_seed(monkeypatch):
    monkeypatch.setattr(mirofish_bridge, "run_stage", lambda args, timeout=0: {"ok": True})
    monkeypatch.setattr(mirofish_bridge, "expand_idea", lambda t, m: ("prose only, no actors", "m"))
    try:
        mirofish_bridge.run_simulation("t", "m", "Reddit", out_path=None)
    except mirofish_bridge.BridgeError as e:
        assert "named market participant" in str(e)
    else:
        raise AssertionError("expected the quality gate to reject")


def test_run_simulation_surfaces_stage_error(monkeypatch):
    monkeypatch.setattr(mirofish_bridge, "expand_idea", lambda t, m: ("A - x\nB - y\nC - z", "m"))

    def fake_run(args, timeout=0):
        if args[0] == "health":
            return {"ok": True}
        if args[0] == "ontology":
            return {"ok": True, "project_id": "proj_x"}
        raise mirofish_bridge.BridgeError("graph build failed: model returned no JSON")
    monkeypatch.setattr(mirofish_bridge, "run_stage", fake_run)
    try:
        mirofish_bridge.run_simulation("t", "m", "Reddit", out_path=None)
    except mirofish_bridge.BridgeError as e:
        assert "no JSON" in str(e)
    else:
        raise AssertionError("stage errors must propagate")


def test_run_simulation_turns_zep_401_into_install_hint(monkeypatch):
    def boom(args, timeout=0):
        raise mirofish_bridge.BridgeError("status_code: 401, body: unauthorized")
    monkeypatch.setattr(mirofish_bridge, "run_stage", boom)
    try:
        mirofish_bridge.run_simulation("t", "m", "Reddit", out_path=None)
    except mirofish_bridge.BridgeError as e:
        assert "install_local_graph.py" in str(e)
    else:
        raise AssertionError("expected a preflight failure")


def test_read_report_returns_text(tmp_path):
    p = tmp_path / "r.md"
    p.write_text("# Report\n\n" + ("Body sentence. " * 30), encoding="utf-8")
    assert "Body sentence" in mirofish_bridge.read_report(str(p))


def test_read_report_returns_empty_for_missing_file(tmp_path):
    assert mirofish_bridge.read_report(str(tmp_path / "nope.md")) == ""


def test_read_report_returns_empty_for_truncated(tmp_path):
    p = tmp_path / "r.md"
    p.write_text("# Report\n\n" + ("Body. " * 40) + "cut mid", encoding="utf-8")
    assert mirofish_bridge.read_report(str(p)) == ""


def test_read_report_returns_empty_for_none_path():
    assert mirofish_bridge.read_report(None) == ""


def test_accepts_complete_chinese_report():
    body = "# 报告\n\n" + ("这是一段足够长的正文内容。" * 20) + "最后一句也正常结束。"
    assert mirofish_bridge.report_is_complete(body) is True


def test_rejects_truncated_chinese_report():
    body = "# 报告\n\n" + ("这是一段足够长的正文内容。" * 20) + "然后突然就断在"
    assert mirofish_bridge.report_is_complete(body) is False


def test_accepts_report_ending_in_other_cjk_punctuation():
    for terminator in ("\u3002", "\uff01", "\uff1f", "\u201d", "\u300d"):
        body = ("# Report\n\n" + ("A full sentence here. " * 20)).rstrip() + terminator
        assert mirofish_bridge.report_is_complete(body) is True, terminator


def test_is_transient_report_rejection_matches_known_409():
    msg = "Simulation or Zep graph ingestion is still active; wait for a terminal run status"
    assert mirofish_bridge.is_transient_report_rejection(msg) is True


def test_is_transient_report_rejection_ignores_other_errors():
    assert mirofish_bridge.is_transient_report_rejection("graph build failed: no JSON") is False
    assert mirofish_bridge.is_transient_report_rejection("") is False


def test_report_stage_retries_until_simulation_settles(monkeypatch, tmp_path):
    sleeps = []
    monkeypatch.setattr(mirofish_bridge, "_sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(mirofish_bridge, "expand_idea", lambda t, m: ("A - x\nB - y\nC - z", "m"))
    state = {"report_calls": 0}

    def fake_run(args, timeout=0):
        name = args[0]
        if name == "health":
            return {"ok": True}
        if name == "ontology":
            return {"ok": True, "project_id": "proj_x"}
        if name == "build":
            return {"ok": True, "graph_id": "g1"}
        if name == "simulate":
            return {"ok": True, "simulation_id": "sim_x"}
        if name == "report":
            state["report_calls"] += 1
            if state["report_calls"] < 3:
                raise mirofish_bridge.BridgeError(
                    "Simulation or Zep graph ingestion is still active")
            return {"ok": True, "saved_to": str(tmp_path / "r.md")}
        return {"ok": True}

    monkeypatch.setattr(mirofish_bridge, "run_stage", fake_run)
    result = mirofish_bridge.run_simulation("t", "m", "Reddit", out_path=tmp_path / "r.md")
    assert result["report_path"] == str(tmp_path / "r.md")
    assert state["report_calls"] == 3
    assert len(sleeps) == 2, "should wait between the two failed attempts"


def test_report_stage_does_not_retry_real_errors(monkeypatch, tmp_path):
    monkeypatch.setattr(mirofish_bridge, "_sleep", lambda s: None)
    monkeypatch.setattr(mirofish_bridge, "expand_idea", lambda t, m: ("A - x\nB - y\nC - z", "m"))
    calls = {"n": 0}

    def fake_run(args, timeout=0):
        if args[0] == "health":
            return {"ok": True}
        if args[0] == "ontology":
            return {"ok": True, "project_id": "proj_x"}
        if args[0] == "build":
            return {"ok": True, "graph_id": "g1"}
        if args[0] == "simulate":
            return {"ok": True, "simulation_id": "sim_x"}
        calls["n"] += 1
        raise mirofish_bridge.BridgeError("report generation blew up for an unrelated reason")

    monkeypatch.setattr(mirofish_bridge, "run_stage", fake_run)
    try:
        mirofish_bridge.run_simulation("t", "m", "Reddit", out_path=tmp_path / "r.md")
    except mirofish_bridge.BridgeError as e:
        assert "unrelated reason" in str(e)
    else:
        raise AssertionError("expected the error to propagate")
    assert calls["n"] == 1, "non-transient errors must not be retried"


def test_sanitize_removes_tool_call_blocks():
    md = "# R\n\nReal text.\n\n<tool_call>\n<thinking>hmm</thinking>\n</tool_call>\n\nMore real text.\n"
    out = mirofish_bridge.sanitize_report(md)
    assert "<tool_call>" not in out
    assert "<thinking>" not in out
    assert "Real text." in out
    assert "More real text." in out


def test_sanitize_removes_thinking_blocks():
    md = "# R\n\nBefore.\n<thinking>internal monologue</thinking>\nAfter.\n"
    out = mirofish_bridge.sanitize_report(md)
    assert "internal monologue" not in out
    assert "Before." in out and "After." in out


def test_sanitize_removes_thought_paragraphs():
    md = ("# R\n\n## Section\n\n"
          "Thought: I have now used three tools. I will try quick_search next.\n\n"
          "The actual finding is that buyers resent manual list building.\n")
    out = mirofish_bridge.sanitize_report(md)
    assert "Thought:" not in out
    assert "three tools" not in out
    assert "buyers resent manual list building" in out


def test_sanitize_preserves_normal_markdown():
    md = ("# Title\n\n> Quote line.\n\n## Section\n\n- bullet one\n- bullet two\n\n"
          "| a | b |\n|---|---|\n| 1 | 2 |\n\nFinal sentence.\n")
    assert mirofish_bridge.sanitize_report(md) == md


def test_sanitize_handles_empty_and_none():
    assert mirofish_bridge.sanitize_report("") == ""
    assert mirofish_bridge.sanitize_report(None) == ""


def test_read_report_sanitizes(tmp_path):
    p = tmp_path / "r.md"
    p.write_text("# R\n\nGood text.\n\n<tool_call>junk</tool_call>\n\n" + ("More text. " * 30),
                 encoding="utf-8")
    out = mirofish_bridge.read_report(str(p))
    assert "<tool_call>" not in out
    assert "Good text." in out
