"""Tests for Scripts/Strata_Start.bat asking which model to start when more than one is installed.

The launcher is batch, and its one piece of real logic (which config, and whether to ask) is a
PowerShell probe it writes at run time, so there is nothing importable to call.  These tests read
the script's text instead - the same thing test_setup_strix_halo.py does for build_windows.bat -
and check the things whose getting wrong is silent: a picker that is reached before the config is
appended, a menu printed with percent expansion (a model name holding & becomes a command), a
cancelled `choice` that answers 0, and the gates that keep a scheduled task or a
single-model install from ever being asked anything.

Verified by hand beyond these, on Windows, three configs installed: the menu appears and picks;
Enter and an ended input both take the newest; /newest, a --config of your own, --engine mock and
a non-console input all start without asking; a wrong number is asked for again; and one installed
model asks nothing.  Nothing here needs a GPU, a model or a download.

    python -m unittest tools.test_start_script
"""
from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "Scripts" / "Strata_Start.bat"
LINES = SCRIPT.read_text(encoding="utf-8").splitlines()


def line_of(needle: str) -> int:
    """The first line holding `needle`; -1 when no line does."""
    return next((i for i, line in enumerate(LINES) if needle in line), -1)


class StrataStart(unittest.TestCase):
    def test_the_script_is_there(self):
        self.assertTrue(SCRIPT.is_file(), SCRIPT)
        self.assertGreater(line_of("@echo off"), -1)

    def test_the_probe_collects_every_config_and_says_when_it_guessed(self):
        """AUTO distinguishes this script's own newest-config fallback from a config that was asked for -
        it is the one gate that lets --config, a running server and /restart all skip the question."""
        self.assertIn('$auto = 0', "\n".join(LINES))
        self.assertGreater(line_of("if ($newest -ne $null) { $cfg = $newest.FullName; $auto = 1 }"), -1)
        self.assertGreater(line_of("[void]$configs.Add($f)"), -1)          # the whole list, not just the newest

    def test_the_probe_emits_what_the_picker_needs(self):
        for key in ("TTY", "AUTO", "WANTPORT"):
            self.assertGreater(line_of(f"Write-Output ('{key}='"), -1, key)
        self.assertGreater(line_of("Write-Output ('NCFG=' + $ordered.Count)"), -1)
        for key in ("CFG", "MODEL", "EXE", "PORT"):
            self.assertGreater(line_of(f"Write-Output ('{key}' + $k + '='"), -1, key)

    def test_the_read_back_defaults_the_new_variables(self):
        """An undefined NCFG makes `for /l ... (1,1,%NCFG%)` a syntax error, so the defaults are not tidiness."""
        for var in ("AUTO", "NCFG", "TTY", "WANTPORT"):
            self.assertGreater(line_of(f'if not defined {var} set "{var}='), -1, var)

    def test_the_picker_waits_for_all_four_answers(self):
        for gate in ('if not "%AUTO%"=="1" goto banner',
                     'if not %NCFG% gtr 1 goto banner',
                     'if not "%TTY%"=="1" goto banner',
                     'if not "%USERTEST%"=="%EXTRA%" goto banner',
                     'if defined NEWEST goto banner'):
            self.assertGreater(line_of(gate), -1, gate)

    def test_nothing_asks_before_the_window_handoff(self):
        """/window re-runs this script in a new console, so a prompt here would be asked in the parent
        and thrown away; the child is the one with a console to ask in."""
        self.assertLess(line_of("if defined WINDOW goto window"), line_of(":pickprompt"))

    def test_the_config_is_appended_after_the_choice(self):
        """The command line is built from %CFG%; a line above the picker would carry the newest config
        whatever was then picked, which is the failure this whole feature is about."""
        enginearg = line_of('if not "%CFG%"=="" if "%USERTEST%"=="%EXTRA%" set "ENGINEARG=--engine strata')
        self.assertGreater(enginearg, line_of(":picked"))
        self.assertGreater(enginearg, line_of("set \"CFG=!CFG%SEL%!\""))

    def test_the_menu_prints_names_through_delayed_expansion(self):
        """%MODEL1% would be expanded before cmd looks for &, | or >, so a name holding one would be read
        as syntax rather than printed."""
        self.assertGreater(line_of(r"echo    %%i. !MODEL%%i!"), -1)
        self.assertEqual(line_of("echo    %%i. %MODEL%%i%"), -1)

    def test_a_cancelled_choice_takes_the_default(self):
        """`choice` returns 0 on Ctrl+C and on an ended input; the guard turns that into the default
        instead of an invalid answer, and an undefined SEL still falls back to DEF."""
        fallback = line_of('if not defined SEL set "SEL=!DEF!"')
        cancelled = line_of('if "!SEL!"=="0" set "SEL=!DEF!"')
        self.assertGreater(fallback, -1)
        self.assertGreater(cancelled, fallback)
        self.assertLess(cancelled, line_of('for /l %%i in (1,1,%NCFG%) do if "!SEL!"=="%%i" set "OK=1"'))

    def test_a_wrong_answer_is_asked_for_again(self):
        self.assertGreater(line_of('echo  Enter a number from 1 to %NCFG%.'), -1)

    def test_the_choice_selects_the_config_the_name_and_the_engine(self):
        for var in ("CFG", "MODEL", "EXE"):
            self.assertGreater(line_of(f'set "{var}=!{var}%SEL%!"'), -1, var)
        self.assertGreater(line_of('if "%WANTPORT%"=="0" set "PORT=!PORT%SEL%!"'), -1)

    def test_newest_is_parsed_documented_and_handed_to_the_window(self):
        self.assertGreater(line_of('if /i "%~1"=="/newest"'), -1)
        self.assertIn("/newest", "\n".join(LINES[:40]))                   # the header comment
        self.assertGreater(line_of("echo    Strata_Start.bat /newest"), -1)
        self.assertGreater(line_of('if defined NEWEST set "CHILDARGS=!CHILDARGS! /newest"'), -1)


if __name__ == "__main__":
    unittest.main()
