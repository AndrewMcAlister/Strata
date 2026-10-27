#!/usr/bin/env python3
"""Tests for Scripts/Strata_Service.py (the Windows service wrapper).

These need no Administrator rights and do not touch the real service manager
except for one read-only lookup of a service name that cannot exist. The SCM
calls that change anything are exercised by hand on a real install.

Run: python -m pytest tools/test_service.py -q
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "Scripts"))

import Strata_Service as svc                                  # noqa: E402

WINDOWS = os.name == "nt"


def write_config(path: Path, *, model: str = "test-model", port: int | None = 8080,
                 context: int | None = 2048) -> Path:
    args = ["--native", "model.gguf"]
    if context is not None:
        args += ["--max-context", str(context)]
    data = {"exe": "engine.exe", "args": args, "model_name": model}
    if port is not None:
        data["port"] = port
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


# ------------------------------------------------------------------------------ service.conf reading

def test_parse_conf_skips_comments_and_blank_lines():
    values, problems = svc.parse_conf_text(
        "# a comment\n\n; another\nconfig=D:\\models\\a.json\nPORT = 9090\n")
    assert problems == []
    assert values == {"config": "D:\\models\\a.json", "port": "9090"}


def test_parse_conf_reports_a_line_without_equals():
    values, problems = svc.parse_conf_text("config=x\nthis line is wrong\n")
    assert values == {"config": "x"}
    assert len(problems) == 1
    assert "line 2" in problems[0]


def test_conf_values_reads_the_settings(tmp_path):
    config = write_config(tmp_path / "strata-a.json")
    conf = svc.conf_values(
        f"config={config}\nport=9091\nhost=0.0.0.0\nshutdown_timeout=45\n")
    assert conf["config"] == str(config)
    assert conf["port"] == 9091
    assert conf["host"] == "0.0.0.0"
    assert conf["shutdown_timeout"] == 45.0
    assert conf["unknown"] == []


def test_conf_values_lists_unknown_settings(tmp_path):
    config = write_config(tmp_path / "strata-a.json")
    conf = svc.conf_values(f"config={config}\ncolour=blue\n")
    assert conf["unknown"] == ["colour"]


def test_conf_values_needs_a_config_line():
    with pytest.raises(svc.ServiceError, match="no config="):
        svc.conf_values("# nothing here\n")


def test_conf_values_rejects_a_bad_port(tmp_path):
    config = write_config(tmp_path / "strata-a.json")
    with pytest.raises(svc.ServiceError, match="not a number"):
        svc.conf_values(f"config={config}\nport=eighty\n")
    with pytest.raises(svc.ServiceError, match="1..65535"):
        svc.conf_values(f"config={config}\nport=70000\n")


def test_conf_values_resolves_a_relative_config_against_the_strata_folder():
    conf = svc.conf_values("config=my-model.json\n")
    assert Path(conf["config"]) == (svc.ROOT / "my-model.json").resolve()


def test_load_conf_reports_a_missing_file(tmp_path):
    with pytest.raises(svc.ServiceError, match="is missing"):
        svc.load_conf(tmp_path / "absent.conf")


# ------------------------------------------------------------------------------ config discovery

def test_run_config_summary_reads_model_port_and_context(tmp_path):
    path = write_config(tmp_path / "strata-live.json", model="qwen-x", port=8081, context=65536)
    summary = svc.run_config_summary(path)
    assert summary == {"path": str(path), "model": "qwen-x", "port": 8081, "context": 65536}


def test_run_config_summary_rejects_json_that_is_not_a_run_config(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text('{"theme": "dark"}', encoding="utf-8")
    assert svc.run_config_summary(path) is None
    path.write_text("not json", encoding="utf-8")
    assert svc.run_config_summary(path) is None


def test_discover_models_lists_only_model_configs(tmp_path):
    write_config(tmp_path / "strata-b.json")
    write_config(tmp_path / "strata-a.json")
    write_config(tmp_path / "strata-a.shared-settings.json")
    (tmp_path / "strata-old.json.bak-20260101").write_text("{}", encoding="utf-8")
    (tmp_path / "strata-broken.json").write_text("{}", encoding="utf-8")
    found = [Path(m["path"]).name for m in svc.discover_models(tmp_path)]
    assert found == ["strata-a.json", "strata-b.json"]


def test_default_config_is_the_newest(tmp_path):
    older = write_config(tmp_path / "strata-older.json")
    newer = write_config(tmp_path / "strata-newer.json")
    os.utime(older, (1_600_000_000, 1_600_000_000))
    os.utime(newer, (1_700_000_000, 1_700_000_000))
    assert svc.default_config(tmp_path) == newer


def test_default_config_with_no_models_is_none(tmp_path):
    assert svc.default_config(tmp_path) is None


# ------------------------------------------------------------------------------ the written file

def test_render_conf_puts_the_chosen_model_on_the_config_line(tmp_path):
    chosen = write_config(tmp_path / "strata-chosen.json", model="chosen")
    other = write_config(tmp_path / "strata-other.json", model="other", port=9000)
    text = svc.render_conf(chosen, svc.discover_models(tmp_path))
    assert f"config={chosen}" in text
    assert f"#  {chosen}" in text
    assert "strata-other.json" in text
    assert "context 2048" in text
    assert "port 9000" in text


def test_render_conf_round_trips_through_the_reader(tmp_path):
    chosen = write_config(tmp_path / "strata-chosen.json")
    text = svc.render_conf(chosen, svc.discover_models(tmp_path))
    values, problems = svc.parse_conf_text(text)
    assert problems == []
    assert values["config"] == str(chosen)


def test_write_conf_writes_utf8_without_a_bom(tmp_path):
    chosen = write_config(tmp_path / "strata-chosen.json")
    target = tmp_path / "service.conf"
    svc.write_conf(target, chosen, svc.discover_models(tmp_path))
    raw = target.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert str(chosen).encode("utf-8") in raw


# ------------------------------------------------------------------------------ boot registration

def test_build_bin_path_quotes_paths_with_spaces(tmp_path):
    python = tmp_path / "Program Files" / "python.exe"
    script = tmp_path / "my scripts" / "Strata_Service.py"
    line = svc.build_bin_path(python, script, "Strata")
    assert line == f'"{python}" "{script}" run --name Strata'


def test_stable_python_prefers_the_archives_private_runtime(tmp_path):
    bundled = tmp_path / "python" / "python.exe"
    bundled.parent.mkdir(parents=True)
    bundled.write_bytes(b"")
    venv = tmp_path / ".venv" / "Scripts" / "python.exe"
    venv.parent.mkdir(parents=True)
    venv.write_bytes(b"")
    assert svc.stable_python(tmp_path) == bundled


def test_stable_python_falls_back_to_the_venv(tmp_path):
    venv = tmp_path / ".venv" / "Scripts" / "python.exe"
    venv.parent.mkdir(parents=True)
    venv.write_bytes(b"")
    assert svc.stable_python(tmp_path) == venv


# ------------------------------------------------------------------------------ the watchdog

@pytest.mark.parametrize("state,action", [
    ("ready", "wait"),
    ("starting", "wait"),
    ("failed", "stop-then-start"),
    ("stale", "stop-then-start"),
    ("stopped", "start"),
    ("not managed", "start"),
])
def test_decide_keeps_a_healthy_server_and_replaces_a_dead_one(state, action):
    assert svc.decide(state) == action


def test_tail_returns_the_end_of_a_log(tmp_path):
    log = tmp_path / "x.log"
    log.write_text("\n".join(f"line {n}" for n in range(200)), encoding="utf-8")
    assert svc._tail(log, lines=3) == ["line 197", "line 198", "line 199"]


def test_tail_of_a_missing_file_is_empty(tmp_path):
    assert svc._tail(tmp_path / "absent.log") == []


# ------------------------------------------------------------------------------ the service's state

def test_prepare_environment_points_state_at_the_service_folder(monkeypatch):
    keys = ("PYTHONUNBUFFERED", "PYTHONIOENCODING", "PYTHONUTF8", "STRATA_RUNTIME_DIR",
            "STRATA_PROFILE_DIR", "STRATA_LAUNCH_PRESET_DIR", "STRATA_COMPONENTS_DIR")
    saved = {key: os.environ.get(key) for key in keys}
    try:
        svc.prepare_environment()
        assert Path(os.environ["STRATA_RUNTIME_DIR"]) == svc.STATE_DIR / "runtime"
        assert Path(os.environ["STRATA_PROFILE_DIR"]) == svc.STATE_DIR / "profiles"
        assert os.environ["PYTHONUTF8"] == "1"
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_health_url_prefers_the_setting_then_the_config(tmp_path):
    config = write_config(tmp_path / "strata-a.json", port=8181)
    assert svc._health_url({"config": str(config), "host": None, "port": 8282}) == \
        "http://127.0.0.1:8282/health"
    assert svc._health_url({"config": str(config), "host": "0.0.0.0", "port": None}) == \
        "http://127.0.0.1:8181/health"


def test_service_root_can_be_moved_for_a_test(tmp_path):
    env = dict(os.environ, STRATA_SERVICE_ROOT=str(tmp_path), PYTHONPATH=str(ROOT))
    out = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, r'%s'); import Strata_Service as s; "
         "print(s.SERVICE_ROOT); print(s.CONF_PATH)" % (ROOT / "Scripts")],
        capture_output=True, text=True, env=env, timeout=60)
    assert out.returncode == 0, out.stderr
    assert Path(out.stdout.splitlines()[0]) == tmp_path


@pytest.mark.skipif(not WINDOWS, reason="the service manager is Windows only")
def test_status_of_a_name_that_cannot_exist_is_not_installed():
    result = svc.service_status("StrataTestNoSuchService")
    assert result["installed"] is False
    assert result["state"] == "not installed"


@pytest.mark.skipif(not WINDOWS, reason="the service manager is Windows only")
def test_run_outside_the_service_manager_says_so(monkeypatch):
    keys = ("STRATA_RUNTIME_DIR", "STRATA_PROFILE_DIR", "STRATA_LAUNCH_PRESET_DIR",
            "STRATA_COMPONENTS_DIR")
    saved = {key: os.environ.get(key) for key in keys}
    try:
        with pytest.raises(svc.ServiceError, match="Service Control Manager"):
            svc.run_service("StrataTestNoSuchService")
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
