"""Tests for tools/large_pages_ab.py, without a GPU: a stand-in engine whose speed and whose reported
arena backing are both chosen by the test.

The point of these is the guard rails.  An A/B of page backing is worthless if the run that was supposed
to get large pages silently fell back to 4 KB - both halves would then be the same configuration - so the
script refuses rather than averaging, and that refusal is what most of this file pins down.

    python -m unittest tools.test_large_pages_ab
"""
from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT))
import large_pages_ab as AB  # noqa: E402

REFUSED_LINE = ("cudaHostRegister PORTABLE ok; large pages refused for 42916118528 B "
                "(GetLargePageMinimum=2097152, VirtualAlloc error 1314); using 4 KB pages")
LARGE_LINE = "cudaHostRegister PORTABLE ok; large pages (2097152 B)"


class FakeEngine:
    def __init__(self, rate: float):
        self.rate = rate
        self.last = {}
        self.prefill_tok_s_mean = rate * 20.0
        self.proc = None                      # calibrate.close() returns at once

    def generate(self, ids, max_new, sampling, cancel):
        for _ in range(max_new):
            yield 1
        self.last = {"generated": max_new, "decode_ms": max_new / self.rate * 1000.0}


def run_main(cfg: dict, rate_for, backing_for, rounds: int = 2):
    """Drive main() against a stand-in engine.  Both callbacks take (variant, round_index), where
    round_index counts that variant's own starts, so a test can make one round misbehave.
    `backing_for` is the arena line to write into the log."""
    calls: dict[str, int] = {}

    def fake_start(c, log):
        env = c.get("env") or {}
        variant = "4k" if AB.MARKER in env else "large"
        r = calls.get(variant, 0)
        calls[variant] = r + 1
        Path(log).write_text(f"strata generate: expert arena: {backing_for(variant, r)}\n",
                             encoding="utf-8")
        return FakeEngine(rate_for(variant, r))

    old_start, old_ids, old_argv = AB.start, AB.load_ids, sys.argv
    AB.start = fake_start
    AB.load_ids = lambda c: [[1, 2, 3]] * 3
    try:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "strata-test.json"
            cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
            sys.argv = ["large_pages_ab.py", str(cfg_path), "--rounds", str(rounds)]
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = AB.main()
            return code, buf.getvalue()
    finally:
        AB.start, AB.load_ids, sys.argv = old_start, old_ids, old_argv


CFG = {"exe": "x", "args": ["--pack", "p"], "tokenizer": "t", "model_name": "m"}


class Helpers(unittest.TestCase):
    def test_backing_of(self):
        self.assertEqual(AB.backing_of(REFUSED_LINE), "4k")
        self.assertEqual(AB.backing_of(LARGE_LINE), "large")
        self.assertEqual(AB.backing_of(None), "unknown")
        self.assertEqual(AB.backing_of("something else entirely"), "unknown")

    def test_arena_note_takes_the_last_line(self):
        # the log is appended to across starts, so the newest statement is the one that counts
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "x.log"
            p.write_text(f"strata generate: expert arena: {REFUSED_LINE}\n"
                         f"strata generate: expert arena: {LARGE_LINE}\n", encoding="utf-8")
            self.assertEqual(AB.backing_of(AB.arena_note(p)), "large")

    def test_arena_note_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(AB.arena_note(Path(tmp) / "nope.log"))

    def test_variants(self):
        v = AB.variants(CFG)
        self.assertEqual(v[AB.LARGE]["env"], {})                       # absent, not present-and-falsey
        self.assertNotIn(AB.MARKER, v[AB.LARGE]["env"])
        self.assertEqual(v[AB.NORMAL]["env"], {AB.MARKER: "1"})
        self.assertEqual(v[AB.LARGE]["args"], CFG["args"])              # nothing else is disturbed
        self.assertEqual(v[AB.NORMAL]["args"], CFG["args"])

    def test_variants_keep_other_env(self):
        v = AB.variants(dict(CFG, env={"OTHER": "keep", AB.MARKER: "1"}))
        self.assertEqual(v[AB.LARGE]["env"], {"OTHER": "keep"})         # the marker is stripped for run A
        self.assertEqual(v[AB.NORMAL]["env"], {"OTHER": "keep", AB.MARKER: "1"})

    def test_variants_do_not_share_state(self):
        v = AB.variants(CFG)
        v[AB.LARGE]["env"]["mutated"] = "1"
        self.assertNotIn("mutated", v[AB.NORMAL]["env"])
        self.assertNotIn("mutated", CFG.get("env") or {})


class Verdicts(unittest.TestCase):
    def test_keep_when_large_pages_win(self):
        code, out = run_main(CFG, lambda v, r: 60.0 if v == "large" else 55.0,
                             lambda v, r: LARGE_LINE if v == "large" else REFUSED_LINE)
        self.assertEqual(code, 0)
        self.assertIn("KEEP", out)
        self.assertIn("9.1%", out)                                     # 60 / 55 - 1

    def test_noise(self):
        code, out = run_main(CFG, lambda v, r: 55.0,
                             lambda v, r: LARGE_LINE if v == "large" else REFUSED_LINE)
        self.assertEqual(code, 0)
        self.assertIn("WITHIN NOISE", out)

    def test_small_gain_is_still_noise(self):
        # 2% is under calibrate.py's 3% bar
        code, out = run_main(CFG, lambda v, r: 51.0 if v == "large" else 50.0,
                             lambda v, r: LARGE_LINE if v == "large" else REFUSED_LINE)
        self.assertEqual(code, 0)
        self.assertIn("WITHIN NOISE", out)

    def test_a_noisy_run_cannot_claim_a_win_either_way(self):
        # the real result from this PC: 4 KB medians 4.8% ahead, but one configuration's own runs
        # spread 8%, so the direction is not evidence
        big = {0: 59.3, 1: 61.2, 2: 62.7}
        small = {0: 59.2, 1: 64.6, 2: 64.1}
        code, out = run_main(CFG,
                             lambda v, r: (big if v == "large" else small)[r],
                             lambda v, r: LARGE_LINE if v == "large" else REFUSED_LINE,
                             rounds=3)
        self.assertEqual(code, 0)
        self.assertIn("INCONCLUSIVE", out)
        self.assertNotIn("KEEP", out)
        self.assertNotIn("Consider dropping", out)

    def test_drops_a_round_that_fell_back(self):
        # round 2's large-page start silently fell back to 4 KB.  Its number is a 4 KB number and
        # must not be averaged into the large-page median.
        def backing(v, r):
            if v == "large":
                return REFUSED_LINE if r == 1 else LARGE_LINE
            return REFUSED_LINE

        code, out = run_main(CFG, lambda v, r: 55.0, backing, rounds=3)
        self.assertEqual(code, 0)
        self.assertIn("DROPPED", out)
        self.assertIn("2 round(s), not 3", out)

    def test_refuses_when_the_large_run_did_not_get_large_pages(self):
        # the privilege is not in the logon token yet: the experiment never happened
        code, out = run_main(CFG, lambda v, r: 60.0, lambda v, r: REFUSED_LINE)
        self.assertEqual(code, 1)
        self.assertIn("REFUSED", out)

    def test_refuses_when_the_control_was_not_4k(self):
        # STRATA_NO_LARGEPAGES never reached the engine: both halves would be the same configuration
        code, out = run_main(CFG, lambda v, r: 60.0, lambda v, r: LARGE_LINE)
        self.assertEqual(code, 1)
        self.assertIn("REFUSED", out)

    def test_refuses_when_too_few_clean_rounds_survive(self):
        # 1 clean large round out of 3 is not a median
        def backing(v, r):
            if v == "large":
                return LARGE_LINE if r == 0 else REFUSED_LINE
            return REFUSED_LINE

        code, out = run_main(CFG, lambda v, r: 55.0, backing, rounds=3)
        self.assertEqual(code, 1)
        self.assertIn("REFUSED", out)

    def test_interleaves_the_two_configurations(self):
        order = []

        def rate(v, r):
            order.append(v)
            return 55.0

        run_main(CFG, rate, lambda v, r: LARGE_LINE if v == "large" else REFUSED_LINE, rounds=3)
        self.assertEqual(order, ["large", "4k"] * 3)


if __name__ == "__main__":
    unittest.main()
