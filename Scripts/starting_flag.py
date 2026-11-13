"""The starting flag: who is loading Strata right now.

Why a file, and not the port
----------------------------
The server binds 8080 only *after* the engine has loaded the model: 40 s on a warm expert
cache, minutes on a cold one. In that window a starter that checks only the port sees
"nothing running", starts a second Strata, and puts two 15 GB models on one 16 GB card.
The flag closes the window: a starter creates it before it loads anything and deletes it
when the port answers.

Protocol
--------
* A starter calls :func:`acquire` **before** it loads anything. ``False`` back means another
  one is starting, and the caller should log that and do nothing: no second model, no fight
  over the port.
* The same starter calls :func:`release` when 8080 answers, and on **every** failure path,
  so a broken config cannot leave the machine permanently "starting".
* A reader calls :func:`is_fresh` to tell "a Strata is loading" from "a Strata crashed while
  loading": a flag older than :data:`STALE_SECONDS` is debris and is ignored (and cleared by
  the next :func:`acquire`).

The file lives in ``C:\\ProgramData\\Strata`` because the two users who need it differ: the
service runs as the installing account, the sentiment worker as LocalSystem, and a per-user
``%LOCALAPPDATA%`` path is invisible to whichever one did not create it.

The module is stdlib only and is imported by :mod:`Strata_Service` (in this folder) and run
as a script by ``Strata-EnsureRunning.ps1``, so there is one implementation of the protocol
rather than two that can disagree.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Optional, Tuple

#: Where the flag lives. Overridable for a test that must not touch the live one.
FLAG_PATH = Path(os.environ.get("STRATA_STARTING_FLAG", r"C:\ProgramData\Strata\strata.starting"))

#: A flag older than this is debris from a crash. Longer than the slowest plausible load
#: (cold expert cache included), short enough that a crashed start costs one check.
STALE_SECONDS = 300.0


def _payload(owner: str) -> str:
    return json.dumps(
        {
            "pid": os.getpid(),
            "started": time.time(),
            "started_iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "owner": owner,
        },
        separators=(",", ":"),
    )


def read(path: Path = FLAG_PATH) -> Optional[dict]:
    """The flag's contents, or ``None`` when there is no readable flag."""
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        # A half-written flag is still a flag: something is (or was) starting.
        return {"pid": None, "started": None, "started_iso": "", "owner": ""}
    return data if isinstance(data, dict) else {"pid": None, "started": None, "started_iso": "", "owner": ""}


def age(path: Path = FLAG_PATH) -> Optional[float]:
    """Seconds since the flag was written, or ``None`` when there is no flag."""
    try:
        return max(0.0, time.time() - path.stat().st_mtime)
    except OSError:
        return None


def is_fresh(path: Path = FLAG_PATH, stale_seconds: float = STALE_SECONDS) -> bool:
    """Whether a starter is currently loading Strata."""
    seconds = age(path)
    return seconds is not None and seconds < stale_seconds


def describe(path: Path = FLAG_PATH) -> str:
    """One line for a log: who is starting, and how long ago."""
    data = read(path)
    if data is None:
        return "no starting flag"
    seconds = age(path)
    who = str(data.get("owner") or "another Strata")
    pid = data.get("pid")
    age_text = "just now" if seconds is None else f"{seconds:.0f} s ago"
    pid_text = f" pid {pid}" if pid else ""
    return f"{who}{pid_text}, {age_text}"


def acquire(
    owner: str,
    path: Path = FLAG_PATH,
    stale_seconds: float = STALE_SECONDS,
) -> Tuple[bool, str]:
    """Take the flag for this process; ``(False, why)`` when another starter holds it.

    ``O_EXCL`` is the whole lock: the create is atomic, so of two starters running at the
    same instant exactly one gets ``True``. A stale flag is removed and the create retried
    once, because a crashed starter must not block the machine until someone notices.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    for attempt in (1, 2):
        try:
            handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            if attempt == 2 or is_fresh(path, stale_seconds):
                return False, describe(path)
            # Debris from a crash: clear it and try once more.
            try:
                path.unlink()
            except OSError:
                return False, describe(path)
            continue
        try:
            os.write(handle, _payload(owner).encode("utf-8"))
        finally:
            os.close(handle)
        return True, f"flag taken by {owner} (pid {os.getpid()})"
    return False, describe(path)


def release(path: Path = FLAG_PATH) -> bool:
    """Delete the flag. Safe when there is none, and safe when another stale one exists."""
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


def _main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="starting_flag.py",
        description="Say who is loading Strata, so a second starter waits instead of competing.",
    )
    parser.add_argument("action", choices=("acquire", "release", "status"))
    parser.add_argument("--owner", default=f"{Path(sys.argv[0]).name} pid {os.getpid()}")
    parser.add_argument("--path", default=str(FLAG_PATH))
    args = parser.parse_args(argv)
    path = Path(args.path)

    if args.action == "acquire":
        taken, detail = acquire(args.owner, path)
        print(detail)
        return 0 if taken else 1
    if args.action == "release":
        print("released" if release(path) else "no flag to release")
        return 0
    print(describe(path))
    return 0 if is_fresh(path) else 1


if __name__ == "__main__":
    raise SystemExit(_main())
