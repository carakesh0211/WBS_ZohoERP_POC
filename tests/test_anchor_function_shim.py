"""The Catalyst Cron Function shim for the daily audit anchor (Fable 5.1).

The writer shipped correct, tested and called by nothing (docs/runbooks/
audit-anchor.md). `catalyst/functions/capex_audit_anchor/main.py` is the
scheduled caller; these tests import it with a fake Catalyst context and
prove, without a database:

* the handler calls the SAME entry point the CLI calls
  (`app.backend.jobs.anchor_entry.run_from_environment`), passing the
  schedule's checkpoint through;
* the entry point reads the job token from the environment BY NAME and the
  handler never logs or returns it;
* SUCCEEDED closes with success; PARTIAL / TAMPER / FAILED close with failure
  and the PARTIAL result carries the checkpoint for the next tick;
* an exception in the job is reported by CLASS name only (a driver message
  can echo the DSN) and closes with failure;
* the CLI (`tools/anchor_job.py`) delegates to the same function;
* the function folder's catalyst-config.json is a Python cron pointing at
  main.py, and the builder refuses an output inside the repository.
"""
from __future__ import annotations

import importlib.util
import json
import logging
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.backend.jobs import anchor_entry, audit_anchor  # noqa: E402

FUNCTION_MAIN = ROOT / "catalyst" / "functions" / "capex_audit_anchor" / "main.py"


class _Context:
    def __init__(self):
        self.closed = None

    def close_with_success(self):
        self.closed = "success"

    def close_with_failure(self):
        self.closed = "failure"


class _Cron:
    def __init__(self, params=None):
        self._params = params

    def get_all_cron_params(self):
        return self._params


def _load_handler():
    spec = importlib.util.spec_from_file_location("capex_audit_anchor_main", FUNCTION_MAIN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run(outcome, *, anchor_date="2026-09-11", phases=("ANCHOR",)):
    return audit_anchor.AnchorJobRun(outcome=outcome, anchor_date=anchor_date,
                                     phases_completed=tuple(phases))


@pytest.fixture()
def calls(monkeypatch):
    seen = []

    def fake(checkpoint=None):
        seen.append(checkpoint)
        return fake.result
    fake.result = _run(audit_anchor.OUTCOME_SUCCEEDED)
    monkeypatch.setattr(anchor_entry, "run_from_environment", fake)
    return seen, fake


def test_success_closes_with_success_and_returns_the_redacted_result(calls, caplog):
    seen, fake = calls
    mod = _load_handler()
    ctx = _Context()
    with caplog.at_level(logging.INFO, logger="capex.anchor_function"):
        out = mod.handler(_Cron(), ctx)
    assert ctx.closed == "success"
    assert out["job"] == "audit_anchor" and out["outcome"] == "SUCCEEDED"
    assert seen == [None]


@pytest.mark.parametrize("outcome", [audit_anchor.OUTCOME_PARTIAL, audit_anchor.OUTCOME_TAMPER_DETECTED,
                                     audit_anchor.OUTCOME_FAILED_RETRYABLE, audit_anchor.OUTCOME_FAILED_PERMANENT])
def test_every_non_success_outcome_closes_with_failure(calls, outcome):
    seen, fake = calls
    fake.result = _run(outcome)
    mod = _load_handler()
    ctx = _Context()
    out = mod.handler(_Cron(), ctx)
    assert ctx.closed == "failure"
    assert out["outcome"] == outcome
    # `as_dict()` already carries the job's own checkpoint; the shim guarantees
    # it for PARTIAL, which is the outcome the next tick resumes from.
    if outcome == audit_anchor.OUTCOME_PARTIAL:
        assert out["checkpoint"] == {"anchor_date": "2026-09-11", "phases_completed": ["ANCHOR"]}


def test_the_schedules_checkpoint_is_passed_through_as_a_dict_or_json(calls):
    seen, fake = calls
    mod = _load_handler()
    cp = {"anchor_date": "2026-09-10", "phases_completed": ["ANCHOR"]}
    mod.handler(_Cron({"checkpoint": cp}), _Context())
    mod.handler(_Cron({"checkpoint": json.dumps(cp)}), _Context())
    mod.handler(_Cron({"checkpoint": "not json"}), _Context())
    mod.handler(_Cron(None), _Context())
    assert seen == [cp, cp, None, None]


def test_an_exception_is_reported_by_class_only_and_closes_with_failure(monkeypatch, caplog):
    def boom(checkpoint=None):
        raise ConnectionError("postgresql://capex:SECRET@host/db refused")
    monkeypatch.setattr(anchor_entry, "run_from_environment", boom)
    mod = _load_handler()
    ctx = _Context()
    with caplog.at_level(logging.ERROR, logger="capex.anchor_function"):
        out = mod.handler(_Cron(), ctx)
    assert ctx.closed == "failure"
    assert out == {"job": "audit_anchor", "outcome": "FAILED_RETRYABLE", "error_class": "ConnectionError"}
    assert "SECRET" not in caplog.text and "postgresql://" not in caplog.text


def test_the_token_is_read_by_name_from_the_environment_and_never_logged(monkeypatch, caplog):
    captured = {}

    class _DB:
        def __init__(self, cfg, secret_provider=None):
            captured["provider"] = secret_provider

        def close(self):
            captured["closed"] = True

    class _Cfg:
        pass
    from app.backend.pg import config as pg_config
    monkeypatch.setenv(audit_anchor.ANCHOR_JOB_TOKEN_SECRET, "tok-xyz-should-not-appear")
    monkeypatch.setenv("CAPEX_DB_HOST", "example.invalid")
    monkeypatch.setenv("CAPEX_DB_PASSWORD", "pw-should-not-appear")
    monkeypatch.setattr(pg_config, "from_env", lambda prefix="CAPEX_DB_": _Cfg())
    import app.backend.pg.engine as engine_mod
    monkeypatch.setattr(engine_mod, "Database", _DB)

    def fake_job(database, *, credential, secrets, checkpoint):
        captured["credential"] = credential
        captured["checkpoint"] = checkpoint
        return _run(audit_anchor.OUTCOME_SUCCEEDED)
    monkeypatch.setattr(anchor_entry, "run_anchor_job", fake_job)
    mod = _load_handler()
    ctx = _Context()
    with caplog.at_level(logging.DEBUG):
        out = mod.handler(_Cron({"checkpoint": {"anchor_date": "2026-09-10", "phases_completed": []}}), ctx)
    assert captured["credential"] == "tok-xyz-should-not-appear"
    assert captured["checkpoint"] == {"anchor_date": "2026-09-10", "phases_completed": []}
    assert captured["closed"] is True
    assert ctx.closed == "success"
    text = caplog.text + json.dumps(out)
    assert "tok-xyz" not in text and "pw-should-not-appear" not in text


def test_the_cli_delegates_to_the_same_entry_point(monkeypatch):
    sys.path.insert(0, str(ROOT / "tools"))
    import anchor_job as cli
    seen = {}

    def fake(checkpoint=None):
        seen["checkpoint"] = checkpoint
        return _run(audit_anchor.OUTCOME_SUCCEEDED)
    monkeypatch.setattr(anchor_entry, "run_from_environment", fake)
    assert cli.main(["--quiet"]) == 0
    assert seen == {"checkpoint": None}


def test_the_function_configuration_is_a_python_cron_over_main_py():
    cfg = json.loads((FUNCTION_MAIN.parent / "catalyst-config.json").read_text(encoding="utf-8"))
    assert cfg["deployment"]["type"] == "cron"
    assert cfg["deployment"]["stack"].startswith("python_3_13")
    assert cfg["execution"]["main"] == "main.py"
    assert cfg["execution"]["timeout"] <= 900
    project = json.loads((ROOT / "catalyst" / "catalyst.json").read_text(encoding="utf-8"))
    assert "capex_audit_anchor" in project["functions"]["targets"]


def test_the_builder_refuses_an_output_inside_the_repository(tmp_path):
    sys.path.insert(0, str(ROOT / "tools" / "appsail"))
    import build_anchor_function as baf
    import build_uat_bundle as b
    with pytest.raises(b.BuildFailed, match="inside the repository"):
        baf.build(tmp_path, ROOT / "catalyst" / "should-not-exist")
    assert not (ROOT / "catalyst" / "should-not-exist").exists()
