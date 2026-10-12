"""Read the desktop shortcut and the installed Strata-data layout, without needing a shell."""
import os
import re
import sys
from pathlib import Path

OUT = []


def say(*a):
    OUT.append(" ".join(str(x) for x in a))


say()
say("=== repo root files")
for p in sorted(Path(r"d:\Strata").glob("*.json")):
    say("  ", p.name, p.stat().st_size)
for p in sorted(Path(r"d:\Strata").glob("*.log")):
    say("  ", p.name, p.stat().st_size, p.stat().st_mtime)

say()
say("=== engine dir")
eng = Path(r"d:\Strata\engine")
if eng.exists():
    for p in sorted(eng.iterdir()):
        say("  ", p.name, p.stat().st_size if p.is_file() else "<dir>")
else:
    say("   (missing)")
if (eng / "BUILD.json").exists():
    say("BUILD.json:", (eng / "BUILD.json").read_text(errors="replace")[:2000])

say()
say("=== data root D:\\Strata-data")
root = Path(r"D:\Strata-data")
if not root.exists():
    say("   (missing) - looking for others")
    for base in (r"D:\\", r"C:\\"):
        b = Path(base)
        if b.exists():
            for p in sorted(b.iterdir()):
                if p.is_dir() and "trata" in p.name:
                    say("   candidate:", p)
else:
    for p in sorted(root.iterdir()):
        say("  ", p.name, "<dir>" if p.is_dir() else p.stat().st_size)
    md = root / "models"
    if md.is_dir():
        for p in sorted(md.iterdir()):
            say("     models/", p.name, "<dir>" if p.is_dir() else p.stat().st_size)
    for pack in sorted((root / "packs").glob("*")) if (root / "packs").is_dir() else []:
        say("     packs/", pack.name)

say()
say("=== active config?")
for p in sorted(Path(r"d:\Strata").glob("strata-*.json")):
    say("---", p.name)
    say(p.read_text(encoding="utf-8-sig"))

print("\n".join(OUT))
