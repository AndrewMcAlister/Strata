"""Tests for Scripts/Strata_Stop.bat writing the PowerShell it runs.

The probe this script starts is batch `echo` lines that become a .ps1 at run time, so cmd.exe parses every line before
PowerShell ever sees it. A PowerShell pipeline in such a line (`@($cfgd | ForEach-Object { ... })`) is a cmd pipe unless
it is written `^|`: cmd then runs `ForEach-Object` itself, which prints
`'ForEach-Object' is not recognized as an internal or external command` in the console of whoever clicked the shortcut,
and the line never lands in the generated script (seen on this PC, 25 November 2026: StrataStack.ps1 -Action Start
stopped the running model first, then printed that error before the model menu).

A line that is a real cmd pipeline (`echo %GRACE%|findstr ...`) has no "%PSFILE%" in it and must stay as it is, so the
check looks only at the writer lines.  Nothing here needs Windows, a GPU, a model or a download.

    python -m unittest tools.test_stop_script
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "Scripts" / "Strata_Stop.bat"
LINES = SCRIPT.read_text(encoding="utf-8").splitlines()

WRITER = re.compile(r'^(>>?)\"%PSFILE%\"\s+echo\s?(.*)$', re.I)


def writers() -> list[tuple[int, str]]:
    """(line number, the PowerShell text after `echo`) of every line that writes the generated script."""
    return [(n, m.group(2)) for n, line in enumerate(LINES, 1) if (m := WRITER.match(line.strip()))]


def unescaped_pipes(text: str) -> int:
    """Pipes cmd would treat as a pipeline: a `|` that no `^` stands in front of."""
    return len(re.findall(r"(?<!\^)\|", text))


class StrataStop(unittest.TestCase):
    def test_the_script_writes_its_probe(self):
        self.assertTrue(SCRIPT.is_file(), SCRIPT)
        self.assertGreater(len(writers()), 100, "the probe is written line by line")

    def test_no_writer_line_hands_a_pipeline_to_cmd(self):
        """The bug of 25 November: `@($cfgd | ForEach-Object {...})` ran in cmd instead of being written."""
        broken = [(n, text) for n, text in writers() if unescaped_pipes(text)]
        self.assertEqual(broken, [], "escape the pipe as ^| so cmd writes it into the script")

    def test_the_two_folder_fallback_is_escaped(self):
        """Configs live in Strata's folder and in its terminal\\ folder, so the fallback lists both."""
        found = [(n, text) for n, text in writers() if "$cfgd" in text and "ForEach-Object" in text]
        self.assertEqual(len(found), 1, "one line builds the list of folders to search")
        self.assertIn("^| ForEach-Object", found[0][1])

    def test_a_real_cmd_pipeline_is_left_alone(self):
        """`echo %GRACE%|findstr` is cmd's own pipeline, not a writer line: it must not be escaped."""
        grace = [line for line in LINES if "%GRACE%" in line and "findstr" in line]
        self.assertTrue(grace, "the /timeout number is still checked")
        self.assertIn("|findstr", grace[0])


if __name__ == "__main__":
    unittest.main()
