#!/usr/bin/env python3
r"""
A skills MCP server for the Strata model's chat page.

WHAT THIS IS

`serve/mcp.py` lets the Strata model call tools from MCP servers listed in
`strata-<model>.json`. This one gives it the skills in a folder such as
`D:\skills`: it lists them (name, description, path), reads one whole, reads a
supporting file inside one, and searches their text. The model then follows the
skill with the file tools it has beside this one.

Standard library only, for the same reason `tools/filesystem_mcp.py` is: a tool
that needs a package installed is a tool that fails the first time it is called.

WHAT IT WILL NOT DO

  * Nothing outside the root. Every path is realpath'd and refused if it lands
    outside. UNC and device paths are refused. `read_skill_file` takes a path
    relative to the skill's own folder and refuses an absolute one, because the
    whole point is to read what a skill carries with it, not anywhere on disk.
  * Nothing is written. This server is read-only: skills are edited with the
    files server, which is gated by its policy file.

    python tools/skills_mcp.py                  # run as a server (stdio)
    python tools/skills_mcp.py --root D:\skills
    python tools/skills_mcp.py --selftest       # exercise the tools and exit
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

PROTOCOL = "2025-06-18"
SERVER_NAME = "skills"
SERVER_VERSION = "1.0.0"

DEFAULT_ROOT = "D:\\skills"
SKILL_FILE = "SKILL.md"
MAX_READ_BYTES = 400_000          # a skill's supporting file: refuse anything larger in one call
MAX_LIST = 200                    # skills named in one list_skills answer
MAX_SEARCH_SKILLS = 20            # skills named in one search answer
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", ".pytest_cache"}


def _force_utf8() -> None:
    """Windows opens stdio with the ANSI code page; the wire format is UTF-8."""
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


_force_utf8()


def log(message: str) -> None:
    print(f"[{SERVER_NAME}] {message}", file=sys.stderr, flush=True)


class Refused(Exception):
    """A request this server will not carry out, whatever the model asks."""


def _under(path: Path | str, root: Path | str) -> bool:
    """Is `path` the root itself, or something below it? (case-folded, as Windows compares)."""
    a = os.path.normcase(str(path))
    b = os.path.normcase(str(root)).rstrip("\\/")
    return a == b or a.startswith(b + "\\")


def _confined_root(raw: str) -> Path:
    """The one folder this server may look in, resolved, or Refused."""
    text = str(raw or "").strip().strip('"')
    if text.startswith("\\\\") or text.startswith("//"):
        raise Refused(f"UNC paths are not allowed: {text}")
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    root = Path(os.path.realpath(candidate))
    if not root.is_dir():
        raise Refused(f"the skills folder does not exist: {root}")
    return root


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """(frontmatter keys, body) from a SKILL.md, or ({}, text) when there is none.

    A skill's frontmatter is the `---` block Claude Code skills use. Only the
    single-line `key: value` entries are read: `name`, `description`,
    `argument-hint` and `disable-model-invocation`. Nested blocks (`metadata:`)
    and list items are stepped over rather than guessed at.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    for i in range(1, min(len(lines), 400)):
        if lines[i].strip() != "---":
            continue
        data: dict[str, str] = {}
        for raw in lines[1:i]:
            if not raw.strip() or raw[:1] in (" ", "\t", "-", "#") or ":" not in raw:
                continue
            key, value = raw.split(":", 1)
            key, value = key.strip().lower(), value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if key and value:
                data[key] = value
        return data, "\n".join(lines[i + 1:]).lstrip("\n")
    return {}, text


def _first_paragraph(body: str) -> str:
    """A description for a skill that has no frontmatter description."""
    for raw in body.splitlines():
        line = raw.strip()
        if line and not line.startswith(("#", "|", ">", "```")):
            return line[:400]
    return ""


class Skill:
    """One SKILL.md and the folder it lives in."""

    __slots__ = ("id", "name", "description", "folder", "path", "text",
                 "hint", "user_invoked")

    def __init__(self, rid: str, meta: dict, folder: Path, path: Path, text: str, body: str):
        self.id = rid
        self.name = (meta.get("name") or folder.name).strip()
        self.description = (meta.get("description") or _first_paragraph(body)).strip()
        self.folder, self.path, self.text = folder, path, text
        self.hint = (meta.get("argument-hint") or "").strip()
        self.user_invoked = str(meta.get("disable-model-invocation", "")).strip().lower() == "true"


class Library:
    """Every skill under one root, indexed once when the server starts."""

    def __init__(self, root: str):
        self.root = _confined_root(root)
        self.skills = self._scan()
        self.by_id = {s.id.lower(): s for s in self.skills}

    def _scan(self) -> list[Skill]:
        found: list[Skill] = []
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = sorted(d for d in dirnames if d.lower() not in SKIP_DIRS)
            if SKILL_FILE not in filenames:
                continue
            folder = Path(dirpath)
            path = folder / SKILL_FILE
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                log(f"skipping {path}: {exc}")
                continue
            meta, body = _parse_frontmatter(text)
            rid = folder.relative_to(self.root).as_posix()
            found.append(Skill(rid, meta, folder, path, text, body))
        found.sort(key=lambda s: s.id.lower())
        return found

    def resolve(self, query: str) -> Skill:
        """The one skill named by an id, a name, a folder name, or a path to its SKILL.md."""
        text = str(query or "").strip().strip('"')
        if not text:
            raise Refused("no skill named. Use list_skills to see the ids.")
        plain = text.replace("\\", "/").strip("/").lower()
        hit = self.by_id.get(plain)
        if hit is not None:
            return hit

        if os.path.isabs(text) or "/" in text or "\\" in text or text.lower() == SKILL_FILE.lower():
            candidate = Path(text)
            if not candidate.is_absolute():
                candidate = self.root / candidate
            real = Path(os.path.realpath(candidate))
            if not _under(real, self.root):
                raise Refused(f"{text!r} is outside the skills folder {self.root}")
            if real.is_dir():
                real = real / SKILL_FILE
            for s in self.skills:
                if os.path.normcase(str(s.path)) == os.path.normcase(str(real)):
                    return s

        for match in ([s for s in self.skills if s.name.lower() == text.lower()]
                      + [s for s in self.skills if s.folder.name.lower() == text.lower()]):
            return match
        partial = [s for s in self.skills
                   if text.lower() in s.id.lower() or text.lower() in s.name.lower()]
        if len(partial) == 1:
            return partial[0]
        if partial:
            raise Refused(f"{text!r} matches {len(partial)} skills: "
                          + ", ".join(s.id for s in partial[:15])
                          + " - name one exactly, or use its path.")
        raise Refused(f"no skill named {text!r}. Use list_skills to see the ids.")

    def file_in(self, skill: Skill, relative: str) -> Path:
        """A path inside one skill's folder, or Refused."""
        text = str(relative or "").strip().strip('"')
        if not text:
            raise Refused("no file named")
        if text.startswith("\\\\") or text.startswith("//"):
            raise Refused(f"UNC paths are not allowed: {text}")
        if text.startswith("\\\\.\\"):
            raise Refused(f"device paths are not allowed: {text}")
        if os.path.isabs(text) or re.match(r"^[A-Za-z]:", text):
            raise Refused(f"{text!r} is absolute: give the path relative to {skill.id}")
        target = Path(os.path.realpath(skill.folder / text))
        if not _under(target, skill.folder):
            raise Refused(f"{text!r} leads outside the skill folder {skill.folder}")
        return target


# --------------------------------------------------------------------------
# The tools
# --------------------------------------------------------------------------

def list_skills(lib: Library, scope: str = "") -> str:
    """Every skill, or the ones matching a word."""
    q = str(scope or "").strip().lower()
    rows = [s for s in lib.skills
            if not q or q in s.id.lower() or q in s.name.lower() or q in s.description.lower()]
    if not rows:
        return (f"no skill matches {scope!r} under {lib.root} "
                f"({len(lib.skills)} skill(s) there)")
    head = f"{len(rows)} skill(s) under {lib.root}"
    if q:
        head += f" matching {scope!r}"
    lines = [head + ".", ""]
    for s in rows[:MAX_LIST]:
        tag = "  [user-invoked]" if s.user_invoked else ""
        lines += [f"- {s.id}  ({s.name}){tag}", f"    {s.description}", f"    file: {s.path}"]
    if len(rows) > MAX_LIST:
        lines.append(f"... and {len(rows) - MAX_LIST} more")
    lines += ["", "Read one with read_skill(skill=\"<id>\"), then read the files it lists "
                  "with read_skill_file (or the files server's read_file)."]
    return "\n".join(lines)


def read_skill(lib: Library, skill: str) -> str:
    """One SKILL.md whole, and the files that sit beside it."""
    s = lib.resolve(skill)
    files = []
    for path in sorted(s.folder.rglob("*")):
        try:
            rel = path.relative_to(s.folder)
        except ValueError:
            continue
        if path.is_file() and path.name != SKILL_FILE and not any(
                part.lower() in SKIP_DIRS for part in rel.parts):
            try:
                files.append((rel.as_posix(), path.stat().st_size))
            except OSError:
                files.append((rel.as_posix(), 0))
    parent = s.folder.parent
    category = parent.relative_to(lib.root).as_posix() if parent != lib.root else "."
    lines = [f"# {s.name}", f"id: {s.id}", f"category: {category}", f"file: {s.path}"]
    if s.hint:
        lines.append(f"argument-hint: {s.hint}")
    if s.user_invoked:
        lines.append("This skill is user-invoked: follow it when the person asks for it by name.")
    lines += ["", s.text.strip(), ""]
    if files:
        lines.append("Supporting files in this skill folder "
                     "(read with read_skill_file(skill, path)):")
        for rel, size in files[:60]:
            lines.append(f"  {rel}  ({size:,} bytes)")
        if len(files) > 60:
            lines.append(f"  ... and {len(files) - 60} more")
    else:
        lines.append("This skill folder holds no other files.")
    return "\n".join(lines)


def read_skill_file(lib: Library, skill: str, path: str) -> str:
    """A supporting file inside one skill's folder, or a folder listing."""
    s = lib.resolve(skill)
    target = lib.file_in(s, path)
    if not target.exists():
        raise Refused(f"no such file in {s.id}: {path}")
    if target.is_dir():
        entries = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        lines = [f"{target} - {len(entries)} entries"]
        for p in entries:
            if p.is_dir():
                lines.append(f"  dir   {p.name}/")
            else:
                try:
                    lines.append(f"  {p.stat().st_size:>10,}  {p.name}")
                except OSError:
                    lines.append(f"  ????  {p.name}")
        return "\n".join(lines)
    size = target.stat().st_size
    if size > MAX_READ_BYTES:
        raise Refused(f"{path} is {size:,} bytes; this tool reads up to {MAX_READ_BYTES:,} "
                      f"in one call - read it in parts with the files server")
    return target.read_text(encoding="utf-8", errors="replace")


def search_skills(lib: Library, query: str, limit: int = MAX_SEARCH_SKILLS) -> str:
    """The skills whose text mentions a word, with the matching lines."""
    q = str(query or "").strip()
    if not q:
        raise Refused("no search text given")
    try:
        limit = max(1, min(int(limit), 100))
    except (TypeError, ValueError):
        limit = MAX_SEARCH_SKILLS
    rx = re.compile(re.escape(q), re.I)
    names, hits = [], []
    for s in lib.skills:
        matched = []
        for number, line in enumerate(s.text.splitlines(), 1):
            if rx.search(line):
                matched.append(f"  {s.id}:{number}: {line.strip()[:200]}")
                if len(matched) >= 5:
                    break
        if matched:
            names.append(s.id)
            hits.extend(matched)
            if len(names) >= limit:
                break
    if not hits:
        return (f"no skill under {lib.root} mentions {q!r}. "
                f"Try list_skills, or a different word.")
    head = f"{len(names)} skill(s) mention {q!r}" + (f" (first {limit})" if len(names) >= limit else "")
    return head + "\n" + "\n".join(hits)


TOOLS = [
    {
        "name": "list_skills",
        "description": (
            "List the skills available to this chat: id, name, description and file. Use it "
            "first when the person asks for a skill by name, or when a task looks like one."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "scope": {"type": "string",
                          "description": "Optional word to match in an id, name or description."},
            },
        },
    },
    {
        "name": "read_skill",
        "description": (
            "Read one skill in full: its SKILL.md (frontmatter, instructions, examples) and "
            "the names of the files beside it. Follow the steps with the files server, and "
            "read a supporting file with read_skill_file."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "skill": {"type": "string",
                          "description": "Skill id (engineering/tdd), name (tdd), folder name, "
                                         "or path to its SKILL.md."},
            },
            "required": ["skill"],
        },
    },
    {
        "name": "read_skill_file",
        "description": (
            "Read a supporting file that belongs to one skill (such as tests.md or "
            "mocking.md). The path is relative to the skill's folder."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "skill": {"type": "string", "description": "Skill id, name, or path."},
                "path": {"type": "string",
                         "description": "File inside the skill's folder, e.g. tests.md."},
            },
            "required": ["skill", "path"],
        },
    },
    {
        "name": "search_skills",
        "description": (
            "Find the skills whose text mentions a word or phrase, with the matching lines. "
            "Use it when no skill name is known and the task is described in words."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Word or phrase to look for."},
                "limit": {"type": "integer",
                          "description": f"How many skills to report (default {MAX_SEARCH_SKILLS}, max 100)."},
            },
            "required": ["query"],
        },
    },
]

CALLS = {
    "list_skills": lambda lib, a: list_skills(lib, a.get("scope", "")),
    "read_skill": lambda lib, a: read_skill(lib, a.get("skill", "")),
    "read_skill_file": lambda lib, a: read_skill_file(lib, a.get("skill", ""), a.get("path", "")),
    "search_skills": lambda lib, a: search_skills(lib, a.get("query", ""), a.get("limit", MAX_SEARCH_SKILLS)),
}


def result(text: str, error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": error}


def handle(message: dict, lib: Library) -> dict | None:
    """The response for one JSON-RPC message, or None for a notification."""
    method, rid = message.get("method"), message.get("id")
    params = message.get("params") if isinstance(message.get("params"), dict) else {}
    if "id" not in message:
        return None                                   # notifications: initialized, cancelled
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": str(params.get("protocolVersion") or PROTOCOL),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "title": "Strata skills",
                           "version": SERVER_VERSION}}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}}
    if method == "tools/call":
        name = str(params.get("name") or "")
        arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        fn = CALLS.get(name)
        if fn is None:
            return {"jsonrpc": "2.0", "id": rid,
                    "result": result(f"error: there is no tool named {name!r}; the tools are "
                                     + ", ".join(CALLS), error=True)}
        try:
            text = fn(lib, arguments)
        except Refused as exc:
            text, ok = str(exc), False
        except (OSError, ValueError, TypeError) as exc:
            text, ok = f"{type(exc).__name__}: {exc}", False
        else:
            ok = True
        return {"jsonrpc": "2.0", "id": rid, "result": result(text, error=not ok)}
    return {"jsonrpc": "2.0", "id": rid,
            "error": {"code": -32601, "message": f"method not supported: {method}"}}


def serve(lib: Library) -> int:
    """JSON-RPC lines on stdin, answers on stdout, its log on stderr (as Strata runs it)."""
    log(f"ready on stdio (protocol {PROTOCOL})")
    log(f"root {lib.root} - {len(lib.skills)} skill(s)")
    for name in [s.id for s in lib.skills[:8]]:
        log(f"  {name}")
    if len(lib.skills) > 8:
        log(f"  ... and {len(lib.skills) - 8} more")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            log(f"ignoring a line that is not JSON: {line[:80]!r}")
            continue
        for one in message if isinstance(message, list) else [message]:
            if not isinstance(one, dict):
                continue
            try:
                answer = handle(one, lib)
            except Exception as exc:                   # noqa: BLE001 - one bad call must not end the server
                log(f"error handling {one.get('method')}: {exc}")
                if "id" in one:
                    answer = {"jsonrpc": "2.0", "id": one.get("id"),
                              "result": result(f"error: {type(exc).__name__}: {exc}", error=True)}
                else:
                    answer = None
            if answer is not None:
                sys.stdout.write(json.dumps(answer, ensure_ascii=False) + "\n")
                sys.stdout.flush()
    return 0


def selftest(lib: Library) -> int:
    """Exercise every tool against the real folder; print what it found."""
    failures: list[str] = []

    def check(label: str, condition: bool, detail: str = "") -> None:
        print(f"    {'ok  ' if condition else 'FAIL'}  {label}"
              + (f"   {detail[:200]}" if detail and not condition else ""))
        if not condition:
            failures.append(label)

    print(f"  skills root: {lib.root}")
    print(f"  skills found: {len(lib.skills)}")
    check("the folder holds at least one skill", bool(lib.skills))

    listing = list_skills(lib)
    check("list_skills names the first skill",
          bool(lib.skills) and lib.skills[0].id in listing, listing[:200])
    check("list_skills says how many", "skill(s) under" in listing)

    if lib.skills:
        first = lib.skills[0]
        body = read_skill(lib, first.id)
        check("read_skill returns its instructions",
              first.name in body and len(body) > len(first.text) - 1, body[:200])
        check("read_skill names its file", str(first.path) in body)
        files = [p for p in first.folder.rglob("*")
                 if p.is_file() and p.name != SKILL_FILE]
        if files:
            rel = files[0].relative_to(first.folder).as_posix()
            text = read_skill_file(lib, first.id, rel)
            check(f"read_skill_file reads {rel}", bool(text.strip()), text[:120])
        try:
            read_skill_file(lib, first.id, "..\\..\\outside.txt")
            check("read_skill_file refuses a path out of the folder", False)
        except Refused:
            check("read_skill_file refuses a path out of the folder", True)

    found = search_skills(lib, "test")
    check("search_skills answers", "skill(s) mention" in found or "no skill" in found, found[:200])

    try:
        lib.resolve("definitely-not-a-skill-name")
        check("an unknown skill is refused", False)
    except Refused:
        check("an unknown skill is refused", True)

    print(f"\n  {len(failures)} failure(s)")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="A read-only MCP server for the skills in a folder (Strata's chat page).")
    parser.add_argument("--root", default=DEFAULT_ROOT,
                        help=f"the folder holding the skills (default {DEFAULT_ROOT})")
    parser.add_argument("--selftest", action="store_true",
                        help="exercise the tools against the folder and exit")
    args = parser.parse_args(argv)
    try:
        lib = Library(args.root)
    except Refused as exc:
        log(f"cannot start: {exc}")
        return 2
    if args.selftest:
        return selftest(lib)
    return serve(lib)


if __name__ == "__main__":
    raise SystemExit(main())


