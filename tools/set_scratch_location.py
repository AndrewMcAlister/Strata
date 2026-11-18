#!/usr/bin/env python3
r"""
Keep Strata's runtime scratch off the drive the install sits on: logs, learned profiles, slots, picture scratch.

WHY THIS EXISTS

The install folder is read most of the time. What a Strata that serves 24/7 keeps WRITING is smaller and busier:

  * the run log, `log` in each strata-<model>.json - one line per request, appended forever;
  * the learned expert profile the engine saves while it adapts (`--expert-profile` / `--expert-profile-save`), rewritten
    every few minutes;
  * `ds4-slots\\` (the DeepSeek slot state) and `ds-pictures\\` (the picture reader's working files), which Strata places
    next to the run log - so moving the log moves them too.

Left alone, all of that lands in the install folder, which is exactly what you do not want on a drive kept for something
else. This script moves this write traffic to a folder of your choosing and remembers the choice in Strata's settings
(`scratch_dir`), so it is one command now and one command again if the folder ever changes. It does not move the install
itself: the engine, the server and the venv stay where they were installed.

WHAT IT CHANGES

For every strata-<model>.json:
  * `log` -> <scratch>/<same file name>, so the log, `ds4-slots\\` and `ds-pictures\\` all move at once;
  * every `--expert-profile` and `--expert-profile-save` value -> <scratch>/expert/<same file name>, with the file
    moved there first (copied, verified by size, then the original removed - and never removed if the copy failed);
  * mtimes are preserved, because Strata_Start.bat orders its model menu by write time.

    python tools/set_scratch_location.py                  # what is written where, right now
    python tools/set_scratch_location.py E:\Strata-scratch
    python tools/set_scratch_location.py E:\Strata-scratch --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strata_control import discovery  # noqa: E402 - needs ROOT on the path first

PROFILE_FLAGS = ("--expert-profile", "--expert-profile-save")


def config_files(install: Path) -> list[Path]:
    if not install.is_dir():
        return []
    return [p for p in sorted(install.glob("strata-*.json")) if not p.name.endswith(".shared-settings.json")]


def _load(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _profile_paths(data: dict) -> list[str]:
    """Every expert-profile file a config names: the flag's value in args and in args_written, plus the save key."""
    found: list[str] = []
    for key in ("args", "args_written"):
        items = data.get(key)
        if not isinstance(items, list):
            continue
        for index, item in enumerate(items[:-1]):
            if isinstance(item, str) and item in PROFILE_FLAGS:
                value = items[index + 1]
                if isinstance(value, str) and value:
                    found.append(value)
    save = data.get("expert_profile_save")
    if isinstance(save, str) and save:
        found.append(save)
    return list(dict.fromkeys(found))


def _rewrite(data: dict, log_target: str | None, profile_map: dict[str, str]):
    """A config with its log and profile paths pointed at the new scratch folder."""
    fixed = dict(data)
    if log_target and isinstance(fixed.get("log"), str):
        fixed["log"] = log_target
    for key in ("args", "args_written"):
        items = fixed.get(key)
        if isinstance(items, list):
            fixed[key] = [profile_map.get(item, item) if isinstance(item, str) else item for item in items]
    if isinstance(fixed.get("expert_profile_save"), str):
        fixed["expert_profile_save"] = profile_map.get(fixed["expert_profile_save"], fixed["expert_profile_save"])
    return fixed


def report(install: Path) -> int:
    own = discovery._read_json(discovery.own_settings_file())
    print(f"  scratch_dir  {own.get('scratch_dir') or '(not set: each config names its own)'}")
    rows = 0
    for cfg in config_files(install):
        data = _load(cfg)
        if not data or not data.get("exe"):
            continue
        rows += 1
        log = data.get("log") or "(none)"
        profiles = _profile_paths(data) or ["(none)"]
        print(f"    {cfg.name}")
        print(f"      log      {log}")
        for profile in profiles:
            print(f"      profile  {profile}")
    if not rows:
        print(f"  no run configs in {install}")
    return 0


def set_location(folder: str, *, install: Path, dry_run: bool = False) -> int:
    target = Path(os.path.expandvars(folder.strip().strip('"'))).expanduser()
    if not target.is_absolute():
        print(f"  {target} is not a full path: give the whole path, like E:\\Strata-scratch", file=sys.stderr)
        return 2
    if not target.parent.exists():
        print(f"  {target.parent} is not there. Plug the drive in, or pick another folder.", file=sys.stderr)
        return 2
    expert = target / "expert"
    print(f"  logs   {target}")
    print(f"  expert {expert}")
    confs = [c for c in config_files(install) if (_load(c) or {}).get("exe")]
    if not confs:
        print(f"  no run configs in {install}", file=sys.stderr)
        return 2

    if dry_run:
        for cfg in confs:
            data = _load(cfg) or {}
            print(f"    would repoint {cfg.name}: log -> {target / Path(data.get('log') or 'model.log').name}")
        print("\n  dry run: nothing changed")
        return 0

    target.mkdir(parents=True, exist_ok=True)
    expert.mkdir(parents=True, exist_ok=True)

    # The profile files first: a config that names a file which is not there yet is a config that fails at load.
    moved: dict[str, str] = {}
    for cfg in confs:
        data = _load(cfg) or {}
        for raw in _profile_paths(data):
            if raw in moved:
                continue
            src = Path(raw)
            dst = expert / src.name
            if dst.exists() and dst.stat().st_size == (src.stat().st_size if src.exists() else -1):
                moved[raw] = str(dst)
                continue
            if not src.exists():
                # The old folder is not there (a drive that is unplugged, a path that was cleaned up). The config still has
                # to stop naming it: left as it is, Strata would try to write its log and its profile onto a drive that is
                # absent and fail at startup. The new path is named instead, and the engine makes the profile again.
                moved[raw] = str(dst)
                print(f"  {src} is not there; naming {dst} instead (Strata makes a new profile)")
                continue
            try:
                if src.stat().st_dev == dst.parent.stat().st_dev:
                    shutil.copy2(src, dst)
                else:
                    shutil.copy2(src, dst)
            except OSError as exc:
                print(f"  could not copy {src} to {dst}: {exc}", file=sys.stderr)
                return 1
            if dst.stat().st_size != src.stat().st_size:
                print(f"  the copy of {src.name} has the wrong size; the original was kept", file=sys.stderr)
                return 1
            os.remove(src)
            moved[raw] = str(dst)
            print(f"  moved {src}  ->  {dst}")

    changed = 0
    for cfg in confs:
        data = _load(cfg) or {}
        old_log = data.get("log")
        log_target = str(target / Path(old_log).name) if isinstance(old_log, str) and old_log else None
        fixed = _rewrite(data, log_target, moved)
        if fixed == data:
            print(f"  ok        {cfg.name}")
            continue
        stat = cfg.stat()
        cfg.write_text(json.dumps(fixed, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        os.utime(cfg, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        changed += 1
        print(f"  repointed {cfg.name}  ({old_log} -> {log_target})")

    # Remember it, so the report and the next change agree. Strata ignores the key; it is this tool's own note.
    settings_file = discovery.own_settings_file()
    current = discovery._read_json(settings_file)
    current["scratch_dir"], current["scratch_dir_at"] = str(target), time.time()
    try:
        settings_file.parent.mkdir(parents=True, exist_ok=True)
        temp = settings_file.with_name(f"{settings_file.name}.{os.getpid()}.tmp")
        temp.write_text(json.dumps(current, indent=1), encoding="utf-8")
        os.replace(temp, settings_file)
        print(f"\n  wrote  {settings_file}  (scratch_dir)")
    except OSError as exc:
        print(f"\n  could not write {settings_file}: {exc}", file=sys.stderr)

    print(f"  {changed} run config(s) repointed. Restart Strata so the new log and profile paths are used.")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Move Strata's runtime scratch (logs, profiles, slots) to another folder.")
    ap.add_argument("folder", nargs="?", help="where logs and scratch should go")
    ap.add_argument("--install-dir", default=str(ROOT), help=f"where the run configs are (default {ROOT})")
    ap.add_argument("--dry-run", action="store_true", help="say what would change, change nothing")
    args = ap.parse_args(argv)
    install = Path(args.install_dir).expanduser()
    if args.folder is None:
        return report(install)
    return set_location(args.folder, install=install, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
