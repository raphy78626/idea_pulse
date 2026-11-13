"""Tests for graph-backend preflight. No network, no models, no MiroFish.

History worth keeping: an earlier version of this check rejected the placeholder
ZEP_API_KEY outright and pointed at app.getzep.com. That was wrong. MiroFish runs
the Hermes *local* graph backend (install_local_graph.py), which overrides
get_zep_client() to use Graphiti+Kuzu and authenticates with LLM_API_KEY. The
placeholder exists only to satisfy upstream's `if not Config.ZEP_API_KEY` guards.
Completed simulations under that exact config prove it works, so these tests pin
the corrected rules:

  - local backend installed + LLM_API_KEY  -> OK (placeholder Zep is fine)
  - local backend missing + no real Zep key -> error naming both remedies
  - local backend installed + no LLM key    -> error naming LLM_API_KEY
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mirofish_bridge as b  # noqa: E402

ZEP_BLOCK = "# >>> hermes local graph backend (managed block, do not edit) >>>"


@pytest.fixture()
def envfile(tmp_path, monkeypatch):
    scratch = tmp_path / ".env"
    monkeypatch.setattr(b, "_env_file_candidates", lambda: [scratch])
    return scratch


@pytest.fixture()
def zep_py(tmp_path, monkeypatch):
    """Point the local-backend probe at a scratch zep.py."""
    p = tmp_path / "zep.py"
    monkeypatch.setattr(b, "_local_zep_path", lambda: p, raising=False)
    return p


def _patch_zep_path(monkeypatch, path):
    monkeypatch.setattr(b, "_local_zep_path", lambda: path, raising=False)


def test_local_backend_with_llm_key_is_fine(envfile, tmp_path, monkeypatch):
    """The documented working setup: placeholder Zep + local backend + LLM key."""
    envfile.write_text("ZEP_API_KEY=placeholder-key\nLLM_API_KEY=sk-or-" + "x" * 60 + "\n")
    z = tmp_path / "zep.py"
    z.write_text(ZEP_BLOCK + "\ndef get_zep_client(): ...\n")
    _patch_zep_path(monkeypatch, z)
    assert b.graph_backend_problem() == ""


def test_no_local_backend_and_no_real_zep_key(envfile, tmp_path, monkeypatch):
    envfile.write_text("ZEP_API_KEY=placeholder-key\n")
    z = tmp_path / "zep.py"
    z.write_text("def get_zep_client(): ...\n")     # no managed block
    _patch_zep_path(monkeypatch, z)
    msg = b.graph_backend_problem()
    assert "not installed" in msg
    assert "install_local_graph.py" in msg, "must offer the no-account fix first"
    assert "app.getzep.com" in msg, "must still name the paid alternative"


def test_no_local_backend_but_real_zep_key_is_fine(envfile, tmp_path, monkeypatch):
    envfile.write_text("ZEP_API_KEY=zep_" + "a" * 40 + "\n")
    z = tmp_path / "zep.py"
    z.write_text("def get_zep_client(): ...\n")
    _patch_zep_path(monkeypatch, z)
    assert b.graph_backend_problem() == ""


def test_local_backend_without_llm_key_is_reported(envfile, tmp_path, monkeypatch):
    envfile.write_text("ZEP_API_KEY=placeholder-key\n")   # no LLM_API_KEY
    z = tmp_path / "zep.py"
    z.write_text(ZEP_BLOCK + "\n")
    _patch_zep_path(monkeypatch, z)
    msg = b.graph_backend_problem()
    assert "LLM_API_KEY" in msg
    assert "app.getzep.com" not in msg, "Zep is irrelevant when the local backend is used"


def test_missing_zep_py_is_treated_as_not_installed(envfile, tmp_path, monkeypatch):
    envfile.write_text("ZEP_API_KEY=placeholder-key\n")
    _patch_zep_path(monkeypatch, tmp_path / "does-not-exist.py")
    assert "not installed" in b.graph_backend_problem()


def test_quoted_keys_are_unwrapped(envfile, tmp_path, monkeypatch):
    envfile.write_text('ZEP_API_KEY="placeholder"\nLLM_API_KEY="sk-or-' + "b" * 50 + '"\n')
    z = tmp_path / "zep.py"
    z.write_text(ZEP_BLOCK + "\n")
    _patch_zep_path(monkeypatch, z)
    assert b.graph_backend_problem() == ""


def test_later_env_file_is_used_when_first_lacks_key(tmp_path, monkeypatch):
    first, second = tmp_path / "a.env", tmp_path / "b.env"
    second.write_text("LLM_API_KEY=sk-or-" + "c" * 50 + "\n")
    monkeypatch.setattr(b, "_env_file_candidates", lambda: [first, second])
    z = tmp_path / "zep.py"
    z.write_text(ZEP_BLOCK + "\n")
    _patch_zep_path(monkeypatch, z)
    assert b.graph_backend_problem() == ""


def test_malformed_env_does_not_crash(envfile, tmp_path, monkeypatch):
    envfile.write_text("# comment\nZEP_API_KEY\nnot a kv\n")
    _patch_zep_path(monkeypatch, tmp_path / "nope.py")
    assert isinstance(b.graph_backend_problem(), str)


def test_preflight_reports_problem(monkeypatch):
    monkeypatch.setattr(b, "run_stage", lambda *a, **k: {"status": "ok"})
    monkeypatch.setattr(b, "graph_backend_problem", lambda: "backend broken")
    ok, hint = b.preflight()
    assert ok is False and "backend broken" in hint


def test_preflight_can_skip_graph_check(monkeypatch):
    monkeypatch.setattr(b, "run_stage", lambda *a, **k: {"status": "ok"})
    monkeypatch.setattr(b, "graph_backend_problem", lambda: "should not be consulted")
    ok, hint = b.preflight(check_graph=False)
    assert ok is True and hint == ""
