#!/usr/bin/env python3
"""Tests for tools/skills_mcp.py (the skills tool the Strata chat page calls).

No network, no GPU, no SDK: a small skills folder in a temporary directory, and
the server's own functions plus one stdio round trip.

Run: python -m pytest tools/test_skills_mcp.py -q
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
sys.path.insert(0, str(TOOLS))

import skills_mcp as S  # noqa: E402

SKILLS = str(TOOLS / "skills_mcp.py")

TDD = """---
name: tdd
description: Test-driven development. Use when the user wants a feature test-first.
---

# Test-Driven Development

Red, then green. See [tests.md](tests.md).
"""

HANDOFF = """---
name: handoff
description: "Compact the conversation into a handoff document."
argument-hint: "What will the next session be used for?"
disable-model-invocation: true
metadata:
  owner: someone
---

# Handoff

Write the note.
"""


@pytest.fixture()
def library(tmp_path: Path) -> S.Library:
    """A skills folder with two skills, one supporting file, and a file that is not a skill."""
    tdd = tmp_path / "skills" / "engineering" / "tdd"
    tdd.mkdir(parents=True)
    (tdd / "SKILL.md").write_text(TDD, encoding="utf-8")
    (tdd / "tests.md").write_text("# Good and Bad Tests\n\nTest through real interfaces.\n",
                                 encoding="utf-8")
    handoff = tmp_path / "skills" / "productivity" / "handoff"
    handoff.mkdir(parents=True)
    (handoff / "SKILL.md").write_text(HANDOFF, encoding="utf-8")
    notes = tmp_path / "docs" / "notes"
    notes.mkdir(parents=True)
    (notes / "README.md").write_text("# Notes, not a skill\n", encoding="utf-8")
    hidden = tmp_path / ".git" / "objects"
    hidden.mkdir(parents=True)
    (hidden / "SKILL.md").write_text(TDD, encoding="utf-8")     # must never be listed
    return S.Library(str(tmp_path))


# ------------------------------------------------------------------------------ the index

def test_scan_finds_every_skill_and_skips_other_files(library: S.Library):
    assert [s.id for s in library.skills] == ["skills/engineering/tdd",
                                              "skills/productivity/handoff"]
    assert all(s.path.name == "SKILL.md" for s in library.skills)


def test_frontmatter_is_read_and_unquoted(library: S.Library):
    tdd, handoff = library.skills
    assert tdd.name == "tdd"
    assert tdd.description.startswith("Test-driven development")
    assert handoff.description == "Compact the conversation into a handoff document."
    assert handoff.hint == "What will the next session be used for?"
    assert handoff.user_invoked is True
    assert tdd.user_invoked is False


def test_a_file_without_frontmatter_gets_its_folder_name(tmp_path: Path):
    folder = tmp_path / "plain"
    folder.mkdir()
    (folder / "SKILL.md").write_text("# Plain skill\n\nDo the plain thing.\n", encoding="utf-8")
    skill = S.Library(str(tmp_path)).skills[0]
    assert skill.name == "plain"
    assert skill.description == "Do the plain thing."


# ------------------------------------------------------------------------------ resolve

def test_resolve_by_id_name_folder_and_path(library: S.Library):
    tdd = library.skills[0]
    for query in ("skills/engineering/tdd", "skills\\engineering\\tdd", "tdd",
                  "engineering/tdd", str(tdd.path), str(tdd.folder)):
        assert library.resolve(query) is tdd, query


def test_resolve_an_unknown_skill_is_refused(library: S.Library):
    with pytest.raises(S.Refused, match="no skill named"):
        library.resolve("not-a-skill")


def test_resolve_an_ambiguous_part_is_refused(tmp_path: Path):
    for name in ("one", "one-thing"):
        folder = tmp_path / name
        folder.mkdir()
        (folder / "SKILL.md").write_text(f"---\nname: {name}\n---\n", encoding="utf-8")
    with pytest.raises(S.Refused, match="matches 2 skills"):
        S.Library(str(tmp_path)).resolve("on")


def test_resolve_a_path_outside_the_root_is_refused(library: S.Library, tmp_path: Path):
    outside = tmp_path.parent / "elsewhere" / "SKILL.md"
    with pytest.raises(S.Refused, match="outside the skills folder"):
        library.resolve(str(outside))


# ------------------------------------------------------------------------------ the tools

def test_list_skills_shows_id_name_description_and_file(library: S.Library):
    text = S.list_skills(library)
    assert "2 skill(s) under" in text
    assert "- skills/engineering/tdd  (tdd)" in text
    assert "SKILL.md" in text
    assert "[user-invoked]" in text


def test_list_skills_scope_filters(library: S.Library):
    text = S.list_skills(library, "handoff")
    assert "handoff" in text and "tdd" not in text


def test_read_skill_returns_the_whole_file_and_its_neighbours(library: S.Library):
    text = S.read_skill(library, "tdd")
    assert "# Test-Driven Development" in text
    assert "tests.md" in text
    assert "Supporting files" in text
    assert "user-invoked" in S.read_skill(library, "handoff")


def test_read_skill_file_reads_inside_the_skill(library: S.Library):
    assert "Test through real interfaces" in S.read_skill_file(library, "tdd", "tests.md")
    assert "SKILL.md" in S.read_skill_file(library, "tdd", ".")


@pytest.mark.parametrize("path", ["../tests.md", "..\\..\\outside.txt", "/etc/passwd",
                                  "C:\\Windows\\win.ini", "\\\\server\\share\\x.txt"])
def test_read_skill_file_refuses_anything_outside_the_skill(library: S.Library, path: str):
    with pytest.raises(S.Refused):
        S.read_skill_file(library, "tdd", path)


def test_read_skill_file_refuses_a_missing_file(library: S.Library):
    with pytest.raises(S.Refused, match="no such file"):
        S.read_skill_file(library, "tdd", "missing.md")


def test_search_skills_names_the_lines(library: S.Library):
    text = S.search_skills(library, "green")
    assert "1 skill(s) mention 'green'" in text
    assert "skills/engineering/tdd:" in text
    assert "no skill" in S.search_skills(library, "nothing-here-at-all")


def test_an_empty_root_is_refused(tmp_path: Path):
    with pytest.raises(S.Refused, match="does not exist"):
        S.Library(str(tmp_path / "missing"))


def test_the_library_never_writes(library: S.Library, tmp_path: Path):
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    S.list_skills(library)
    S.read_skill(library, "tdd")
    S.search_skills(library, "test")
    after = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert before == after


# ------------------------------------------------------------------------------ the wire

def test_initialize_lists_the_four_tools(library: S.Library):
    init = S.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                     "params": {"protocolVersion": "2025-06-18"}}, library)
    assert init["result"]["serverInfo"]["name"] == "skills"
    assert init["result"]["capabilities"] == {"tools": {}}
    listed = S.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, library)
    assert [t["name"] for t in listed["result"]["tools"]] == [
        "list_skills", "read_skill", "read_skill_file", "search_skills"]


def test_a_tool_call_answers_text_and_a_bad_one_answers_iserror(library: S.Library):
    ok = S.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                   "params": {"name": "list_skills", "arguments": {}}}, library)["result"]
    assert ok["isError"] is False and "tdd" in ok["content"][0]["text"]

    unknown = S.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                        "params": {"name": "no_such_tool", "arguments": {}}}, library)["result"]
    assert unknown["isError"] is True and "no tool named" in unknown["content"][0]["text"]

    missing = S.handle({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                        "params": {"name": "read_skill", "arguments": {"skill": "nope"}}},
                       library)["result"]
    assert missing["isError"] is True and "no skill named" in missing["content"][0]["text"]


def test_an_unknown_method_is_an_error_and_a_notification_is_silent(library: S.Library):
    answer = S.handle({"jsonrpc": "2.0", "id": 6, "method": "prompts/list"}, library)
    assert answer["error"]["code"] == -32601
    assert S.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}, library) is None


def test_the_stdio_server_answers_a_real_conversation(tmp_path: Path):
    folder = tmp_path / "hello"
    folder.mkdir()
    (folder / "SKILL.md").write_text("---\nname: hello\n---\n\n# Hello skill\n\nSay hello.\n",
                                     encoding="utf-8")
    lines = [
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18"}}),
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                    "params": {"name": "read_skill", "arguments": {"skill": "hello"}}}),
    ]
    proc = subprocess.run([sys.executable, SKILLS, "--root", str(tmp_path)],
                          input="\n".join(lines) + "\n", capture_output=True, text=True,
                          encoding="utf-8", timeout=60)
    assert proc.returncode == 0, proc.stderr
    answers = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
    assert [a.get("id") for a in answers] == [1, 2]
    assert answers[1]["result"]["isError"] is False
    assert "# Hello skill" in answers[1]["result"]["content"][0]["text"]
    assert "ready on stdio" in proc.stderr

