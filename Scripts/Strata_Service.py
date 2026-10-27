#!/usr/bin/env python3
"""Strata as a Windows service: install, remove, control, and supervise.

The service does not run the HTTP server itself. It runs Strata's own owner host
(``strata_control.runtime``), the same background service ``strata runtime start``
uses. That host already starts the server, drains it on stop (stdin EOF on Windows)
and keeps the server and engine in a kill-on-close job. What it does not do is
restart a server that exited, so this file adds the watchdog, the boot registration
and the daily log.

Why ctypes instead of pywin32 or NSSM: a service must keep working after a Strata
update rebuilds ``.venv`` and must not depend on a third-party binary that update
cannot install. Strata already talks to Windows through ctypes (``winproc.py``,
``private_files.py``), so the SCM calls use the same approach and nothing extra is
installed.

Which model the service serves is one editable file, ``service.conf``, written by
``install`` with every ``strata-*.json`` config in the Strata folder listed above the
chosen line as a comment. Edit it, then restart the service.

Commands (normally through Scripts\\Strata_Service.ps1, which asks for elevation):

    install [--config PATH] [--no-start] [--force]
    uninstall [--purge]
    start | stop | restart | status | logs
    run                      started by the Service Control Manager only
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

# The junction path is kept as written: a terminal archive is updated by retargeting
# <install>\terminal\current, and resolving it here would register the versioned
# folder instead, which a later update removes.
ROOT = Path(os.path.abspath(__file__)).parent.parent
SCRIPT = Path(os.path.abspath(__file__))
SERVICE_NAME = "Strata"
DISPLAY_NAME = "Strata AI Server"
DESCRIPTION = "Serves Strata's local model and its OpenAI/Anthropic-compatible API at startup."
PROFILE = "strata-service"
SERVICE_SCRIPT = ROOT / "Scripts" / "Strata_Service.py"


def _service_root() -> Path:
    """Where service.conf, its logs and its private state live."""
    override = os.environ.get("STRATA_SERVICE_ROOT")
    if override:
        return Path(override)
    program_data = os.environ.get("PROGRAMDATA") or r"C:\ProgramData"
    return Path(program_data) / "Strata" / "service"


SERVICE_ROOT = _service_root()
CONF_PATH = SERVICE_ROOT / "service.conf"
LOG_DIR = SERVICE_ROOT / "logs"
STATE_DIR = SERVICE_ROOT / "state"


class ServiceError(Exception):
    """A problem worth one plain sentence to the user."""

    def __init__(self, message: str, code: int = 2):
        super().__init__(message)
        self.code = code


# ------------------------------------------------------------------------------------------------ conf

CONF_KEYS = ("config", "port", "host", "shutdown_timeout")


def parse_conf_text(text: str) -> tuple[dict, list[str]]:
    """Read ``name=value`` lines. ``#`` and ``;`` start a comment; blank lines are skipped."""
    values: dict[str, str] = {}
    problems: list[str] = []
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if "=" not in line:
            problems.append(f"line {number}: expected name=value, found {line!r}")
            continue
        key, value = line.split("=", 1)
        key = key.strip().lower()
        if not key:
            problems.append(f"line {number}: the name before '=' is empty")
            continue
        values[key] = value.strip().strip('"').strip("'")
    return values, problems


def conf_values(text: str) -> dict:
    """Validated settings from the file text. Raises ServiceError with the file's own words."""
    values, problems = parse_conf_text(text)
    if problems:
        raise ServiceError("service.conf has a problem: " + "; ".join(problems))
    config = (values.get("config") or "").strip()
    if not config:
        raise ServiceError("service.conf has no config= line. Add the model config to serve, for example "
                           r"config=D:\Strata\strata-iq3_xxs.json")
    path = Path(config).expanduser()
    if not path.is_absolute():
        path = (ROOT / path).resolve()
    port = None
    if values.get("port"):
        try:
            port = int(values["port"])
        except ValueError:
            raise ServiceError(f"service.conf: port={values['port']!r} is not a number") from None
        if not 1 <= port <= 65535:
            raise ServiceError("service.conf: port must be 1..65535")
    try:
        shutdown = float(values.get("shutdown_timeout") or 30)
    except ValueError:
        raise ServiceError("service.conf: shutdown_timeout must be a number of seconds") from None
    return {"config": str(path), "port": port, "host": values.get("host") or None,
            "shutdown_timeout": shutdown,
            "unknown": sorted(set(values) - set(CONF_KEYS))}


def load_conf(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        raise ServiceError(f"{path} is missing. Run the install action again to write it.", 5) from None
    except OSError as exc:
        raise ServiceError(f"cannot read {path} ({exc.__class__.__name__})", 5) from None
    return conf_values(text)


def run_config_summary(path: Path) -> dict | None:
    """The model name, port and context of one run config, or None when it is not one."""
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("exe") or not isinstance(data.get("args"), list):
        return None
    context = None
    args = [str(x) for x in data["args"]]
    if "--max-context" in args:
        index = args.index("--max-context")
        if index + 1 < len(args):
            try:
                context = int(args[index + 1])
            except ValueError:
                context = None
    port = data.get("port")
    return {"path": str(path), "model": str(data.get("model_name") or ""),
            "port": int(port) if isinstance(port, int) else None, "context": context}


def discover_models(root: Path) -> list[dict]:
    """Every installed model config in the Strata folder, in name order.

    ``strata-*.json`` also matches the server's ``strata-<tag>.shared-settings.json``
    sidecar and the backup files a manual edit leaves behind; neither is a model.
    """
    found = []
    for path in sorted(root.glob("strata-*.json")):
        name = path.name.lower()
        if name.endswith(".shared-settings.json") or ".bak" in name or not path.is_file():
            continue
        summary = run_config_summary(path)
        if summary is not None:
            found.append(summary)
    return found


def default_config(root: Path) -> Path | None:
    """The newest valid config; the most recently set-up model is the obvious first choice."""
    models = discover_models(root)
    if not models:
        return None
    newest = max(models, key=lambda m: (Path(m["path"]).stat().st_mtime, Path(m["path"]).name))
    return Path(newest["path"])


def _model_comment(model: dict) -> str:
    bits = [model["path"]]
    if model["model"]:
        bits.append(model["model"])
    if model["context"]:
        bits.append(f"context {model['context']}")
    if model["port"]:
        bits.append(f"port {model['port']}")
    return "  " + "  ".join(bits)


def render_conf(config: Path, models: list[dict]) -> str:
    lines = [
        "# Strata Windows service settings.",
        "#",
        "# The service starts this model at boot. To serve another one, change the",
        "# config= line, save, then restart the service:",
        f'#   powershell -ExecutionPolicy Bypass -File "{ROOT / "Scripts" / "Strata_Service.ps1"}" -Action Restart',
        "#",
        "# Model configs found in " + str(ROOT) + " when this file was written:",
    ]
    lines.extend("#" + _model_comment(m) for m in models)
    lines += [
        "#",
        "# Other settings are optional. A # in front means \"use the config's value\".",
        f"config={config}",
        "#port=8080",
        "#host=127.0.0.1",
        "#shutdown_timeout=30",
        "",
    ]
    return "\n".join(lines)


def write_conf(path: Path, config: Path, models: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_conf(config, models), encoding="utf-8")


def grant_user_write(path: Path) -> None:
    """Let the installing person edit service.conf without an elevated editor.

    The file lives under ProgramData, where Users have read only. The person who
    installed the service is already an administrator, so this adds no rights they
    do not have; it only saves opening an elevated Notepad.
    """
    user = os.environ.get("USERNAME")
    domain = os.environ.get("USERDOMAIN")
    if not user:
        return
    account = f"{domain}\\{user}" if domain else user
    try:
        subprocess.run(["icacls", str(path), "/grant", f"{account}:M"],
                       capture_output=True, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired):
        pass


# ------------------------------------------------------------------------------------------------ Win32

if os.name == "nt":
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)

    SC_MANAGER_CONNECT = 0x0001
    SC_MANAGER_ALL_ACCESS = 0xF003F
    SERVICE_QUERY_STATUS = 0x0004
    SERVICE_START = 0x0010
    SERVICE_STOP = 0x0020
    SERVICE_DELETE = 0x10000
    SERVICE_ALL_ACCESS = 0xF01FF
    SERVICE_WIN32_OWN_PROCESS = 0x00000010
    SERVICE_AUTO_START = 0x00000002
    SERVICE_ERROR_NORMAL = 0x00000001
    SERVICE_NO_CHANGE = 0xFFFFFFFF
    SERVICE_CONTROL_STOP = 0x00000001
    SERVICE_CONTROL_SHUTDOWN = 0x00000005
    SERVICE_STOPPED = 0x00000001
    SERVICE_START_PENDING = 0x00000002
    SERVICE_STOP_PENDING = 0x00000003
    SERVICE_RUNNING = 0x00000004
    SERVICE_ACCEPT_STOP = 0x00000001
    SERVICE_ACCEPT_SHUTDOWN = 0x00000004
    SC_STATUS_PROCESS_INFO = 0
    SERVICE_CONFIG_DESCRIPTION = 1
    NORMAL_PRIORITY_CLASS = 0x00000020
    ERROR_SERVICE_DOES_NOT_EXIST = 1060
    ERROR_SERVICE_ALREADY_RUNNING = 1056
    ERROR_SERVICE_NOT_ACTIVE = 1062
    ERROR_SERVICE_MARKED_FOR_DELETE = 1072
    ERROR_SERVICE_EXISTS = 1073
    ERROR_FAILED_SERVICE_CONTROLLER_CONNECT = 1063

    STATE_NAMES = {1: "stopped", 2: "starting", 3: "stopping", 4: "running",
                   5: "continuing", 6: "pausing", 7: "paused"}

    class SERVICE_STATUS(ctypes.Structure):
        _fields_ = [("dwServiceType", wintypes.DWORD), ("dwCurrentState", wintypes.DWORD),
                    ("dwControlsAccepted", wintypes.DWORD), ("dwWin32ExitCode", wintypes.DWORD),
                    ("dwServiceSpecificExitCode", wintypes.DWORD), ("dwCheckPoint", wintypes.DWORD),
                    ("dwWaitHint", wintypes.DWORD)]

    class SERVICE_STATUS_PROCESS(ctypes.Structure):
        _fields_ = [("dwServiceType", wintypes.DWORD), ("dwCurrentState", wintypes.DWORD),
                    ("dwControlsAccepted", wintypes.DWORD), ("dwWin32ExitCode", wintypes.DWORD),
                    ("dwServiceSpecificExitCode", wintypes.DWORD), ("dwCheckPoint", wintypes.DWORD),
                    ("dwWaitHint", wintypes.DWORD), ("dwProcessId", wintypes.DWORD),
                    ("dwServiceFlags", wintypes.DWORD)]

    class QUERY_SERVICE_CONFIGW(ctypes.Structure):
        _fields_ = [("dwServiceType", wintypes.DWORD), ("dwStartType", wintypes.DWORD),
                    ("dwErrorControl", wintypes.DWORD), ("lpBinaryPathName", wintypes.LPWSTR),
                    ("lpLoadOrderGroup", wintypes.LPWSTR), ("dwTagId", wintypes.DWORD),
                    ("lpDependencies", wintypes.LPWSTR), ("lpServiceStartName", wintypes.LPWSTR),
                    ("lpDisplayName", wintypes.LPWSTR)]

    class SERVICE_DESCRIPTIONW(ctypes.Structure):
        _fields_ = [("lpDescription", wintypes.LPWSTR)]

    LPHANDLER_FUNCTION_EX = ctypes.WINFUNCTYPE(wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                                               ctypes.c_void_p, ctypes.c_void_p)
    LPSERVICE_MAIN_FUNCTIONW = ctypes.WINFUNCTYPE(None, wintypes.DWORD,
                                                  ctypes.POINTER(wintypes.LPWSTR))

    class SERVICE_TABLE_ENTRYW(ctypes.Structure):
        _fields_ = [("lpServiceName", wintypes.LPWSTR), ("lpServiceProc", LPSERVICE_MAIN_FUNCTIONW)]

    advapi32.OpenSCManagerW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
    advapi32.OpenSCManagerW.restype = wintypes.HANDLE
    advapi32.CreateServiceW.argtypes = [wintypes.HANDLE, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                        wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                                        wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD),
                                        wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR]
    advapi32.CreateServiceW.restype = wintypes.HANDLE
    advapi32.OpenServiceW.argtypes = [wintypes.HANDLE, wintypes.LPCWSTR, wintypes.DWORD]
    advapi32.OpenServiceW.restype = wintypes.HANDLE
    advapi32.CloseServiceHandle.argtypes = [wintypes.HANDLE]
    advapi32.CloseServiceHandle.restype = wintypes.BOOL
    advapi32.QueryServiceStatusEx.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                              wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    advapi32.QueryServiceStatusEx.restype = wintypes.BOOL
    advapi32.QueryServiceConfigW.argtypes = [wintypes.HANDLE, ctypes.POINTER(QUERY_SERVICE_CONFIGW),
                                             wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    advapi32.QueryServiceConfigW.restype = wintypes.BOOL
    advapi32.ChangeServiceConfigW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                              wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                              ctypes.POINTER(wintypes.DWORD), wintypes.LPCWSTR,
                                              wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR]
    advapi32.ChangeServiceConfigW.restype = wintypes.BOOL
    advapi32.ChangeServiceConfig2W.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p]
    advapi32.ChangeServiceConfig2W.restype = wintypes.BOOL
    advapi32.StartServiceW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                       ctypes.POINTER(wintypes.LPWSTR)]
    advapi32.StartServiceW.restype = wintypes.BOOL
    advapi32.ControlService.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                        ctypes.POINTER(SERVICE_STATUS)]
    advapi32.ControlService.restype = wintypes.BOOL
    advapi32.DeleteService.argtypes = [wintypes.HANDLE]
    advapi32.DeleteService.restype = wintypes.BOOL
    advapi32.RegisterServiceCtrlHandlerExW.argtypes = [wintypes.LPCWSTR, LPHANDLER_FUNCTION_EX,
                                                       ctypes.c_void_p]
    advapi32.RegisterServiceCtrlHandlerExW.restype = wintypes.HANDLE
    advapi32.SetServiceStatus.argtypes = [wintypes.HANDLE, ctypes.POINTER(SERVICE_STATUS)]
    advapi32.SetServiceStatus.restype = wintypes.BOOL
    advapi32.StartServiceCtrlDispatcherW.argtypes = [ctypes.POINTER(SERVICE_TABLE_ENTRYW)]
    advapi32.StartServiceCtrlDispatcherW.restype = wintypes.BOOL
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.SetPriorityClass.restype = wintypes.BOOL
    shell32.IsUserAnAdmin.argtypes = []
    shell32.IsUserAnAdmin.restype = wintypes.BOOL


def require_windows() -> None:
    if os.name != "nt":
        raise ServiceError("The Strata service is for Windows only.", 2)


def is_admin() -> bool:
    if os.name != "nt":
        return False
    try:
        return bool(shell32.IsUserAnAdmin())
    except Exception:                                     # noqa: BLE001 - an unelevated answer is enough
        return False


def _last_error() -> int:
    return ctypes.get_last_error() if os.name == "nt" else 0


def _word(code: int) -> str:
    try:
        return f"{ctypes.WinError(code)} (error {code})"
    except Exception:                                     # noqa: BLE001
        return f"error {code}"


def _open_scm(access: int = SC_MANAGER_CONNECT):
    handle = advapi32.OpenSCManagerW(None, None, access)
    if not handle:
        raise ServiceError("Cannot open the Windows service manager. Run this in an elevated "
                           f"PowerShell ({_word(_last_error())}).", 5)
    return handle


def _query_status(handle) -> SERVICE_STATUS_PROCESS:
    info = SERVICE_STATUS_PROCESS()
    needed = wintypes.DWORD()
    if not advapi32.QueryServiceStatusEx(handle, SC_STATUS_PROCESS_INFO, ctypes.byref(info),
                                         ctypes.sizeof(info), ctypes.byref(needed)):
        raise ServiceError(f"Cannot read the service state ({_word(_last_error())}).", 5)
    return info


def service_status(name: str = SERVICE_NAME) -> dict:
    """The SCM's answer, with no assumption that the service exists."""
    require_windows()
    scm = _open_scm()
    service = None
    try:
        service = advapi32.OpenServiceW(scm, name, SERVICE_QUERY_STATUS)
        if not service:
            code = _last_error()
            if code == ERROR_SERVICE_DOES_NOT_EXIST:
                return {"installed": False, "state": "not installed", "state_code": 0, "pid": 0}
            raise ServiceError(f"Cannot open service '{name}' ({_word(code)}).", 5)
        info = _query_status(service)
        return {"installed": True, "state": STATE_NAMES.get(info.dwCurrentState, str(info.dwCurrentState)),
                "state_code": info.dwCurrentState, "pid": info.dwProcessId}
    finally:
        if service:
            advapi32.CloseServiceHandle(service)
        advapi32.CloseServiceHandle(scm)


def _binary_path(service) -> str | None:
    needed = wintypes.DWORD()
    advapi32.QueryServiceConfigW(service, None, 0, ctypes.byref(needed))
    if not needed.value:
        return None
    buffer = ctypes.create_string_buffer(needed.value)
    if not advapi32.QueryServiceConfigW(service, ctypes.cast(buffer, ctypes.POINTER(QUERY_SERVICE_CONFIGW)),
                                        needed.value, ctypes.byref(needed)):
        return None
    config = ctypes.cast(buffer, ctypes.POINTER(QUERY_SERVICE_CONFIGW)).contents
    return config.lpBinaryPathName


def build_bin_path(python: Path, script: Path, name: str = SERVICE_NAME) -> str:
    return f'"{python}" "{script}" run --name {name}'


def stable_python(root: Path = ROOT) -> Path:
    """The interpreter the service should run, chosen the way bin\\strata.cmd chooses it.

    The archive's private runtime and a source checkout's venv both sit below the
    stable install root, so the service survives an update that replaces a versioned
    folder behind the ``current`` junction.
    """
    for candidate in (root / "python" / "python.exe", root / ".venv" / "Scripts" / "python.exe"):
        if candidate.is_file():
            return candidate
    return Path(sys.executable)


def register_service(name: str, bin_path: str, *, force: bool = False) -> str:
    """Create the service, or repoint ours. Returns "created" or "updated"."""
    require_windows()
    scm = _open_scm(SC_MANAGER_ALL_ACCESS)
    service = None
    try:
        service = advapi32.CreateServiceW(scm, name, DISPLAY_NAME, SERVICE_ALL_ACCESS,
                                          SERVICE_WIN32_OWN_PROCESS, SERVICE_AUTO_START,
                                          SERVICE_ERROR_NORMAL, bin_path, None, None, None, None, None)
        action = "created"
        if not service:
            code = _last_error()
            if code != ERROR_SERVICE_EXISTS:
                raise ServiceError(f"Cannot create service '{name}' ({_word(code)}).", 5)
            service = advapi32.OpenServiceW(scm, name, SERVICE_ALL_ACCESS)
            if not service:
                raise ServiceError(f"Cannot open the existing service '{name}' ({_word(_last_error())}).", 5)
            current = _binary_path(service) or ""
            if str(SERVICE_SCRIPT) not in current and not force:
                raise ServiceError(f"A service named '{name}' already exists and was not installed by "
                                   f"this script ({current or 'path unknown'}). Use --name to pick "
                                   "another name, or --force to replace it.", 5)
            if not advapi32.ChangeServiceConfigW(service, SERVICE_NO_CHANGE, SERVICE_AUTO_START,
                                                 SERVICE_NO_CHANGE, bin_path, None, None, None, None,
                                                 None, DISPLAY_NAME):
                raise ServiceError(f"Cannot update service '{name}' ({_word(_last_error())}).", 5)
            action = "updated"
        description = SERVICE_DESCRIPTIONW(DESCRIPTION)
        advapi32.ChangeServiceConfig2W(service, SERVICE_CONFIG_DESCRIPTION, ctypes.byref(description))
        return action
    finally:
        if service:
            advapi32.CloseServiceHandle(service)
        advapi32.CloseServiceHandle(scm)


def _wait_state(service, wanted: int, timeout: float) -> SERVICE_STATUS_PROCESS:
    deadline = time.monotonic() + max(0.5, timeout)
    info = _query_status(service)
    while time.monotonic() < deadline:
        if info.dwCurrentState == wanted:
            return info
        if wanted != SERVICE_STOPPED and info.dwCurrentState == SERVICE_STOPPED:
            return info
        time.sleep(0.25)
        info = _query_status(service)
    return info


def start_service(name: str = SERVICE_NAME, timeout: float = 30) -> dict:
    require_windows()
    scm = _open_scm()
    service = None
    try:
        service = advapi32.OpenServiceW(scm, name, SERVICE_START | SERVICE_QUERY_STATUS)
        if not service:
            raise ServiceError(f"Service '{name}' is not installed ({_word(_last_error())}).", 5)
        if not advapi32.StartServiceW(service, 0, None):
            code = _last_error()
            if code != ERROR_SERVICE_ALREADY_RUNNING:
                raise ServiceError(f"Cannot start '{name}' ({_word(code)}).", 5)
        info = _wait_state(service, SERVICE_RUNNING, timeout)
        return {"state": STATE_NAMES.get(info.dwCurrentState, str(info.dwCurrentState)), "pid": info.dwProcessId}
    finally:
        if service:
            advapi32.CloseServiceHandle(service)
        advapi32.CloseServiceHandle(scm)


def stop_service(name: str = SERVICE_NAME, timeout: float = 120) -> dict:
    require_windows()
    scm = _open_scm()
    service = None
    try:
        service = advapi32.OpenServiceW(scm, name, SERVICE_STOP | SERVICE_QUERY_STATUS)
        if not service:
            raise ServiceError(f"Service '{name}' is not installed ({_word(_last_error())}).", 5)
        status = SERVICE_STATUS()
        if not advapi32.ControlService(service, SERVICE_CONTROL_STOP, ctypes.byref(status)):
            code = _last_error()
            if code == ERROR_SERVICE_NOT_ACTIVE:
                return {"state": "stopped", "pid": 0}
            raise ServiceError(f"Cannot stop '{name}' ({_word(code)}).", 5)
        info = _wait_state(service, SERVICE_STOPPED, timeout)
        state = STATE_NAMES.get(info.dwCurrentState, str(info.dwCurrentState))
        if state != "stopped":
            raise ServiceError(f"Service '{name}' did not stop within {int(timeout)} seconds "
                               f"(state: {state}).", 5)
        return {"state": state, "pid": 0}
    finally:
        if service:
            advapi32.CloseServiceHandle(service)
        advapi32.CloseServiceHandle(scm)


def delete_service(name: str = SERVICE_NAME) -> None:
    require_windows()
    scm = _open_scm(SC_MANAGER_ALL_ACCESS)
    service = None
    try:
        service = advapi32.OpenServiceW(scm, name, SERVICE_DELETE | SERVICE_QUERY_STATUS)
        if not service:
            code = _last_error()
            if code == ERROR_SERVICE_DOES_NOT_EXIST:
                return
            raise ServiceError(f"Cannot open '{name}' for removal ({_word(code)}).", 5)
        if not advapi32.DeleteService(service):
            code = _last_error()
            if code not in (ERROR_SERVICE_DOES_NOT_EXIST, ERROR_SERVICE_MARKED_FOR_DELETE):
                raise ServiceError(f"Cannot remove '{name}' ({_word(code)}).", 5)
    finally:
        if service:
            advapi32.CloseServiceHandle(service)
        advapi32.CloseServiceHandle(scm)


# ------------------------------------------------------------------------------------------------ supervisor

def _tail(path: Path, lines: int = 20, limit: int = 16384) -> list[str]:
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - limit))
            data = handle.read()
    except OSError:
        return []
    return data.decode("utf-8", "replace").splitlines()[-lines:]


def decide(state: str) -> str:
    """What the watchdog does next for one runtime state.

    ``ready`` and ``starting`` are healthy; ``failed`` and ``stale`` need the old
    owner ended before a new start, or ``runtime.start`` refuses because a live
    owner still owns the profile.
    """
    if state in ("ready", "starting"):
        return "wait"
    if state in ("failed", "stale"):
        return "stop-then-start"
    return "start"


class Supervisor:
    """Owns the runtime owner host and restarts the server when it exits."""

    def __init__(self, name: str, conf_path: Path):
        self.name = name
        self.conf_path = conf_path
        self.handle = None
        self.stop_event = threading.Event()
        self.log_path = LOG_DIR / f"service-{time.strftime('%Y-%m-%d')}.log"
        self._runtime = None
        self._profiles = None

    # -- logging ---------------------------------------------------------------------------------
    def log(self, message: str) -> None:
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} [Strata] {message}"
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            with open(self.log_path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError:
            pass
        try:
            print(line, file=sys.stderr, flush=True)
        except Exception:                                 # noqa: BLE001 - a service has no console
            pass

    def _tail_runtime_logs(self) -> None:
        if not self._runtime:
            return
        base = Path(os.environ.get("STRATA_RUNTIME_DIR") or STATE_DIR / "runtime")
        for name in (f"{PROFILE}.server.log", f"{PROFILE}.host.log"):
            path = base / name
            if path.is_file():
                self.log(f"last lines of {path}:")
                for line in _tail(path):
                    self.log("  " + line)
                return

    # -- SCM reporting ---------------------------------------------------------------------------
    def _report(self, state: int, accepted: int = 0, checkpoint: int = 0, wait_hint: int = 0) -> None:
        if not self.handle or os.name != "nt":
            return
        status = SERVICE_STATUS(SERVICE_WIN32_OWN_PROCESS, state, accepted, 0, 0, checkpoint, wait_hint)
        advapi32.SetServiceStatus(self.handle, ctypes.byref(status))

    def report_start_pending(self) -> None:
        self._report(SERVICE_START_PENDING, checkpoint=1, wait_hint=30000)

    def report_running(self) -> None:
        self._report(SERVICE_RUNNING, accepted=SERVICE_ACCEPT_STOP | SERVICE_ACCEPT_SHUTDOWN)

    def report_stop_pending(self, wait_hint: int) -> None:
        self._report(SERVICE_STOP_PENDING, checkpoint=1, wait_hint=max(1000, wait_hint))

    def report_stopped(self) -> None:
        self._report(SERVICE_STOPPED)

    def control(self, command: int) -> int:
        if command in (SERVICE_CONTROL_STOP, SERVICE_CONTROL_SHUTDOWN):
            self.stop_event.set()
            self.report_stop_pending(120000)
        return 0

    # -- the service body ------------------------------------------------------------------------
    def main(self) -> None:
        if os.name != "nt":
            return
        self.handle = advapi32.RegisterServiceCtrlHandlerExW(self.name, _HANDLER_REF, None)
        if not self.handle:
            return
        self.report_start_pending()
        self.report_running()                             # the model loads in the background
        try:
            self.run()
        finally:
            self.report_stopped()

    def _import_strata(self) -> bool:
        try:
            from strata_control import profiles, runtime
        except Exception as exc:                          # noqa: BLE001 - a broken install is retried
            self.log(f"cannot import Strata from {ROOT}: {exc.__class__.__name__}: {exc}")
            return False
        self._profiles, self._runtime = profiles, runtime
        return True

    def _ensure_profile(self, conf: dict) -> None:
        profiles = self._profiles
        config = Path(conf["config"]).resolve()
        if not config.is_file():
            raise ServiceError(f"the model config {config} is not there")
        try:
            profile, revision = profiles.load(PROFILE)
        except profiles.ProfileError:
            try:
                profiles.create(PROFILE, str(config))
            except profiles.ProfileError:
                pass                                   # another start created it; load below decides
            profile, revision = profiles.load(PROFILE)
        if Path(profile["source_config"]).resolve() != config:
            proposed, revision = profiles.propose_source(PROFILE, str(config))
            profiles.save(PROFILE, proposed, expected_revision=revision)
            profile, revision = profiles.load(PROFILE)
        wanted = dict(profile.get("overrides") or {})
        changed = False
        if conf["port"] is not None and wanted.get("port") != conf["port"]:
            wanted["port"] = conf["port"]
            changed = True
        if conf["host"] and wanted.get("host") != conf["host"]:
            wanted["host"] = conf["host"]
            changed = True
        if changed:
            profiles.save(PROFILE, {**profile, "overrides": wanted}, expected_revision=revision)
            self.log(f"profile overrides set to {wanted}")

    def _stop_runtime(self, timeout: float) -> None:
        try:
            self._runtime.stop(PROFILE, timeout=max(5, min(int(timeout), 330)))
        except Exception as exc:                          # noqa: BLE001 - "nothing to stop" is normal
            text = str(exc)
            if "not_owned" not in text and "not managed" not in text and "not saved" not in text:
                self.log(f"stop: {exc.__class__.__name__}: {exc}")

    def run(self) -> None:
        os.chdir(ROOT)
        kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), NORMAL_PRIORITY_CLASS)
        self.log(f"service starting: python={sys.executable} root={ROOT} conf={self.conf_path}")
        backoff = 5
        while not self.stop_event.is_set():
            if not self._import_strata():
                self.stop_event.wait(60)
                continue
            try:
                conf = load_conf(self.conf_path)
                if conf["unknown"]:
                    self.log("ignored unknown setting(s) in service.conf: " + ", ".join(conf["unknown"]))
                self._ensure_profile(conf)
                current = self._runtime.status(PROFILE)
                state = str(current.get("state", "unknown"))
                action = decide(state)
                if action == "wait":
                    if state == "ready":
                        backoff = 5
                    self.stop_event.wait(5 if state == "ready" else 2)
                    continue
                if action == "stop-then-start":
                    # The runtime keeps the server's exit code and its last lines.
                    # Copy them here: this log is the one the installing person can read.
                    detail = ""
                    if state == "failed":
                        detail = (f" (error={current.get('error')} exit={current.get('exit_code')} "
                                  f"engine_exit={current.get('engine_exit_code')})")
                    self.log(f"server state is {state}{detail}; ending what is left before a new start")
                    tail = current.get("log_tail")
                    if tail:
                        self.log("server log: " + str(tail)[:2000])
                    self._stop_runtime(30)
                self.log(f"starting {conf['config']} (profile {PROFILE})")
                self._runtime.start(PROFILE, wait=0, shutdown_timeout=conf["shutdown_timeout"])
                self.stop_event.wait(min(backoff, 10))
                # A model that fails at once would otherwise be started again every
                # pass; grow the pause until a start reaches "ready" and resets it.
                backoff = min(backoff * 2, 60)
            except ServiceError as exc:
                self.log(f"cannot start: {exc}")
                self.stop_event.wait(backoff)
                backoff = min(backoff * 2, 60)
            except Exception as exc:                      # noqa: BLE001 - a watchdog must not die
                self.log(f"start failed: {exc.__class__.__name__}: {exc}")
                self._tail_runtime_logs()
                self.stop_event.wait(backoff)
                backoff = min(backoff * 2, 60)
        self.log("stop requested; stopping the server")
        if self._runtime is not None:
            try:
                conf = load_conf(self.conf_path)
            except ServiceError:
                conf = {"shutdown_timeout": 30}
            self._stop_runtime(conf["shutdown_timeout"] + 60)
        self.log("stopped")


_HANDLER_REF = None
_MAIN_REF = None


def run_service(name: str = SERVICE_NAME, conf_path: Path = CONF_PATH) -> int:
    """The SCM entry point. Returns only after the service stops."""
    require_windows()
    global _HANDLER_REF, _MAIN_REF
    prepare_environment()
    supervisor = Supervisor(name, conf_path)

    @LPHANDLER_FUNCTION_EX
    def on_control(command, event_type, event_data, context):
        return supervisor.control(command)

    @LPSERVICE_MAIN_FUNCTIONW
    def on_main(argc, argv):
        supervisor.main()

    _HANDLER_REF, _MAIN_REF = on_control, on_main
    table = (SERVICE_TABLE_ENTRYW * 2)()
    table[0].lpServiceName = name
    table[0].lpServiceProc = on_main
    table[1].lpServiceName = None
    table[1].lpServiceProc = LPSERVICE_MAIN_FUNCTIONW()
    if not advapi32.StartServiceCtrlDispatcherW(table):
        code = ctypes.get_last_error()
        if code == ERROR_FAILED_SERVICE_CONTROLLER_CONNECT:
            raise ServiceError("'run' is started by the Windows Service Control Manager, not by hand. "
                               "Use: Scripts\\Strata_Service.ps1 -Action Start", 5)
        raise ServiceError(f"cannot connect to the service manager: {_word(code)}", 5)
    return 0


def prepare_environment() -> None:
    """Point Strata's per-user state at the service's own folder.

    A LocalSystem service has no user profile of its own. Its runtime state and
    profiles are private to the account that writes them (Strata restricts them to
    the current user), so they must be written by the service, not read from the
    installing person's %APPDATA%.
    """
    os.environ["PYTHONUNBUFFERED"] = "1"
    os.environ["PYTHONIOENCODING"] = "utf-8"
    os.environ["PYTHONUTF8"] = "1"
    os.environ["STRATA_RUNTIME_DIR"] = str(STATE_DIR / "runtime")
    os.environ["STRATA_PROFILE_DIR"] = str(STATE_DIR / "profiles")
    os.environ["STRATA_LAUNCH_PRESET_DIR"] = str(STATE_DIR / "launch-presets")
    os.environ["STRATA_COMPONENTS_DIR"] = str(STATE_DIR / "components")
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))


# ------------------------------------------------------------------------------------------------ commands

def _health_url(conf: dict) -> str:
    host = conf.get("host") or "127.0.0.1"
    if host in ("0.0.0.0", "::", "*"):
        host = "127.0.0.1"
    port = conf.get("port")
    if port is None:
        try:
            data = json.loads(Path(conf["config"]).read_text(encoding="utf-8-sig"))
            port = data.get("port") if isinstance(data.get("port"), int) else None
        except (OSError, ValueError):
            port = None
    return f"http://{host}:{port or 8080}/health"


def wait_healthy(conf: dict, seconds: float) -> dict | None:
    url = _health_url(conf)
    deadline = time.monotonic() + max(1, seconds)
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return json.loads(response.read().decode("utf-8", "replace"))
        except (OSError, ValueError, urllib.error.URLError):
            pass
        time.sleep(1)
    return None


def command_install(args) -> int:
    require_windows()
    if not is_admin():
        raise ServiceError("Administrator rights are needed to install the service. Run "
                           "Scripts\\Strata_Service.ps1 -Action Install, which asks for them.", 5)
    if not 1 <= args.start_wait <= 1800:
        raise ServiceError("--start-wait must be 1..1800 seconds", 2)
    SERVICE_ROOT.mkdir(parents=True, exist_ok=True)
    chosen: Path | None = None
    wrote_conf = False
    if args.config:
        chosen = Path(args.config).expanduser()
        if not chosen.is_absolute():
            chosen = (Path.cwd() / chosen).resolve()
        if run_config_summary(chosen) is None:
            raise ServiceError(f"{chosen} is not a Strata model config: it needs 'exe' and 'args'.", 2)
    elif CONF_PATH.is_file():
        try:
            chosen = Path(load_conf(CONF_PATH)["config"])
        except ServiceError:
            chosen = None
        if chosen and Path(chosen).is_file():
            print(f"Keeping the existing settings: {CONF_PATH}")
        else:
            chosen = None
    if chosen is None:
        found = default_config(ROOT)
        if found is None:
            raise ServiceError(f"No model config (strata-*.json) was found in {ROOT}. Run Strata once "
                               "(Scripts\\Strata_Start.bat) or pass -Config PATH.", 2)
        chosen = found
    if args.config or not CONF_PATH.is_file():
        write_conf(CONF_PATH, chosen, discover_models(ROOT))
        wrote_conf = True
        print(f"Wrote {CONF_PATH}")
    grant_user_write(CONF_PATH)
    python = stable_python(ROOT)
    bin_path = build_bin_path(python, SERVICE_SCRIPT, args.name)
    action = register_service(args.name, bin_path, force=args.force)
    # The watchdog restarts the server; this is the SCM restarting the watchdog itself
    # if it ever exits. The exit code is the only thing checked (sc's text is localized).
    subprocess.run(["sc.exe", "failure", args.name, "reset=", "86400",
                    "actions=", "restart/5000/restart/30000/restart/60000"],
                   capture_output=True, timeout=15, check=False)
    print(f"Service '{args.name}' {action}: {bin_path}")
    print(f"Account: LocalSystem; start: automatic; model: {chosen}")
    if args.no_start:
        print("Not started (-NoStart).")
        return 0
    result = start_service(args.name, timeout=30)
    if result["state"] != "running":
        raise ServiceError(f"Service '{args.name}' did not reach Running (state: {result['state']}). "
                           f"See {LOG_DIR}.", 5)
    conf = load_conf(CONF_PATH)
    health = wait_healthy(conf, args.start_wait)
    if health is None:
        print(f"Service '{args.name}' is running, but {_health_url(conf)} has not answered yet. "
              f"The model may still be loading; check: Strata_Service.ps1 -Action Status. Logs: {LOG_DIR}")
    else:
        print(f"Service '{args.name}' is serving: {_health_url(conf)} "
              f"(model {health.get('model') or 'unknown'})")
    if wrote_conf:
        print(f"Edit {CONF_PATH} to choose another model, then restart the service.")
    return 0


def command_uninstall(args) -> int:
    require_windows()
    if not is_admin():
        raise ServiceError("Administrator rights are needed to remove the service.", 5)
    status = service_status(args.name)
    if not status["installed"]:
        print(f"Service '{args.name}' is not installed.")
    else:
        if status["state"] not in ("stopped",):
            print(f"Stopping '{args.name}' ...")
            stop_service(args.name, timeout=180)
        delete_service(args.name)
        print(f"Service '{args.name}' removed.")
    if args.purge:
        if SERVICE_ROOT.exists():
            shutil.rmtree(SERVICE_ROOT)
            print(f"Removed {SERVICE_ROOT} (settings, logs and the service's private state).")
        else:
            print(f"{SERVICE_ROOT} was already gone.")
    else:
        print(f"Kept {SERVICE_ROOT} (-Purge removes it). Models and the Strata install were not touched.")
    return 0


def command_start(args) -> int:
    require_windows()
    if not is_admin():
        raise ServiceError("Administrator rights are needed to start the service.", 5)
    result = start_service(args.name, timeout=30)
    if result["state"] != "running":
        raise ServiceError(f"Service '{args.name}' did not reach Running (state: {result['state']}). "
                           f"See {LOG_DIR}.", 5)
    print(f"Service '{args.name}' is running (process {result['pid']}). The model continues loading in "
          f"the background; check: Strata_Service.ps1 -Action Status.")
    return 0


def command_stop(args) -> int:
    require_windows()
    if not is_admin():
        raise ServiceError("Administrator rights are needed to stop the service.", 5)
    try:
        conf = load_conf(CONF_PATH)
        timeout = conf["shutdown_timeout"] + 60
    except ServiceError:
        timeout = 120
    result = stop_service(args.name, timeout=timeout)
    print(f"Service '{args.name}' is {result['state']}.")
    return 0


def command_restart(args) -> int:
    command_stop(args)
    return command_start(args)


def command_status(args) -> int:
    require_windows()
    status = service_status(args.name)
    if not status["installed"]:
        print(f"Service '{args.name}' is not installed.")
        print(f"Install it with: Scripts\\Strata_Service.ps1 -Action Install")
        return 0
    words = status["state"]
    if status["pid"]:
        words += f" (process {status['pid']})"
    print(f"Service    {args.name}: {words}")
    try:
        conf = load_conf(CONF_PATH)
    except ServiceError as exc:
        print(f"Settings   {exc}")
        print(f"Logs       {LOG_DIR}")
        return 0
    print(f"Model      {conf['config']}")
    try:
        data = json.loads(Path(conf["config"]).read_text(encoding="utf-8-sig"))
        if data.get("log"):
            print(f"Engine log {data['log']}")
    except (OSError, ValueError):
        pass
    url = _health_url(conf)
    if status["state"] == "running":
        health = wait_healthy(conf, 2)
        if health is None:
            print(f"Endpoint   {url}  (not answering yet; the model may still be loading)")
        else:
            print(f"Endpoint   {url}  (ok; model {health.get('model') or 'unknown'})")
    else:
        print(f"Endpoint   {url}  (not checked: the service is {status['state']})")
    print(f"Settings   {CONF_PATH}")
    print(f"State      {STATE_DIR}")
    print(f"Logs       {LOG_DIR}")
    newest = sorted(LOG_DIR.glob("service-*.log"), key=lambda p: p.stat().st_mtime) if LOG_DIR.is_dir() else []
    if newest:
        print(f"Latest log {newest[-1]}")
    return 0


def command_logs(args) -> int:
    files = sorted(LOG_DIR.glob("service-*.log"), key=lambda p: p.stat().st_mtime) if LOG_DIR.is_dir() else []
    if not files:
        print(f"No service log yet in {LOG_DIR}.")
        return 0
    print(files[-1])
    for line in _tail(files[-1], lines=max(1, args.tail)):
        print(line)
    return 0


def command_run(args) -> int:
    return run_service(args.name, CONF_PATH)


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="Strata_Service.py",
                                 description="Install and control Strata as a Windows service.")
    sub = ap.add_subparsers(dest="command", required=True)
    install = sub.add_parser("install", help="register the service (Administrator)")
    install.add_argument("--config", metavar="PATH", help="the model config to serve (default: the newest strata-*.json)")
    install.add_argument("--name", default=SERVICE_NAME, help="service name (default: Strata)")
    install.add_argument("--no-start", action="store_true", help="register but do not start it")
    install.add_argument("--force", action="store_true", help="replace a service of the same name not made by this script")
    install.add_argument("--start-wait", type=float, default=90, help="seconds to wait for /health after starting")
    uninstall = sub.add_parser("uninstall", help="stop and remove the service (Administrator)")
    uninstall.add_argument("--name", default=SERVICE_NAME)
    uninstall.add_argument("--purge", action="store_true", help="also delete settings, logs and service state")
    for command, text in (("start", "start the service (Administrator)"),
                          ("stop", "stop the service, draining the server (Administrator)"),
                          ("restart", "stop then start (Administrator)"),
                          ("status", "say what the service and the model are doing"),
                          ("logs", "show the end of the newest service log")):
        item = sub.add_parser(command, help=text)
        item.add_argument("--name", default=SERVICE_NAME)
        if command == "logs":
            item.add_argument("--tail", type=int, default=60)
    run = sub.add_parser("run", help="the Service Control Manager entry point")
    run.add_argument("--name", default=SERVICE_NAME)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "run":
            return command_run(args)
        handlers = {"install": command_install, "uninstall": command_uninstall,
                    "start": command_start, "stop": command_stop, "restart": command_restart,
                    "status": command_status, "logs": command_logs}
        return handlers[args.command](args)
    except ServiceError as exc:
        print(f"strata service: {exc}", file=sys.stderr)
        return exc.code
    except KeyboardInterrupt:
        return 130
    except OSError as exc:
        print(f"strata service: {exc}", file=sys.stderr)
        return 5


if __name__ == "__main__":
    raise SystemExit(main())
