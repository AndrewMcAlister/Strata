#!/usr/bin/env python3
"""Give every Strata model config the same MCP tools.

The MCP servers are a property of the Strata install, not of which weights are loaded, so a model config that lacks them
is a model that truthfully tells the user it has no filesystem access. That is how the q4 configs, built from scratch,
ended up without the tools the iq2/iq3 configs had, and how the Inkling config the Windows service serves ended up with
none. Keep them in one place, here.

The configs live where the start menu, the app and the Windows service look for them: this folder and `terminal\\` (the
0.2.16 layout). Every `strata-*.json` that names an `exe` and `args` is stamped, in both.

The folders the tools need are READ FROM STRATA'S OWN SETTING, not written down here. The `models` tool is rooted at
whatever `data_dir` names at the time (strata_control/discovery.py, the same answer `strata launch` gets), so moving the
model files to another drive and running this again is all it takes for the tool to follow them. The `models` tool is
read-only: it is started with models-readonly-policy.json, which prohibits every change, so a chat can look at the model
library and never rewrite it. The `skills` tool reads the skills folder (`D:\\skills`) and never writes.

    python tools/sync_model_tools.py            # stamp every model config
    python tools/sync_model_tools.py --check    # report, change nothing

The write preserves each file's mtime on purpose: Strata_Start.bat orders the model menu by write time, and syncing tools
should not reshuffle it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for _base in (ROOT / "terminal", ROOT):          # 0.2.16 has the packages under terminal\; an older clone at the top
    if str(_base) not in sys.path:
        sys.path.insert(0, str(_base))

from strata_control import discovery  # noqa: E402 - needs ROOT on the path first


def _python() -> str:
    """The interpreter the tools run on: this install's venv in either layout, else this one."""
    for candidate in (ROOT / ".venv" / "Scripts" / "python.exe",
                      ROOT / "terminal" / ".venv" / "Scripts" / "python.exe",
                      ROOT / ".venv" / "bin" / "python3",
                      ROOT / "terminal" / ".venv" / "bin" / "python3"):
        if candidate.exists():
            return str(candidate)
    return sys.executable


EXE = _python()
TOOL = str(ROOT / "tools" / "filesystem_mcp.py")
WEB = str(ROOT / "tools" / "web_search_mcp.py")
TERMINAL = str(ROOT / "tools" / "terminal_mcp.py")
SKILLS = str(ROOT / "tools" / "skills_mcp.py")
POLICY = r"D:\strata-data.json"                      # which folders on D: a file change may touch
MODELS_POLICY = str(ROOT / "models-readonly-policy.json")   # `models` tool: no change is ever allowed
TERMINAL_CWD = "D:\\"                                # where a command starts when the model does not name a folder
SKILLS_ROOT = r"D:\skills"                           # the skills folder the `skills` tool reads
CONFIG_DIRS = (ROOT, ROOT / "terminal")              # where the start menu, the app and the service look

MCP = {"timeout_s": 60, "max_result_chars": 20000, "max_rounds": 50}


def servers(data_dir: Path | None) -> dict:
    """The tool servers every config gets. `models` is left out when the data folder is not there to read."""
    out = {
        "web": {"command": EXE, "args": [WEB]},
        "files": {"command": EXE,
                  "args": [TOOL, "--root", "D:\\", "--policy", POLICY]},
    }
    if data_dir is not None and Path(data_dir).is_dir():
        out["models"] = {"command": EXE,
                         "args": [TOOL, "--root", str(data_dir), "--policy", MODELS_POLICY]}
    if Path(SKILLS_ROOT).is_dir() and Path(SKILLS).is_file():
        out["skills"] = {"command": EXE, "args": [SKILLS, "--root", SKILLS_ROOT]}
    out["terminal"] = {"command": EXE, "args": [TERMINAL, "--cwd", TERMINAL_CWD]}
    return out


def model_configs():
    """Every strata-*.json the start menu or the service would offer, in name order."""
    seen = set()
    for folder in CONFIG_DIRS:
        for path in sorted(folder.glob("strata-*.json")):
            if path.name.endswith(".shared-settings.json"):
                continue
            key = os.path.normcase(str(path.resolve()))
            if key in seen:
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict) or not data.get("exe") or not data.get("args"):
                continue
            seen.add(key)
            yield path, data



def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true",
                        help="report configs that differ; change nothing")
    args = parser.parse_args(argv)

    try:
        data_dir, source = discovery.data_dir(ROOT.parent / "Strata-data")
    except Exception as exc:  # noqa: BLE001 - a settings file that cannot be read is worth saying plainly
        print(f"could not read the models folder from Strata's settings: {exc}", file=sys.stderr)
        return 2
    print(f"  models folder  {data_dir}  (from {source})")
    if not Path(data_dir).is_dir():
        print(f"  {data_dir} is not there: the models tool is left out rather than pointed at nothing",
              file=sys.stderr)
    wanted = servers(Path(data_dir) if Path(data_dir).is_dir() else None)
    print(f"  tools          {', '.join(wanted)}")
    print(f"  configs        {', '.join(str(d) for d in CONFIG_DIRS)}")

    configs = list(model_configs())
    if not configs:
        print("no model configs found in " + ", ".join(str(d) for d in CONFIG_DIRS), file=sys.stderr)
        return 2

    wrong = []
    for path, data in configs:
        if data.get("mcp") == MCP and data.get("mcp_servers") == wanted:
            print(f"  ok       {path.name}")
            continue
        wrong.append(path.name)
        if args.check:
            print(f"  DIFFERS  {path.name}")
            continue
        stat = path.stat()
        data["mcp"] = MCP
        data["mcp_servers"] = wanted
        text = json.dumps(data, indent=1, ensure_ascii=False) + "\n"
        path.write_text(text, encoding="utf-8")
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        print(f"  stamped  {path.name}")

    if args.check and wrong:
        print(f"\n{len(wrong)} config(s) without the canonical tools; "
              f"run tools/sync_model_tools.py", file=sys.stderr)
        return 1
    print(f"\n{len(configs)} config(s), {len(wrong)} changed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
