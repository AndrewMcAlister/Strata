#!/usr/bin/env python3
"""Tests for Scripts/Strata_Service.py (the Windows service wrapper).

These need no Administrator rights and do not touch the real service manager
except for one read-only lookup of a service name that cannot exist. The SCM
calls that change anything are exercised by hand on a real install.

Run: python -m pytest tools/test_service.py -q
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "Scripts"))

import Strata_Service as svc                                  # noqa: E402
import starting_flag                                          # noqa: E402

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


# ------------------------------------------------------------------------------ the component folder
#
# A model of its own shape (Inkling-2-Mini, Hy4-Flash, a Qwen4 shape) starts an engine package from
# <components>\shapes\<shape>\engine\, and the service - LocalSystem - has no components of its own. That is
# what components= is for; without it "a" or "b" in the model menu served nothing at all.

def component_folder(root: Path) -> Path:
    """A folder shaped like the per-user one: engine/ for the Strata engine, shapes/<shape>/engine/ for the others."""
    where = root / "components"
    (where / "engine" / "0.1.66-windows-x86_64-cuda-cuda13").mkdir(parents=True)
    (where / "shapes" / "inkling2" / "engine" / "0.1.67-windows-x86_64-cuda-cuda13").mkdir(parents=True)
    return where


def supervisor_for(tmp_path: Path) -> tuple[svc.Supervisor, list[str]]:
    """A supervisor whose log is a list, with nothing of the real service manager behind it."""
    lines: list[str] = []
    supervisor = svc.Supervisor("StrataTestNoSuchService", tmp_path / "service.conf")
    supervisor.log = lines.append
    return supervisor, lines


def test_conf_values_reads_the_component_folder(tmp_path):
    config = write_config(tmp_path / "strata-a.json")
    where = component_folder(tmp_path)
    conf = svc.conf_values(f"config={config}\ncomponents={where}\n")
    assert Path(conf["components"]) == where
    assert conf["unknown"] == []


def test_conf_values_rejects_a_component_folder_that_is_not_there(tmp_path):
    config = write_config(tmp_path / "strata-a.json")
    with pytest.raises(svc.ServiceError, match="is not a folder"):
        svc.conf_values(f"config={config}\ncomponents=D:\\nowhere\\components\n")


def test_the_component_folder_comes_from_the_line_then_the_engine_package(tmp_path):
    where = component_folder(tmp_path)
    assert svc.components_dir_for({"components": str(where)}) == where
    packaged = where / "engine" / "0.1.66-windows-x86_64-cuda-cuda13" / "strata.exe"
    packaged.write_bytes(b"MZ")
    assert svc.components_dir_for({"engine": str(packaged)}) == where
    hand_build = tmp_path / "build-906-win" / "strata.exe"
    hand_build.parent.mkdir()
    hand_build.write_bytes(b"MZ")
    assert svc.components_dir_for({"engine": str(hand_build)}) == svc.STATE_DIR / "components"


def test_each_turn_points_the_server_at_the_component_folder(monkeypatch, tmp_path):
    where = component_folder(tmp_path)
    monkeypatch.setenv("STRATA_COMPONENTS_DIR", "C:\\elsewhere")
    assert svc.apply_components_env({"components": str(where)}) == where
    assert Path(os.environ["STRATA_COMPONENTS_DIR"]) == where


def test_the_shape_archs_are_the_serve_modules_own():
    """The names are serve/inkling_kind.ARCH and serve/hy4_kind.ARCH; a rename there must not leave a copy behind."""
    inkling = importlib.import_module("serve.inkling_kind")
    hy4 = importlib.import_module("serve.hy4_kind")
    assert set(svc.CUSTOM_SHAPE_ARCHS) == {inkling.ARCH, hy4.ARCH}


def test_a_shape_model_keeps_its_own_engine_and_no_copy_is_written(tmp_path, monkeypatch):
    monkeypatch.setattr(svc, "STATE_DIR", tmp_path / "state")
    supervisor, lines = supervisor_for(tmp_path)
    config = tmp_path / "strata-inkling-2-mini-sq-20.json"
    config.write_text(json.dumps({"exe": "component:strata-inkling", "args": ["--native", "m.strata"],
                                  "arch": "inkling2_loop", "model_name": "inkling-2-mini-sq-20"}), encoding="utf-8")
    engine = str(tmp_path / "engine" / "strata.exe")
    assert supervisor._engine_config(config, engine) == config
    assert not (svc.STATE_DIR / "run-config.json").exists(), "the config's own engine is the one that runs"
    assert supervisor._engine_config(config, engine) == config
    assert sum("runs on its own engine" in line for line in lines) == 1, "said once, not once per watchdog turn"
    assert any("does not replace it" in line for line in lines)


def test_a_strata_engine_model_still_takes_the_engine_from_service_conf(tmp_path, monkeypatch):
    monkeypatch.setattr(svc, "STATE_DIR", tmp_path / "state")
    supervisor, _lines = supervisor_for(tmp_path)
    config = write_config(tmp_path / "strata-qwen.json", model="qwen3.8-flash-next-iq2_xs")
    copy = supervisor._engine_config(config, "D:\\engines\\strata.exe")
    assert copy != config and copy.name == "run-config.json"
    assert json.loads(copy.read_text(encoding="utf-8"))["exe"] == "D:\\engines\\strata.exe"


def test_a_shape_engine_missing_from_the_folder_says_which_line_fixes_it(tmp_path):
    config = tmp_path / "strata-inkling.json"
    config.write_text(json.dumps({"exe": "component:strata-inkling", "args": [], "arch": "inkling2_loop"}),
                      encoding="utf-8")
    supervisor, _lines = supervisor_for(tmp_path)
    with pytest.raises(svc.ServiceError) as caught:
        supervisor._check_own_engine(config, {"components": str(tmp_path / "empty")})
    assert "strata-inkling" in str(caught.value)
    assert "components=<that folder>" in str(caught.value)
    assert "Strata_Engine.ps1 -Components" in str(caught.value)


def test_a_shape_engine_where_the_service_reads_it_is_accepted(tmp_path):
    where = component_folder(tmp_path)
    config = tmp_path / "strata-inkling.json"
    config.write_text(json.dumps({"exe": "component:strata-inkling", "args": [], "arch": "inkling2_loop"}),
                      encoding="utf-8")
    supervisor, _lines = supervisor_for(tmp_path)
    supervisor._check_own_engine(config, {"components": str(where)})       # no refusal


def test_ensure_conf_components_writes_the_line_and_keeps_the_rest(tmp_path):
    config = write_config(tmp_path / "strata-a.json")
    conf = tmp_path / "service.conf"
    conf.write_text(f"config={config}\nport=8090\n", encoding="utf-8")
    where = component_folder(tmp_path)
    assert svc.ensure_conf_components(conf, where) == where
    text = conf.read_text(encoding="utf-8")
    assert f"components={where}" in text
    assert "port=8090" in text, "the person's own settings are kept"
    assert svc.ensure_conf_components(conf, None) == where, "a second switch uses the line already there"


def test_ensure_conf_components_leaves_a_folder_that_holds_no_engine_alone(tmp_path, monkeypatch):
    """The folder of the person running this is only written when it really holds an engine."""
    (tmp_path / "AppData" / "Local" / "Strata" / "components" / "empty").mkdir(parents=True)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData" / "Local"))
    config = write_config(tmp_path / "strata-a.json")
    conf = tmp_path / "service.conf"
    conf.write_text(f"config={config}\n", encoding="utf-8")
    assert svc.ensure_conf_components(conf) is None
    assert "components=" not in conf.read_text(encoding="utf-8")


def test_an_explicit_component_folder_that_holds_no_engine_is_refused(tmp_path):
    """-Components is the person saying where it is: a folder with no engine in it is a mistake, not a default."""
    config = write_config(tmp_path / "strata-a.json")
    conf = tmp_path / "service.conf"
    conf.write_text(f"config={config}\n", encoding="utf-8")
    (tmp_path / "empty").mkdir()
    with pytest.raises(svc.ServiceError, match="holds no engine"):
        svc.ensure_conf_components(conf, tmp_path / "empty")
    assert "components=" not in conf.read_text(encoding="utf-8")


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
        assert Path(os.environ["STRATA_COMPONENTS_DIR"]) == svc.STATE_DIR / "components"
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


# ------------------------------------------------------------------------------ the starting flag

def test_the_flag_can_only_be_released_by_the_process_that_took_it(tmp_path):
    """A stop path must not delete another starter's flag: that would let a second model load."""
    flag = tmp_path / "strata.starting"
    flag.write_text(json.dumps({"pid": os.getpid() + 1, "owner": "another starter"}),
                    encoding="utf-8")
    assert starting_flag.release_own(flag) is False
    assert flag.exists()
    assert starting_flag.is_fresh(flag)
    taken, _ = starting_flag.acquire("this test", flag)
    assert taken is False, "a fresh flag from another pid still blocks a start"


def test_the_service_can_release_the_flag_after_a_failed_start(tmp_path):
    flag = tmp_path / "strata.starting"
    taken, _ = starting_flag.acquire("this test", flag)
    assert taken
    assert starting_flag.release_own(flag) is True
    assert not flag.exists()
    assert starting_flag.release_own(flag) is False


def test_the_service_releases_its_own_flag_before_acquiring_again():
    """An asynchronous failed start leaves the flag; without this the service refuses itself."""
    source = (ROOT / "Scripts" / "Strata_Service.py").read_text(encoding="utf-8")
    assert "starting_flag.release()" not in source
    assert source.count("starting_flag.release_own()") >= 4
    assert source.index("starting_flag.release_own()") < source.index("starting_flag.acquire("), \
        "the retry releases its own flag before it asks for it again"


@pytest.mark.skipif(not WINDOWS, reason="the service loop is Windows only")
def test_a_failed_start_releases_the_flag_so_the_next_retry_can_start(monkeypatch, tmp_path):
    """The failure is asynchronous: _runtime.start returns, then status() says failed. The flag
    is still the service's own, so an unconditional retry would be refused for five minutes."""

    class FakeFlag:
        def __init__(self):
            self.held = False
            self.releases = 0

        def acquire(self, owner):
            if self.held:
                return False, "this service still holds the flag"
            self.held = True
            return True, "taken"

        def release_own(self):
            self.releases += 1
            if not self.held:
                return False
            self.held = False
            return True

    class FakeRuntime:
        def __init__(self):
            self.states = ["stopped", "failed"]
            self.starts = 0

        def status(self, profile):
            return {"state": self.states.pop(0) if self.states else "failed"}

        def start(self, *args, **kwargs):
            self.starts += 1

        def stop(self, *args, **kwargs):
            pass

    class FakeEvent:
        def __init__(self, stop_after):
            self.stop_after = stop_after
            self.waits = 0
            self.done = False

        def is_set(self):
            return self.done

        def wait(self, seconds):
            self.waits += 1
            if self.waits >= self.stop_after:
                self.done = True
            return self.done

    class FakeKernel:
        def GetCurrentProcess(self):
            return 0

        def SetPriorityClass(self, handle, priority):
            return 1

    flag, runtime, event = FakeFlag(), FakeRuntime(), FakeEvent(stop_after=2)
    supervisor = svc.Supervisor("StrataTestNoSuchService", tmp_path / "service.conf")
    supervisor._runtime, supervisor._profiles = runtime, object()
    supervisor.stop_event = event
    supervisor.log = lambda message: None
    monkeypatch.setattr(svc, "starting_flag", flag)
    monkeypatch.setattr(svc, "kernel32", FakeKernel())
    monkeypatch.setattr(svc, "load_conf", lambda path: {
        "config": str(tmp_path / "strata-a.json"), "unknown": [], "shutdown_timeout": 1})
    monkeypatch.setattr(svc.Supervisor, "_ensure_profile", lambda self, conf: None)
    monkeypatch.setattr(svc.Supervisor, "_import_strata", lambda self: True)

    before = os.getcwd()
    try:
        supervisor.run()
    finally:
        os.chdir(before)

    assert runtime.starts == 2, "the retry after the failed start ran"
    assert flag.releases >= 2, "the service released the flag it held before retrying"
