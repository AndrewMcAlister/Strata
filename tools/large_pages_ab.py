r"""What 2 MB pages are worth for the expert arena (Windows large pages).

The expert arena is ~40 GB of host memory that the CPU walks expert by expert and the GPU DMAs out of.
Windows can back it with 2 MB pages or with 4 KB pages, and include/strata/core/pinned.hpp says why the
engine prefers the first: "33.97 GB at 4 KB pages is 8.3 million TLB entries, which does not fit in any
TLB, so every block of every expert matvec takes TLB misses."

Large pages on Windows need SeLockMemoryPrivilege ("Lock pages in memory" - Scripts/Strata_LargePages.ps1
grants it).  With it the engine logs "large pages (2097152 B)"; without it, "large pages refused ... using
4 KB pages".  src/core/pinned.cu:87 ships STRATA_NO_LARGEPAGES=1 to skip the attempt, so one PC can measure
both halves in the same boot with nothing but that variable changed.

Decode speed only, measured the way tools/calibrate.py measures: the median over three fixed prompts at
temperature 0, and the two configurations INTERLEAVED (the adaptive expert tier and the OS make single
measurements noisy by a few percent, which is the size of the effect being looked for).  The verdict uses
calibrate.py's own 3% bar.

Each start also reads the engine's arena line back out of the log it just wrote, so a run that never got
the backing it was supposed to is reported instead of being silently averaged in.

    .venv\Scripts\python tools\large_pages_ab.py strata-iq3_xxs.json              # Windows
    .venv\Scripts\python tools\large_pages_ab.py strata-iq3_xxs.json --rounds 5
    .venv/bin/python tools/large_pages_ab.py strata-iq3_xxs.json                  # Linux

Use the .venv interpreter, not a bare `python` - the tokenizer imports `regex`, which is installed
there and usually not in the system Python.

Strata itself must be STOPPED first: this starts its own engine, and the expert arena cannot be
committed twice (40 GB each).

Every engine start reloads the model (~40 s for the 40 GB packs).  Logs go to a temporary directory that
is removed afterwards; nothing in the Strata folder is written.
"""
from __future__ import annotations

import argparse
import json
import shutil
import statistics
import sys
import tempfile
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT))

from calibrate import PROMPTS, Session, chat_ids, close   # noqa: E402

MIN_GAIN = 0.03                     # calibrate.py's bar: only a bigger win than this is worth keeping
ARENA_MARK = "expert arena:"
MARKER = "STRATA_NO_LARGEPAGES"
LARGE = "large pages"
NORMAL = "4 KB pages"


def load_ids(cfg: dict):
    """The fixed prompt set calibrate.py uses, tokenized through the config's own tokenizer."""
    import strata_tokenizer as ST

    tpath = Path(cfg["tokenizer"])
    vocab = json.loads((tpath / "vocab.json").read_text(encoding="utf-8"))
    toks = [None] * len(vocab)
    for t, i in vocab.items():
        toks[i] = t
    tok = ST.Tokenizer(toks, (tpath / "merges.txt").read_text(encoding="utf-8").split("\n"),
                       json.loads((tpath / "token_type.json").read_text()))
    return [chat_ids(tok, p) for p in PROMPTS]


def arena_note(log_path: Path) -> str | None:
    """The expert-arena line from a log, or None.  Last one wins: the log is appended to."""
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    found = None
    for line in text.splitlines():
        if ARENA_MARK in line:
            found = line.split(ARENA_MARK, 1)[1].strip()
    return found


def backing_of(note: str | None) -> str:
    """'large', '4k', or 'unknown' - what the engine says it actually got."""
    if not note:
        return "unknown"
    if f"{LARGE} (" in note:          # "large pages (2097152 B)" - the success spelling
        return "large"
    if NORMAL in note:               # "large pages refused ...; using 4 KB pages"
        return "4k"
    return "unknown"


def start(cfg: dict, log: Path):
    from serve.server import StrataEngine, child_env

    return StrataEngine(cfg["exe"], cfg["args"], cwd=cfg.get("cwd"), log=str(log), env=child_env(cfg))


def variants(cfg: dict) -> dict:
    """The two configs: the first exactly as it runs today, the second with the large-page attempt off.

    The marker must be ABSENT for the large-page run: src/core/pinned.cu tests getenv(...) == nullptr, so
    any value at all - "0" included - skips the attempt.
    """
    base_env = {k: v for k, v in (cfg.get("env") or {}).items() if k != MARKER}
    large = deepcopy(cfg)
    large["env"] = base_env
    off = deepcopy(cfg)
    off["env"] = dict(base_env, **{MARKER: "1"})
    return {LARGE: large, NORMAL: off}


def main() -> int:
    ap = argparse.ArgumentParser(description="A/B the expert arena's page backing on decode speed.")
    ap.add_argument("config", help="a strata-*.json, as Strata itself uses it")
    ap.add_argument("--rounds", type=int, default=3, help="interleaved A/B pairs (default 3)")
    opts = ap.parse_args()

    cfg = json.loads(Path(opts.config).read_text(encoding="utf-8-sig"))
    ids = load_ids(cfg)
    runs = variants(cfg)

    rates: dict[str, list[float]] = {k: [] for k in runs}
    notes: dict[str, str | None] = {k: None for k in runs}
    prefill: dict[str, list[float]] = {k: [] for k in runs}
    want = {LARGE: "large", NORMAL: "4k"}
    dropped: dict[str, int] = {k: 0 for k in runs}

    tmp = Path(tempfile.mkdtemp(prefix="strata-largepages-"))
    try:
        for r in range(opts.rounds):
            for name, c in runs.items():
                log = tmp / f"{name.replace(' ', '-')}-{r}.log"
                c["log"] = str(log)
                eng = start(c, log)
                try:
                    # The arena line is written while the model loads, so the backing is known
                    # BEFORE the ~30 s of measurement.  A run that did not get what it asked for is
                    # dropped here rather than measured: on this PC a 40 GB large-page block is not
                    # always available, and its number is a 4 KB number wearing the wrong label.
                    note = arena_note(log)
                    got = backing_of(note)
                    if got != want[name]:
                        dropped[name] += 1
                        print(f"  round {r + 1}/{opts.rounds}  {name:<11} DROPPED - asked for"
                              f" {want[name]}, got {got}")
                        continue
                    s = Session(eng, ids)
                    s.warm_up(1)
                    rates[name].append(s.rate())
                finally:
                    close(eng)
                notes[name] = note or notes[name]
                pp = getattr(eng, "prefill_tok_s_mean", None)
                if pp:
                    prefill[name].append(pp)
                print(f"  round {r + 1}/{opts.rounds}  {name:<11} {rates[name][-1]:6.1f} tok/s"
                      f"   [{got}]")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    for name in runs:
        note = notes[name]
        print(f"  {name:<11} arena: {note or '(no arena line found)'}")
    print()

    # Two clean samples each is the minimum a median means anything over; below that the honest
    # answer is that the run did not happen, not a number.
    if len(rates[LARGE]) < 2 or len(rates[NORMAL]) < 2:
        for name in runs:
            if dropped[name]:
                print(f"  {name}: {dropped[name]} round(s) fell back to "
                      f"{'4 KB' if name == LARGE else 'large'} pages and were dropped.")
        print("  REFUSED: fewer than two clean rounds per configuration, so there is nothing to")
        print("  compare.  A large-page run falls back for one of two reasons: the privilege is not")
        print(f"  in this logon's token (Scripts/Strata_LargePages.ps1 /status says), or the pool could")
        print("  not give 40 GB at once (VirtualAlloc 1450) - reboot with memory-heavy programs closed.")
        return 1

    for name in runs:
        if dropped[name]:
            print(f"  note: {dropped[name]} {name} round(s) fell back and were dropped, so the")
            print(f"        median below uses {len(rates[name])} round(s), not {opts.rounds}.")
    if any(dropped.values()):
        print()

    med = {k: statistics.median(v) for k, v in rates.items() if v}
    lo, hi = sorted(med, key=med.get)          # lo = the slower median
    gain = med[hi] / med[lo] - 1.0
    print(f"  large pages {med[LARGE]:6.1f} tok/s   4 KB pages {med[NORMAL]:6.1f} tok/s")
    if prefill.get(LARGE) and prefill.get(NORMAL):
        print(f"  prompt read {statistics.median(prefill[LARGE]):6.1f} tok/s vs"
              f" {statistics.median(prefill[NORMAL]):6.1f} tok/s")
    spread = {k: (max(v) - min(v)) / statistics.median(v) for k, v in rates.items() if v}
    print(f"  spread within each configuration: "
          f"{', '.join(f'{k} {100 * s:.1f}%' for k, s in spread.items())}")
    print()

    # The spread is checked FIRST, on purpose.  A configuration that moves several percent between
    # its own runs cannot establish a difference of a few percent between configurations, so the
    # direction of the medians is not evidence and must not be reported as if it were.
    worst_spread = max(spread.values()) if spread else 0.0
    if worst_spread >= max(abs(gain), MIN_GAIN):
        print(f"  INCONCLUSIVE: {100 * abs(gain):.1f}% between the two medians, but one configuration's")
        print(f"  own runs spread {100 * worst_spread:.1f}%.  The noise is as large as the effect, so this")
        print("  run cannot separate them - whichever way the medians point, it is not a measurement.")
        print(f"  More rounds (--rounds {max(2, opts.rounds * 3)}) is the only fix.")
        return 0
    if med[LARGE] > med[NORMAL] * (1.0 + MIN_GAIN):
        print(f"  KEEP: large pages are {100 * gain:.1f}% faster on decode, above the {100 * MIN_GAIN:.0f}%")
        print("  bar tools/calibrate.py uses.")
        return 0
    if med[NORMAL] > med[LARGE] * (1.0 + MIN_GAIN):
        print(f"  4 KB pages are {100 * gain:.1f}% faster, above the {100 * MIN_GAIN:.0f}% bar, on a run")
        print(f"  that held together ({100 * worst_spread:.1f}% spread).  Consider dropping the privilege.")
        return 0
    print(f"  WITHIN NOISE: {100 * abs(gain):.1f}%, under the {100 * MIN_GAIN:.0f}% bar, with a spread of")
    print(f"  {100 * worst_spread:.1f}%.  Large pages are not worth claiming as a speed win on this PC on")
    print("  this evidence.  The honest answer is that the effect is too small to see here.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
