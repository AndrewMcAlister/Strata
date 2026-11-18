#!/usr/bin/env python3
r"""
Point Strata's model files at a folder: the one command that changes where they live.

WHAT THIS IS

Strata already keeps this as a setting - the data folder, `data_dir` in its settings file, which the desktop app offers as
Settings > Storage and `strata launch --data-dir` does for one launch. Two settings files can name it (the Terminal's and
the app's `app-settings.json`) and Strata itself lets the one changed last win, copying it to the other. This script is the
command-line way to make that change deliberately, from one place, and to carry the run configs with it.

Why the configs need carrying: every `strata-<model>.json` names its model files by absolute path (the pack, the GGUF
shards, the draft layer, the tokenizer). Changing the setting alone leaves those configs pointing at the old folder, so
the next launch either fails or serves the old copy. That is the whole job here besides the setting itself.

WHAT IT CHANGES, and what it does not

* It writes the folder and its time into the Terminal's settings file, then lets Strata's own sync copy it to the app's
  settings file, so both agree and the next launch from either picks it up.
* It rewrites a run config's path only when the file exists in the new folder. A path whose file is missing on the new
  side is left alone and reported, because a config half-rewritten to a folder that does not hold the files is worse than
  one still naming the old folder: it fails at model load, minutes in, instead of here.
* mtimes are preserved. Strata_Start.bat orders its model menu by write time, and changing a setting should not reshuffle
  the menu.
* It does not move a single byte. Moving is `strata models move` (same drive or when there is room for a second copy) or
  tools/../migrate_data_folder.py (another drive, adapting to copies that are already there). Change the location when the
  files are already where you want them.

    python tools/set_models_location.py                       # where is it, and what points at it?
    python tools/set_models_location.py E:\Strata-data        # point Strata there
    python tools/set_models_location.py E:\Strata-data --dry-run
    python tools/set_models_location.py --check               # report only, change nothing
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strata_control import discovery, folders  # noqa: E402 - needs ROOT on the path first

PARTS = ("models", "packs", "mtp", "dflash2", "vision")


def _norm(path: str | os.PathLike) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def _under(path: str, root: str) -> bool:
    p, r = _norm(path), _norm(root).rstrip("\\/")
    return p == r or p.startswith(r + "\\") or p.startswith(r + "/")


def config_files(install: Path) -> list[Path]:
    """The run configs of an install folder, in the order Strata's menu reads them."""
    if not install.is_dir():
        return []
    return [p for p in sorted(install.glob("strata-*.json")) if not p.name.endswith(".shared-settings.json")]


def _strings(value):
    """Every string in a config, with its key path, so a report can name what changed."""
    if isinstance(value, list):
        for index, item in enumerate(value):
            yield from _strings(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _strings(item)
    elif isinstance(value, str):
        yield value


def _replace_strings(value, old: str, new: str):
    """Rewrite a config value from under `old` to under `new`, where the new file exists."""
    if isinstance(value, list):
        return [_replace_strings(v, old, new) for v in value]
    if isinstance(value, dict):
        return {k: _replace_strings(v, old, new) for k, v in value.items()}
    if isinstance(value, str) and _under(value, old):
        candidate = new + value[len(old.rstrip("\\/")):]
        if Path(candidate).exists():
            return candidate
    return value


def _count_strings(value, root: str) -> int:
    return sum(1 for s in _strings(value) if _under(s, root))


def _missing(value, root: str) -> list[str]:
    """Paths a config names under `root` that are not there: said before they become a failure at load."""
    out = []
    for s in _strings(value):
        if _under(s, root) and not Path(s).exists():
            out.append(s)
    return out


def report(install: Path) -> int:
    """Say where the setting points, from which file, and what the configs use."""
    own_file = discovery.own_settings_file()
    app_file = discovery.app_settings_file()
    print(f"  Terminal settings  {own_file}")
    print(f"  App settings       {app_file}")
    print(f"  Install folder     {install}")

    own, app = discovery._data_dir_entry(own_file), discovery._data_dir_entry(app_file)
    for label, entry in (("Terminal", own), ("app", app)):
        print(f"  {label:<7}data_dir     {entry[0] if entry else '(not set)'}"
              + (f"   changed {time.strftime('%Y-%m-%d %H:%M', time.localtime(entry[1]))}" if entry else ""))
    chosen = discovery.sync_data_dir()
    print(f"  Strata uses        {chosen[0] if chosen else '(the default)'}"
          + (f"   (from {chosen[1]})" if chosen else ""))

    configs = config_files(install)
    if not configs:
        print(f"\n  no run configs in {install}")
        return 0
    print(f"\n  {len(configs)} run config(s):")
    for cfg in configs:
        try:
            data = json.loads(cfg.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            print(f"    {cfg.name:<48} unreadable: {exc}")
            continue
        folder = next((s for s in _strings(data) if "\\models\\" in s.lower() or "/models/" in s.lower()), "")
        parts = (folder.split("\\models\\")[0].split("/models/")[0]) if folder else ""
        missing = _missing(data, parts) if parts else []
        note = f"  {len(missing)} path(s) missing" if missing else ""
        print(f"    {cfg.name:<48} {parts or '(no model path)'}{note}")
    return 0


def set_location(target: str, *, install: Path, dry_run: bool = False, configs: bool = True) -> int:
    folder = Path(os.path.expandvars(target.strip().strip('"'))).expanduser()
    if not folder.is_absolute():
        print(f"  {folder} is not a full path: give the whole path, like E:\\Strata-data", file=sys.stderr)
        return 2
    bad = folders.problem(folder)
    if bad:
        print("  " + folders.words(bad), file=sys.stderr)
        return 2
    if not folder.is_dir():
        print(f"  {folder} does not exist. Make it, or pick the folder that holds the model files.", file=sys.stderr)
        return 2

    before = discovery.sync_data_dir()
    old = str(before[0]) if before else None
    now = time.time()
    own_file = discovery.own_settings_file()

    print(f"  from   {old or '(not set)'}")
    print(f"  to     {folder}")
    for part in PARTS:
        place = folder / part
        if place.is_dir():
            files = sum(1 for _ in place.rglob("*") if _.is_file())
            size = sum(p.stat().st_size for p in place.rglob("*") if p.is_file())
            print(f"         {part:<9} {files:>4} file(s)  {size / 1e9:>9,.1f} GB")
    if not (folder / "models").is_dir() and not (folder / "packs").is_dir():
        print("         (no models/ or packs/ here yet: Strata will download into it)")

    if dry_run:
        print("\n  dry run: nothing changed")
        return 0

    # The setting. data_dir_at is what makes this change win the sync against the other settings file: without it the
    # app's older folder can win and this one is quietly undone on the next launch.
    current = discovery._read_json(own_file)
    current["data_dir"], current["data_dir_at"] = str(folder), now
    own_file.parent.mkdir(parents=True, exist_ok=True)
    temp = own_file.with_name(f"{own_file.name}.{os.getpid()}.tmp")
    temp.write_text(json.dumps(current, indent=1), encoding="utf-8")
    os.replace(temp, own_file)
    print(f"\n  wrote  {own_file}")
    synced = discovery.sync_data_dir()
    if synced:
        print(f"  synced {discovery.app_settings_file()} -> {synced[0]}")

    changed = 0
    if configs and old:
        for cfg in config_files(install):
            try:
                data = json.loads(cfg.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError) as exc:
                print(f"  skipped {cfg.name}: {exc}")
                continue
            fixed = _replace_strings(data, old, str(folder))
            if fixed == data:
                continue
            changed += 1
            stat = cfg.stat()
            cfg.write_text(json.dumps(fixed, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
            os.utime(cfg, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            left = _missing(fixed, str(folder))
            print(f"  repointed {cfg.name}" + (f"  ({len(left)} path(s) still missing)" if left else ""))
            for path in left:
                print(f"            missing: {path}")

    print(f"\n  {changed} run config(s) repointed. Restart Strata for the serving model to read the new folder.")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Point Strata's model files at a folder (the data folder setting).")
    ap.add_argument("folder", nargs="?", help="the data folder to use (models, packs, mtp)")
    ap.add_argument("--install-dir", default=str(ROOT), help=f"where the run configs are (default {ROOT})")
    ap.add_argument("--check", action="store_true", help="report only, change nothing")
    ap.add_argument("--dry-run", action="store_true", help="say what would change, change nothing")
    ap.add_argument("--no-configs", action="store_true", help="change the setting only, leave the run configs alone")
    args = ap.parse_args(argv)

    install = Path(args.install_dir).expanduser()
    if args.folder is None or args.check:
        return report(install)
    return set_location(args.folder, install=install, dry_run=args.dry_run, configs=not args.no_configs)


if __name__ == "__main__":
    raise SystemExit(main())
