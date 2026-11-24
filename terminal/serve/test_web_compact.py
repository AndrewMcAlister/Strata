"""terminal/serve/web/compact.js - the planning behind the Compact button, and the page wiring around it.

The browser part cannot run here, but the part that decides what to summarize is pure and runs under node with a
stand-in for `window` (the same approach as serve/test_speed_average.py). The wiring checks are on the text of the
files the page actually serves.

The feature: the web chat sends the whole conversation every turn, so a long chat eventually leaves no room for an
answer. compact.js replaces the older turns with a summary made by the loaded model, from the Compact button or
automatically just before a request would crowd the window.
"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

WEB = Path(__file__).resolve().parent / "web"
NODE = shutil.which("node")


def js(expression: str):
    """Run one expression against compact.js with a stand-in window, and return its JSON value."""
    source = (WEB / "compact.js").read_text(encoding="utf-8")
    script = "globalThis.window = {};\n" + source + f"\nconsole.log(JSON.stringify({expression}));"
    done = subprocess.run([NODE, "-e", script], capture_output=True, text=True, encoding="utf-8",
                          timeout=60, check=True)
    return json.loads(done.stdout)


@unittest.skipUnless(NODE, "node is needed to run the page's own code")
class Planner(unittest.TestCase):
    def test_chunks_are_cut_at_the_budget_and_keep_their_order(self):
        self.assertEqual(js("window.StrataCompact.planChunks([10, 10, 10], 25)"), [[0, 2], [2, 3]])
        self.assertEqual(js("window.StrataCompact.planChunks([10, 10, 10], 100)"), [[0, 3]])
        self.assertEqual(js("window.StrataCompact.planChunks([], 30)"), [])

    def test_one_oversized_message_is_a_chunk_of_its_own(self):
        self.assertEqual(js("window.StrataCompact.planChunks([40, 10], 30)"), [[0, 1], [1, 2]])

    def test_every_message_lands_in_exactly_one_chunk(self):
        chunks = js("window.StrataCompact.planChunks([7, 9, 2, 14, 3, 8, 11], 20)")
        seen = [i for chunk in chunks for i in range(*chunk)]
        self.assertEqual(seen, list(range(7)))
        self.assertTrue(all(sum([7, 9, 2, 14, 3, 8, 11][a:b]) <= 20 for a, b in chunks[:-1]))

    def test_the_cut_keeps_the_last_user_turn_even_over_budget(self):
        roles = '["user", "assistant", "user", "assistant"]'
        self.assertEqual(js(f"window.StrataCompact.chooseCut([10, 10, 10, 10], {roles}, 25)"), 2)
        self.assertEqual(js(f"window.StrataCompact.chooseCut([10, 10, 10, 10], {roles}, 5)"), 2,
                         "the question being asked is not summarized away to make room")
        self.assertEqual(js('window.StrataCompact.chooseCut([10, 10, 10], ["user", "assistant", "user"], 0)'), 2)

    def test_the_cut_takes_whole_turns_and_can_take_everything(self):
        roles = '["user", "assistant", "user", "assistant"]'
        self.assertEqual(js(f"window.StrataCompact.chooseCut([10, 10, 10, 10], {roles}, 100)"), 0)
        self.assertEqual(js('window.StrataCompact.chooseCut([10, 10], ["user", "assistant"], 100)'), 0,
                         "one turn has nothing older to compact")
        self.assertEqual(js('window.StrataCompact.chooseCut([10, 10, 10], ["assistant", "user", "assistant"], 100)'), 1,
                         "the tail always starts at a user message, even when that leaves one message to summarize")

    def test_a_message_estimate_is_never_zero(self):
        self.assertGreaterEqual(js("window.StrataCompact.estimateText('')"), 1)
        self.assertGreater(js("window.StrataCompact.estimateText('a'.repeat(360))"),
                           js("window.StrataCompact.estimateText('a'.repeat(36))"))

    def test_a_summary_is_cleaned_of_tag_wrappers(self):
        self.assertEqual(js("window.StrataCompact.cleanSummary('<summary>\\n## Goal\\n- done\\n</summary>')"), "## Goal\n- done")
        self.assertEqual(js("window.StrataCompact.cleanSummary('<details>text</details>')"), "text")
        self.assertEqual(js("window.StrataCompact.cleanSummary('   ')"), "")
        self.assertEqual(js("window.StrataCompact.cleanSummary('plain')"), "plain")

    def test_the_thresholds_leave_room_to_summarize(self):
        constants = js("window.StrataCompact.constants")
        self.assertLess(constants["TRIGGER"], 1)
        self.assertLess(constants["TAIL_SHARE"], constants["TRIGGER"])
        self.assertLess(constants["CHUNK_SHARE"], 1)


class PageWiring(unittest.TestCase):
    def setUp(self):
        self.html = (WEB / "index.html").read_text(encoding="utf-8")
        self.app = (WEB / "app.js").read_text(encoding="utf-8")
        self.compact = (WEB / "compact.js").read_text(encoding="utf-8")

    def test_the_page_has_the_button_and_loads_the_script_after_app(self):
        self.assertIn('id="compact-btn"', self.html)
        self.assertIn('href="web/sprite.svg#i-layers"', self.html)
        self.assertLess(self.html.index("web/app.js"), self.html.index("web/compact.js"),
                        "compact.js uses app.js's helpers, so it must load after it")

    def test_a_request_compacts_before_it_is_sent(self):
        head = self.app[self.app.index("async function generateReply("):]
        head = head[:head.index("const m = {role: \"assistant\"")]
        self.assertIn("maybeAutoCompact", head)

    def test_a_new_message_waits_for_a_running_compaction(self):
        send = self.app[self.app.index("async function send(payload) {"):]
        self.assertIn("StrataCompact.busy()", send[:400])

    def test_the_note_is_its_own_block_and_is_not_a_turn(self):
        self.assertIn('m.role === "user" && m.compacted', self.app)
        self.assertIn("st-msg--compacted", self.app)
        self.assertIn("m.summary || m.text", self.app)

    def test_summaries_are_short_requests_that_do_not_stream_or_think(self):
        self.assertIn('"v1/chat/completions"', self.compact)
        self.assertIn("stream: false", self.compact)
        self.assertIn("enable_thinking: false", self.compact)
        self.assertIn("max_tokens: maxTokens", self.compact)

    def test_nothing_is_replaced_until_the_summary_exists(self):
        run = self.compact[self.compact.index("async function compactNow("):]
        before = run.index("const before = messages.slice()")
        replace = run.index("messages = after")
        self.assertLess(before, replace)
        self.assertNotIn("messages =", run[:before], "no early mutation of the conversation")

    def test_the_server_sees_one_note_and_the_recent_turns(self):
        self.assertIn("const after = [note, ...tail]", self.compact)
        self.assertIn("count: prefix.length", self.compact)


if __name__ == "__main__":
    unittest.main()
