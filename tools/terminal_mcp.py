#!/usr/bin/env python3
r"""
A terminal MCP server for the Strata model's chat page.

WHAT THIS IS

`serve/mcp.py` lets the Strata model call tools from MCP servers listed in
`strata-<model>.json` under `mcp_servers`. This is one such server: it gives the
model a PowerShell (or cmd) command line on this machine, and the ability to
open a real, visible console window at a folder - the terminal a person would
open themselves.

Standard library only, for the same reason `tools/filesystem_mcp.py` and
`tools/web_search_mcp.py` are: a tool that needs a package installed is a tool
that fails the first time it is called. Nothing here is imported that Python on
Windows does not ship.

TOOLS

  run_command     run a command line in PowerShell or cmd, wait for it, give back
                  the exit code and the output. A timeout and an output cap, so
                  one command cannot hold the chat or flood the context.
  open_terminal   open a visible console window at a folder, optionally with a
                  command already typed in. For anything that must be watched or
                  typed into rather than captured: a long build, a TUI, a repair
                  the person should see happen.
  list_terminals  the windows this server opened, and whether each is still open.
  close_terminal  close one of them.

THERE IS NO COMMAND POLICY HERE, ON PURPOSE

The filesystem tool is confined and gated by a policy file because it edits
files, and a fetched web page can ask it to. This one is different: the user
asked for a real terminal, and a terminal that quietly refuses commands is a
terminal whose refusals send the model hunting for a way round them - a series
of half-working commands with no explanation is worse than one honest no. So the
line is drawn at the whole tool instead: this server is either in a config or it
is not, and when it is, the command runs with the same rights as the Strata
server that started it. Take the server out of `mcp_servers` to take the
capability away; that is the switch, and it is the only one.

What is still enforced, because they are not restrictions on what may be done
but on how a tool behaves: a command has a deadline (default 120 s, at most one
hour), its output is capped, and every command is written to an audit log before
it is run and after it finishes, with its exit code. A window opened by
`open_terminal` is recorded too. The log is the record of what was actually run,
which is the thing a person wants afterwards and cannot get from a chat
transcript that was summarised.

The model is told none of this: it sees four tools. `--cwd` decides where a
command starts when it does not say.

    python tools/terminal_mcp.py                       # run as a server (stdio)
    python tools/terminal_mcp.py --selftest            # exercise every tool and exit
    python tools/terminal_mcp.py --cwd E:\             # where commands start
    python tools/terminal_mcp.py --shell cmd           # cmd.exe instead of PowerShell
    python tools/terminal_mcp.py --pwsh                # PowerShell 7 if it is installed
    python tools/terminal_mcp.py --log D:\temp\cmds.log

SECURITY NOTE, SAID PLAINLY

Strata also has web search. A page it fetches can contain instructions, and with
this server in the config those instructions can become commands on this machine.
That is the trade the user asked for. The audit log is the alarm: it names every
command, its folder, and its exit code, in order, at the time it ran.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

PROTOCOL = "2025-06-18"
SERVER_NAME = "terminal"
SERVER_VERSION = "1.0.0"

DEFAULT_TIMEOUT = 120.0
MAX_TIMEOUT = 3600.0
DEFAULT_OUTPUT_CHARS = 60_000     # what one call may put in front of the model
HEAD_CHARS = 15_000               # of that, kept from the front ...
TAIL_CHARS = 45_000               # ... and the back, which is where an error usually is

CREATE_NEW_CONSOLE = 0x00000010   # a real window, not a pipe
CREATE_NEW_PROCESS_GROUP = 0x00000200   # so a timeout can end the whole tree, not just the shell

# The windows this server opened: pid -> {shell, cwd, started, proc}. Kept so
# list_terminals can say whether one is still open. A server restarted by Strata
# after a crash loses the list; the windows themselves stay open.
_WINDOWS: dict[int, dict] = {}

_CWD = Path.cwd()
_SHELL = "powershell"             # "powershell" | "pwsh" | "cmd"
_POWERSHELL = "powershell.exe"
_LOG: Path | None = None


def _force_utf8() -> None:
    """Windows opens stdio with the ANSI code page; the wire format is UTF-8."""
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


_force_utf8()


def log(message: str) -> None:
    print(f"[{SERVER_NAME}] {message}", file=sys.stderr, flush=True)


def audit(entry: dict) -> None:
    """One JSON line per command, before and after it runs.

    Written before the command starts so a command that hangs or crashes the
    server is still on record, and again when it finishes so the record carries
    the exit code. A failed write is not a reason to refuse the command: the log
    is a record, not a gate.
    """
    if _LOG is None:
        return
    try:
        _LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(_LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"time": time.strftime("%Y-%m-%dT%H:%M:%S"), **entry},
                                ensure_ascii=False) + "\n")
    except OSError:
        pass


# --------------------------------------------------------------------------
# The shell
# --------------------------------------------------------------------------

def _shell_argv(command: str, shell: str) -> list[str]:
    """The program and arguments that run one command line and exit with its status."""
    if shell == "cmd":
        comspec = os.environ.get("COMSPEC") or "cmd.exe"
        # 65001 so the output can be decoded as UTF-8 rather than the OEM code
        # page, which would turn a folder name with an accent into a question mark.
        return [comspec, "/d", "/s", "/c", f"chcp 65001 >nul & {command}"]
    exe = _POWERSHELL
    # NoLogo: no banner in the captured output. NoProfile unless asked for: a
    # profile that prints a greeting would put it in every tool result, and a
    # profile that sets a prompt is of no use to a non-interactive call.
    # NonInteractive: a command that waits for a keypress fails instead of
    # holding the chat until the timeout.
    return [exe, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command",
            UTF8_PREAMBLE + command]


# PowerShell writes a pipe in the console's code page. Both of these have to be
# set for captured text to come back as UTF-8, and $ProgressPreference keeps a
# download's progress bar out of the captured output. They are short on purpose:
# -Command repeats the command line in an error's context, so every statement
# added here is noise added to every failure the model reads.
UTF8_PREAMBLE = ("$ProgressPreference='SilentlyContinue';"
                 "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
                 "$OutputEncoding=[Text.Encoding]::UTF8;")


def _decode(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace")


def _cap(text: str) -> str:
    if len(text) <= DEFAULT_OUTPUT_CHARS:
        return text
    omitted = len(text) - HEAD_CHARS - TAIL_CHARS
    return (text[:HEAD_CHARS]
            + f"\n\n[... {omitted:,} characters omitted ...]\n\n"
            + text[-TAIL_CHARS:])


def _where(raw: str | None) -> Path:
    """The folder a command runs in. A missing folder is an error the model can act on, not a silent fallback."""
    folder = Path(os.path.expandvars(str(raw).strip().strip('"'))).expanduser() if raw else _CWD
    if not folder.is_absolute():
        folder = _CWD / folder
    if not folder.is_dir():
        raise ValueError(f"no such folder: {folder}")
    return folder


def _kill(pid: int) -> None:
    """End a process and everything it started. The shell alone is not enough: a child keeps the pipe open."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                       capture_output=True, creationflags=CREATE_NO_WINDOW_FOR_CHILD())
    else:
        try:
            os.killpg(pid, 9)
        except OSError:
            try:
                os.kill(pid, 9)
            except OSError:
                pass


def CREATE_NO_WINDOW_FOR_CHILD() -> int:      # noqa: N802 - kept as a function so the constant is not needed on POSIX
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def run_command(command: str, cwd: str | None = None, shell: str | None = None,
                timeout_s: float | None = None) -> str:
    """Run one command line and return its exit code and output. Blocks until it ends or the deadline passes."""
    command = "" if command is None else str(command)
    if not command.strip():
        raise ValueError("no command given")
    shell = (shell or _SHELL).lower()
    if shell not in ("powershell", "pwsh", "cmd"):
        raise ValueError(f"unknown shell {shell!r}: use powershell, pwsh or cmd")
    folder = _where(cwd)
    timeout = DEFAULT_TIMEOUT if timeout_s is None else float(timeout_s)
    timeout = max(1.0, min(timeout, MAX_TIMEOUT))

    argv = _shell_argv(command, shell)
    audit({"event": "start", "tool": "run_command", "shell": shell,
           "cwd": str(folder), "command": _cap(command), "timeout_s": timeout})
    log(f"RUN  [{shell}] ({folder}) {command[:300]}")
    started = time.monotonic()

    extra = {}
    if os.name == "nt":
        extra["creationflags"] = CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW_FOR_CHILD()
    else:
        extra["start_new_session"] = True
    try:
        proc = subprocess.Popen(argv, cwd=str(folder), stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, **extra)
    except OSError as exc:
        raise ValueError(f"could not start {argv[0]}: {exc}") from None

    timed_out = False
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill(proc.pid)
        try:
            out, err = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            out, err = b"", b""
    seconds = time.monotonic() - started
    text_out, text_err = _decode(out or b""), _decode(err or b"")
    audit({"event": "end", "tool": "run_command", "exit": proc.returncode, "seconds": round(seconds, 2),
           "timed_out": timed_out})

    if timed_out:
        log(f"TIME OUT after {timeout:g} s; the process was ended")
        return (f"timed out after {timeout:g} s - the command was stopped"
                f"{self_note()}.\n\n--- output so far ---\n{_cap(text_out + text_err).strip()}")
    log(f"EXIT {proc.returncode} in {seconds:.2f} s")

    parts = [f"exit code: {proc.returncode}",
             f"duration: {seconds:.2f} s",
             f"cwd: {folder}",
             f"shell: {shell}"]
    body = text_out.rstrip()
    parts.append("\n--- stdout ---\n" + (body if body else "(nothing)"))
    error = text_err.rstrip()
    if error:
        parts.append("\n--- stderr ---\n" + error)
    return _cap("\n".join(parts))


def self_note() -> str:
    return " (it may have left child processes behind; check with Get-Process)"


def open_terminal(cwd: str | None = None, shell: str | None = None, command: str | None = None) -> str:
    """Open a visible console window at a folder. It stays open after this call returns."""
    shell = (shell or _SHELL).lower()
    if shell not in ("powershell", "pwsh", "cmd"):
        raise ValueError(f"unknown shell {shell!r}: use powershell, pwsh or cmd")
    folder = _where(cwd)
    if os.name != "nt":
        raise ValueError("open_terminal needs Windows: there is no console window to open on this system")

    if shell == "cmd":
        comspec = os.environ.get("COMSPEC") or "cmd.exe"
        argv = [comspec, "/k"] + ([command] if command else [])
    else:
        # -NoExit keeps the window open at the prompt after the command, which
        # is the point: the person can look, and can then type.
        argv = [_POWERSHELL, "-NoLogo", "-NoExit"] + (["-Command", command] if command else [])

    try:
        proc = subprocess.Popen(argv, cwd=str(folder), creationflags=CREATE_NEW_CONSOLE)
    except OSError as exc:
        raise ValueError(f"could not open a window: {exc}") from None
    _WINDOWS[proc.pid] = {"shell": shell, "cwd": str(folder), "started": time.time(), "proc": proc}
    audit({"event": "open", "tool": "open_terminal", "shell": shell, "cwd": str(folder),
           "pid": proc.pid, "command": command or ""})
    log(f"OPEN [{shell}] pid {proc.pid} at {folder}")
    return (f"opened a {shell} window (pid {proc.pid}) at {folder}"
            + (f", running: {command}" if command else "")
            + ". It stays open; the person can use it.")


def list_terminals() -> str:
    """The windows this server opened and whether each is still open."""
    if not _WINDOWS:
        return "no windows have been opened by this server"
    lines = []
    for pid, info in sorted(_WINDOWS.items()):
        alive = info["proc"].poll() is None
        minutes = int((time.time() - info["started"]) / 60)
        lines.append(f"pid {pid}  {info['shell']:<10} {info['cwd']}  "
                     f"{'open' if alive else 'closed'}  (opened {minutes} min ago)")
    return "\n".join(lines)


def close_terminal(pid: int | None = None) -> str:
    """Close one window this server opened (or all of them when no pid is given)."""
    if not _WINDOWS:
        return "no windows have been opened by this server"
    targets = [int(pid)] if pid is not None else list(_WINDOWS)
    closed, already = [], []
    for target in targets:
        info = _WINDOWS.get(target)
        if info is None:
            continue
        if info["proc"].poll() is None:
            _kill(target)
            closed.append(target)
        else:
            already.append(target)
    audit({"event": "close", "tool": "close_terminal", "pids": targets})
    pieces = []
    if closed:
        pieces.append("closed " + ", ".join(f"pid {p}" for p in closed))
    if already:
        pieces.append("already closed: " + ", ".join(f"pid {p}" for p in already))
    if not pieces:
        return f"no window with pid {pid} was opened by this server"
    return "; ".join(pieces)


# --------------------------------------------------------------------------
# The tool list the model sees
# --------------------------------------------------------------------------

TOOLS = [
    {
        "name": "run_command",
        "description": (
            "Run a command line on this Windows machine and return its exit code and output. "
            "Use it to inspect the system, run git, run python or any program, and read what came back. "
            "PowerShell is the default shell. Long output is truncated. A command that runs past its "
            "timeout is stopped."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {"type": "string",
                            "description": "The command line, as it would be typed in the terminal."},
                "cwd": {"type": "string",
                        "description": "Folder to run it in. Default: the folder this server started in."},
                "shell": {"type": "string", "enum": ["powershell", "pwsh", "cmd"],
                          "description": "Default powershell."},
                "timeout_s": {"type": "integer",
                              "description": f"Seconds before it is stopped. Default {DEFAULT_TIMEOUT:g}, "
                                             f"most {MAX_TIMEOUT:g}.",
                              "minimum": 1, "maximum": int(MAX_TIMEOUT)},
            },
            "required": ["command"],
        },
    },
    {
        "name": "open_terminal",
        "description": (
            "Open a visible PowerShell (or cmd) window at a folder and leave it open, optionally "
            "running one command first. Use it when the person should watch something run, or needs "
            "a terminal to type into."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "cwd": {"type": "string", "description": "Folder the window opens in."},
                "shell": {"type": "string", "enum": ["powershell", "pwsh", "cmd"],
                          "description": "Default powershell."},
                "command": {"type": "string",
                            "description": "Optional command to run in the new window."},
            },
        },
    },
    {
        "name": "list_terminals",
        "description": "List the terminal windows this server opened and whether each is still open.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "close_terminal",
        "description": "Close a terminal window this server opened. With no pid, close all of them.",
        "inputSchema": {
            "type": "object",
            "properties": {"pid": {"type": "integer", "description": "Which window."}},
        },
    },
]


def call_tool(name: str, arguments: dict) -> str:
    arguments = arguments or {}
    if name == "run_command":
        return run_command(arguments.get("command", ""), arguments.get("cwd"),
                           arguments.get("shell"), arguments.get("timeout_s"))
    if name == "open_terminal":
        return open_terminal(arguments.get("cwd"), arguments.get("shell"), arguments.get("command"))
    if name == "list_terminals":
        return list_terminals()
    if name == "close_terminal":
        return close_terminal(arguments.get("pid"))
    raise ValueError(f"unknown tool: {name}")


# --------------------------------------------------------------------------
# JSON-RPC over stdio: one message per line
# --------------------------------------------------------------------------

def send(message: dict) -> None:
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def send_result(request_id, result) -> None:
    send({"jsonrpc": "2.0", "id": request_id, "result": result})


def send_error(request_id, code: int, message: str) -> None:
    send({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})


def handle(message: dict) -> None:
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") or {}

    if request_id is None and method != "ping":
        return

    if method == "initialize":
        send_result(request_id, {
            "protocolVersion": PROTOCOL,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })
        return

    if method == "ping":
        send_result(request_id, {})
        return

    if method == "tools/list":
        send_result(request_id, {"tools": TOOLS})
        return

    if method == "tools/call":
        name = params.get("name", "")
        arguments = params.get("arguments") or {}
        try:
            text = call_tool(name, arguments)
            send_result(request_id, {"content": [{"type": "text", "text": text}], "isError": False})
        except Exception as exc:  # noqa: BLE001 - a failed tool is a result, not a crash
            log(f"{name} failed: {exc}")
            send_result(request_id, {"content": [{"type": "text", "text": f"error: {exc}"}],
                                     "isError": True})
        return

    if request_id is not None:
        send_error(request_id, -32601, f"method not found: {method}")


def serve() -> int:
    log(f"ready on stdio (protocol {PROTOCOL}), shell {_SHELL} ({_POWERSHELL}), "
        f"cwd {_CWD}, {len(TOOLS)} tools: {', '.join(t['name'] for t in TOOLS)}")
    if _LOG:
        log(f"audit log: {_LOG}")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            log(f"ignoring a line that is not JSON: {line[:80]!r}")
            continue
        try:
            handle(message)
        except Exception as exc:  # noqa: BLE001 - one bad message must not kill the server
            log(f"error handling {message.get('method')}: {exc}")
    return 0


# --------------------------------------------------------------------------
# Selftest
# --------------------------------------------------------------------------

def selftest() -> int:
    """Exercise the tools for real, so a failure is found here and not in a chat."""
    failures = 0
    print(f"  initialize: {json.dumps({'protocolVersion': PROTOCOL})}")
    print(f"  shell: {_SHELL} -> {_POWERSHELL}")
    print(f"  cwd: {_CWD}")
    print(f"  tools: {[t['name'] for t in TOOLS]}")

    def check(label: str, fn, expect=None):
        nonlocal failures
        print(f"\n  {label}")
        try:
            text = fn()
            print("   ", text.replace("\n", "\n    ")[:700])
            if expect is not None and expect not in text:
                print(f"    FAILED: expected {expect!r} in the result")
                failures += 1
        except Exception as exc:  # noqa: BLE001
            print(f"    FAILED: {exc}")
            failures += 1

    def check_raises(label: str, fn, expect: str):
        """A refusal is raised here and becomes an "error:" result in the MCP handler."""
        nonlocal failures
        print(f"\n  {label}")
        try:
            fn()
            print("    FAILED: should have raised")
            failures += 1
        except Exception as exc:  # noqa: BLE001
            print(f"    correctly raised: {exc}")
            if expect not in str(exc):
                print(f"    FAILED: expected {expect!r} in the message")
                failures += 1

    check("run_command('$PSVersionTable.PSVersion.ToString()')",
          lambda: call_tool("run_command", {"command": "$PSVersionTable.PSVersion.ToString()"}))

    check("run_command with non-ASCII output",
          lambda: call_tool("run_command", {"command": "Write-Output 'h\u00e9llo \u2713 \u65e5\u672c\u8a9e'"}),
          expect="\u65e5\u672c\u8a9e")

    check("run_command in another folder (E:\\)",
          lambda: call_tool("run_command", {"command": "(Get-Location).Path", "cwd": "E:\\"}),
          expect="E:\\")

    check("run_command whose command fails",
          lambda: call_tool("run_command", {"command": "Write-Output 'about to fail'; exit 7"}),
          expect="exit code: 7")

    check("run_command errors, which the model reads as a result",
          lambda: call_tool("run_command", {"command": "Get-Item C:\\definitely-not-here"}),
          expect="stderr")

    check("run_command stopped by its timeout",
          lambda: call_tool("run_command", {"command": "Start-Sleep -Seconds 30", "timeout_s": 2}),
          expect="timed out")

    check("run_command via cmd",
          lambda: call_tool("run_command", {"command": "echo from cmd & cd", "shell": "cmd"}),
          expect="from cmd")

    check_raises("a folder that does not exist is refused",
                 lambda: call_tool("run_command", {"command": "dir", "cwd": "C:\\no-such-folder-here"}),
                 expect="no such folder")

    check_raises("an empty command is refused",
                 lambda: call_tool("run_command", {"command": "   "}),
                 expect="no command given")

    def window():
        opened = call_tool("open_terminal", {"cwd": "E:\\", "command": "Write-Output 'opened by the selftest'"})
        listed = call_tool("list_terminals", {})
        closed = call_tool("close_terminal", {})
        return f"{opened}\n{listed}\n{closed}"
    check("open_terminal, list_terminals, close_terminal", window, expect="pid")

    print(f"\n  {'ALL SELFTESTS PASSED' if not failures else f'{failures} FAILED'}")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    global _CWD, _SHELL, _POWERSHELL, _LOG
    ap = argparse.ArgumentParser(description="A terminal MCP server for Strata.")
    ap.add_argument("--cwd", help="the folder commands start in")
    ap.add_argument("--shell", choices=("powershell", "pwsh", "cmd"), default="powershell",
                    help="the default shell (powershell, pwsh or cmd)")
    ap.add_argument("--pwsh", action="store_true",
                    help="use PowerShell 7 (pwsh) instead of Windows PowerShell when --shell is powershell")
    ap.add_argument("--log", help="where to write the audit log "
                                  "(default %%LOCALAPPDATA%%\\Strata\\terminal\\commands.log)")
    ap.add_argument("--selftest", action="store_true", help="exercise the tools and exit")
    args = ap.parse_args(argv)

    if args.cwd:
        folder = Path(os.path.expandvars(args.cwd)).expanduser()
        if not folder.is_dir():
            print(f"error: --cwd {args.cwd} is not a folder", file=sys.stderr)
            return 2
        _CWD = folder
    if args.shell:
        _SHELL = args.shell
    if _SHELL == "pwsh" or (args.pwsh and _SHELL == "powershell"):
        found = shutil.which("pwsh")
        if not found:
            print("error: pwsh (PowerShell 7) is not on PATH", file=sys.stderr)
            return 2
        _POWERSHELL = found
        _SHELL = "pwsh"
    else:
        _POWERSHELL = shutil.which("powershell.exe") or "powershell.exe"
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    _LOG = Path(args.log) if args.log else Path(base) / "Strata" / "terminal" / "commands.log"

    return selftest() if args.selftest else serve()


if __name__ == "__main__":
    raise SystemExit(main())
