"""packaging/uninstall.sh and packaging/uninstall.ps1: syntax, the guards that keep a model folder and another program's
`strata` command safe, the PATH edit as a function of its own, and real runs against a fake install (PowerShell on Windows,
POSIX sh elsewhere). Nothing here touches the real user PATH: the runs pass -NoPath / STRATA_NO_PATH=1."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SH = ROOT / "packaging" / "uninstall.sh"
PS1 = ROOT / "packaging" / "uninstall.ps1"
PS = shutil.which("powershell") or shutil.which("pwsh")
RELEASE = "https://github.com/Niko1221/Strata/releases/latest/download"


def sh_binary():
    return shutil.which("sh") or shutil.which("bash")


class Scripts(unittest.TestCase):
    def test_uninstall_sh_syntax_and_guards(self):
        text = SH.read_text(encoding="utf-8")
        self.assertNotIn(b"\r", SH.read_bytes())                   # *.sh is LF in .gitattributes: a POSIX shell reads it as is
        self.assertIn("launch --stop", text)                       # a running server is stopped first
        self.assertIn("--purge", text)
        self.assertIn("Model folders are not touched", text)
        self.assertIn(RELEASE + "/install.sh", text)               # the way back is printed
        self.assertIsNone(re.search(r'rm -rf\s+"?\$DATA_DIR"?(\s|;|$)', text))     # never the whole data folder
        for line in text.splitlines():
            if "rm -rf" in line:
                self.assertNotIn("models", line.lower())
        sh = sh_binary()
        if not sh:
            self.skipTest("no sh")
        r = subprocess.run([sh, "-n", str(SH)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_uninstall_ps1_parses_and_guards(self):
        text = PS1.read_text(encoding="utf-8")
        self.assertIn("launch --stop", text)
        self.assertIn("[switch]$Purge", text)
        self.assertIn("Model folders are not touched", text)
        self.assertIn(RELEASE + "/install.ps1", text)
        self.assertIsNone(re.search(r"Remove-Item[^\n]*\$data[^\n]*-Recurse", text))
        for line in text.splitlines():
            if "Remove-Item" in line or "rmdir" in line:
                self.assertNotIn("models", line.lower())
        if not PS:
            self.skipTest("no PowerShell")
        cmd = ("$e=$null; $t=$null; [System.Management.Automation.Language.Parser]::ParseFile('%s',[ref]$t,[ref]$e) | Out-Null; "
               "if ($e.Count) { $e | ForEach-Object { $_.Message }; exit 1 } else { 'ok' }" % str(PS1).replace("'", "''"))
        r = subprocess.run([PS, "-NoProfile", "-Command", cmd], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


class Documented(unittest.TestCase):
    def test_install_doc_and_the_release_notes_name_the_uninstaller(self):
        install = (ROOT / "docs" / "INSTALL.md").read_text(encoding="utf-8")
        self.assertIn("uninstall.sh", install)
        self.assertIn("uninstall.ps1", install)
        updates = (ROOT / "packaging" / "UPDATES.md").read_text(encoding="utf-8")
        self.assertIn("uninstall.sh", updates)
        self.assertIn("uninstall.ps1", updates)


@unittest.skipIf(os.name == "nt", "POSIX only")
class PosixUninstall(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        self.data = base / "data"
        self.bin = base / "bin"
        self.home = base / "home"
        self.home.mkdir()
        self.bin.mkdir()
        self.install()

    def install(self):
        version = self.data / "terminal" / "0.1.60"
        (version / "bin").mkdir(parents=True, exist_ok=True)
        tool = version / "bin" / "strata"
        tool.write_text('#!/bin/sh\necho "would stop"\n', encoding="utf-8")
        tool.chmod(0o755)
        (self.data / "terminal" / "current").symlink_to(version)
        (self.bin / "strata").symlink_to(self.data / "terminal" / "current" / "bin" / "strata")

    def run_sh(self, *args):
        env = {k: v for k, v in os.environ.items() if k not in ("XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CONFIG_HOME", "STRATA_HOME")}
        env["HOME"] = str(self.home)
        env["STRATA_DATA_DIR"] = str(self.data)
        env["STRATA_BIN_DIR"] = str(self.bin)
        r = subprocess.run([sh_binary(), str(SH), *args], capture_output=True, text=True, env=env, timeout=120,
                           stdin=subprocess.DEVNULL)
        return r.returncode, r.stdout + r.stderr

    def test_a_plain_run_removes_the_terminal_and_keeps_the_data(self):
        (self.data / "components" / "engine").mkdir(parents=True)
        (self.data / "models" / "qwen").mkdir(parents=True)
        (self.data / "models" / "qwen" / "model.gguf").write_bytes(b"weights")
        (self.home / ".config" / "strata").mkdir(parents=True)
        (self.home / ".strata" / "chats").mkdir(parents=True)
        code, out = self.run_sh()
        self.assertEqual(code, 0, out)
        self.assertIn("Strata Terminal was removed", out)
        self.assertFalse((self.data / "terminal").exists(), out)
        self.assertFalse(os.path.lexists(self.bin / "strata"), out)
        self.assertTrue((self.data / "components" / "engine").is_dir(), out)
        self.assertTrue((self.data / "models" / "qwen" / "model.gguf").is_file(), out)
        self.assertTrue((self.home / ".config" / "strata").is_dir(), out)
        self.assertTrue((self.home / ".strata" / "chats").is_dir(), out)
        code, out = self.run_sh()                                  # a second run is harmless
        self.assertEqual(code, 0, out)
        self.assertIn("not installed", out)

    def test_purge_removes_strata_folders_and_never_a_model_folder(self):
        (self.data / "components" / "engine").mkdir(parents=True)
        (self.data / "tuning").mkdir()
        (self.data / "models" / "qwen").mkdir(parents=True)
        (self.data / "models" / "qwen" / "model.gguf").write_bytes(b"weights")
        (self.home / ".config" / "strata").mkdir(parents=True)
        (self.home / ".strata" / "chats").mkdir(parents=True)
        code, out = self.run_sh("--purge", "--yes")
        self.assertEqual(code, 0, out)
        self.assertFalse((self.data / "components").exists(), out)
        self.assertFalse((self.data / "tuning").exists(), out)
        self.assertFalse((self.home / ".config" / "strata").exists(), out)
        self.assertFalse((self.home / ".strata").exists(), out)
        self.assertTrue((self.data / "models" / "qwen" / "model.gguf").is_file(), out)
        self.assertIn("Kept: every model folder", out)

    def test_purge_without_an_answer_stops_before_removing_anything(self):
        code, out = self.run_sh("--purge")                         # stdin is empty: no one can answer
        self.assertNotEqual(code, 0, out)
        self.assertIn("--yes", out)
        self.assertTrue((self.data / "terminal" / "0.1.60" / "bin" / "strata").exists(), out)

    def test_purge_keeps_the_folder_named_by_strata_home(self):
        own = self.home / "my-strata"
        (own / "chats").mkdir(parents=True)
        (self.home / ".config" / "strata").mkdir(parents=True)
        env = {k: v for k, v in os.environ.items() if k not in ("XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CONFIG_HOME")}
        env.update(HOME=str(self.home), STRATA_DATA_DIR=str(self.data), STRATA_BIN_DIR=str(self.bin), STRATA_HOME=str(own))
        r = subprocess.run([sh_binary(), str(SH), "--purge", "--yes"], capture_output=True, text=True, env=env, timeout=120,
                           stdin=subprocess.DEVNULL)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue((own / "chats").is_dir(), r.stdout)
        self.assertFalse((self.home / ".config" / "strata").exists(), r.stdout)

    def test_plan_changes_nothing(self):
        code, out = self.run_sh("--plan", "--purge")
        self.assertEqual(code, 0, out)
        self.assertIn("Nothing was changed", out)
        self.assertTrue((self.data / "terminal" / "0.1.60" / "bin" / "strata").exists(), out)
        self.assertTrue(os.path.lexists(self.bin / "strata"), out)

    def test_another_programs_strata_and_an_unknown_folder_are_left_alone(self):
        (self.bin / "strata").unlink()
        (self.bin / "strata").symlink_to("/opt/other/strata")
        (self.data / "terminal" / "notes").mkdir()
        code, out = self.run_sh()
        self.assertEqual(code, 0, out)
        self.assertTrue(os.path.lexists(self.bin / "strata"), out)
        self.assertEqual(os.readlink(self.bin / "strata"), "/opt/other/strata")
        self.assertTrue((self.data / "terminal" / "notes").is_dir(), out)
        self.assertFalse((self.data / "terminal" / "0.1.60").exists(), out)


@unittest.skipUnless(os.name == "nt" and PS, "needs Windows and PowerShell")
class WindowsUninstall(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        self.data = base / "data"
        self.home = base / "home"
        self.home.mkdir()
        version = self.data / "terminal" / "0.1.60"
        (version / "bin").mkdir(parents=True)
        (version / "bin" / "strata.cmd").write_text("@echo off\r\nexit /b 0\r\n", encoding="ascii")
        self.current = self.data / "terminal" / "current"
        subprocess.run(["cmd", "/c", "mklink", "/J", str(self.current), str(version)], check=True, capture_output=True)

    def run_ps(self, *args):
        cmd = "& '%s' -DataDir '%s' -NoPath %s" % (PS1, self.data, " ".join(args))
        env = {k: v for k, v in os.environ.items() if k not in ("STRATA_DATA_DIR", "STRATA_HOME")}
        env["STRATA_NO_PATH"] = "1"
        env["USERPROFILE"] = str(self.home)                       # the script reads the environment, never the live profile
        r = subprocess.run([PS, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", cmd], capture_output=True, text=True,
                           env=env, timeout=180, stdin=subprocess.DEVNULL)
        self.assertNotIn("strata uninstaller stopped", r.stdout + r.stderr)   # no Die() path was taken
        return r.returncode, r.stdout + r.stderr

    def test_a_plain_run_removes_the_terminal_and_keeps_the_data(self):
        (self.data / "components" / "engine").mkdir(parents=True)
        (self.data / "models" / "qwen").mkdir(parents=True)
        (self.data / "models" / "qwen" / "model.gguf").write_bytes(b"weights")
        (self.home / ".config" / "strata").mkdir(parents=True)
        (self.home / ".strata" / "chats").mkdir(parents=True)
        code, out = self.run_ps()
        self.assertEqual(code, 0, out)
        self.assertIn("Strata Terminal was removed", out)
        self.assertFalse(os.path.lexists(self.current), out)
        self.assertFalse((self.data / "terminal").exists(), out)
        self.assertTrue((self.data / "components" / "engine").is_dir(), out)
        self.assertTrue((self.data / "models" / "qwen" / "model.gguf").is_file(), out)
        self.assertTrue((self.home / ".config" / "strata").is_dir(), out)
        self.assertTrue((self.home / ".strata" / "chats").is_dir(), out)
        self.assertIn("your user PATH was not changed", out)
        code, out = self.run_ps()                                  # a second run is harmless
        self.assertEqual(code, 0, out)
        self.assertIn("not installed", out)

    def test_purge_removes_strata_folders_and_never_a_model_folder(self):
        (self.data / "components" / "engine").mkdir(parents=True)
        (self.data / "tuning").mkdir()
        (self.data / "models" / "qwen").mkdir(parents=True)
        (self.data / "models" / "qwen" / "model.gguf").write_bytes(b"weights")
        (self.home / ".config" / "strata").mkdir(parents=True)
        (self.home / ".strata" / "chats").mkdir(parents=True)
        code, out = self.run_ps("-Purge", "-Yes")
        self.assertEqual(code, 0, out)
        self.assertFalse((self.data / "components").exists(), out)
        self.assertFalse((self.data / "tuning").exists(), out)
        self.assertFalse((self.home / ".config" / "strata").exists(), out)
        self.assertFalse((self.home / ".strata").exists(), out)
        self.assertTrue((self.data / "models" / "qwen" / "model.gguf").is_file(), out)
        self.assertIn("Kept: every model folder", out)

    def test_plan_changes_nothing(self):
        code, out = self.run_ps("-Plan", "-Purge")
        self.assertEqual(code, 0, out)
        self.assertIn("Nothing was changed", out)
        self.assertTrue(self.current.exists(), out)
        self.assertTrue((self.data / "terminal" / "0.1.60" / "bin" / "strata.cmd").exists(), out)

    def test_the_path_entry_function_removes_only_strata_entries(self):
        text = PS1.read_text(encoding="utf-8")
        m = re.search(r"(?ms)^function Remove-StrataPathEntry.*?^\}", text)
        self.assertTrue(m, "the PATH edit is not a function of its own")
        harness = r"""
$r = Remove-StrataPathEntry -Value 'C:\Windows;C:\x\Strata\terminal\current\bin;D:\keep' -Entry 'C:\x\Strata\terminal\current\bin' -Terminal 'C:\x\Strata\terminal'
"got=[$r]"
$none = Remove-StrataPathEntry -Value 'C:\Windows;D:\keep' -Entry 'C:\x\Strata\terminal\current\bin' -Terminal 'C:\x\Strata\terminal'
"none=[$none]"
$case = Remove-StrataPathEntry -Value 'C:\Windows;C:\X\STRATA\TERMINAL\CURRENT\BIN' -Entry 'C:\x\Strata\terminal\current\bin' -Terminal 'C:\x\Strata\terminal'
"case=[$case]"
$old = Remove-StrataPathEntry -Value 'C:\Windows;C:\x\Strata\terminal\0.1.43\bin;D:\keep' -Entry 'C:\x\Strata\terminal\current\bin' -Terminal 'C:\x\Strata\terminal'
"old=[$old]"
"""
        script = Path(self.home) / "pathfn.ps1"
        script.write_text(m.group(0) + harness, encoding="ascii")
        r = subprocess.run([PS, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)], capture_output=True,
                           text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("got=[C:\\Windows;D:\\keep]", r.stdout)
        self.assertIn("none=[]", r.stdout)
        self.assertIn("case=[C:\\Windows]", r.stdout)
        self.assertIn("old=[C:\\Windows;D:\\keep]", r.stdout)
