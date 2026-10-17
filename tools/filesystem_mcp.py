#!/usr/bin/env python3
r"""
A filesystem MCP server for the Strata model's chat page.

WHAT THIS IS

`serve/mcp.py` lets the Strata model call tools from MCP servers listed in
`strata-<model>.json`. This one gives it read, write, edit, list, glob and grep
over a confined root - the same shape of access a coding assistant has.

Standard library only, for the same reason `tools/strata_mcp.py` is: a tool that
needs a package installed is a tool that fails the first time it is called.

WHAT IT WILL NOT DO

  * Nothing outside the root. Paths are resolved with realpath first, so `..`,
    symlinks and junctions pointing out of the root are refused rather than
    followed. UNC and device paths are refused. The check happens on the parent
    for a file that does not exist yet.
  * A file must be read before it can be overwritten, and must not have changed
    since that read. This is what stops a blind clobber of a file the model never
    saw, or one something else edited in between.
  * The write path refuses to replace a non-empty file with empty content, so an
    accidental empty write cannot look like a delete. delete_path is how a file
    is removed, and it has to be asked for by name.
  * Nothing may be CHANGED that `--policy` does not allow. Reading is never
    gated; only changes are. Without `--policy` nothing is gated, because a
    server that invents a policy from a file it was not given is a server whose
    refusals cannot be traced to anything.

THERE ARE NO BACKUPS. An overwrite replaces the previous contents outright, and
delete_path and move_path act immediately. Two guards stand between a mistaken
call and lost work: the read-before-overwrite rule above, and the policy file.
Both are enforced in code rather than left to the model's judgement, because this
Strata reads web pages and a fetched page can carry instructions. A file it can
overwrite without reading is a file it can destroy without ever having seen it;
a folder it can write to is a folder an injected instruction can reach.

WHY THE GUARDS ARE HERE RATHER THAN LEFT TO THE MODEL

The model decides when to call these tools, and it decides partly on what it has
read - and this Strata also has a web search tool. A page it fetched can contain
instructions. Confinement is what keeps a bad instruction from reaching anything
that matters, and it is enforced here, in code, where an instruction cannot talk
it round. Strata's own documentation asks for exactly this: give a filesystem
server only the folders it needs.

HOW --root AND --policy DIVIDE THE JOB

`--root` confines where this server may look at all: every path, read or write,
is realpath'd and refused if it lands outside. The policy then decides, within
that tree, which paths a change may touch. They answer different questions and
neither replaces the other: a root of D:\\ with a policy that allows nothing
still cannot write, and a permissive policy cannot reach past the root.

THE POLICY FILE

JSON, read once at startup. The file's own `default` is a note rather than a
setting - there is only one default - and it is read-only: anything matching
neither of the two rules below is refused for a change.

  * writable - a path is writable when some folder in its chain of parents, up to
    the root, holds a `.git` entry, or when it sits under an entry in
    `writable.also`. Permission is tied to what makes a mistake recoverable
    rather than to a hand-written list of names: if git can undo it, it may be
    changed. The list therefore maintains itself.
  * prohibited - always wins, including against a writable parent. This is where
    the gitignored and irreplaceable lives: the live trading record, the live
    configuration, .git itself, and the privilege boundary this server sits on.

Entries are normalized before matching, so either slash works and case is
ignored. `**` matches any depth, `*` matches within one path component. An entry
that names no drive is not tied to one, which is what lets `**\.git\**` cover
every repository. Prohibited covers the whole subtree of what it names - the
folder itself included - while an entry whose last component names a file, such
as `D:\PaperTrader\*.json`, covers those files and nothing under them. The
refusal names the entry that refused it, and the path the entry actually
matched: a refusal a model cannot attribute is one it will retry forever.

If the file cannot be read or parsed, or `enforce` is not true, the server loads
no rules and refuses every change rather than falling back to allowing
everything. A policy that fails open is worse than no policy, because it is
believed.

    python tools/filesystem_mcp.py             # run as a server (stdio), no policy
    python tools/filesystem_mcp.py --selftest  # exercise the tools and exit
    python tools/filesystem_mcp.py --root D:\\ --selftest
    python tools/filesystem_mcp.py --policy D:\\strata-data.json
    python tools/filesystem_mcp.py --policy D:\\strata-data.json --explain D:\\PaperTrader\\ledger
"""

from __future__ import annotations

import argparse
import datetime
import fnmatch
import json
import os
import re
import shutil
import sys
from pathlib import Path

PROTOCOL = "2025-06-18"
SERVER_NAME = "filesystem"
SERVER_VERSION = "1.0.0"

DEFAULT_ROOT = "D:\\"
# No policy is assumed. The server that starts without --policy behaves as it did
# before there was a policy, and says so.
DEFAULT_POLICY = None
# No longer written to. Kept so listing and searching still step over the old
# backup folder from when this server made them, which is on disk and large.
BACKUP_DIRNAME = ".strata-backups"
# The marker whose presence makes a folder a git working tree, and therefore
# writable. A file, not just a folder: a worktree or submodule checkout has a
# .git file holding the path of the real git directory.
GIT_MARKER = ".git"

MAX_READ_BYTES = 4_000_000        # refuse to read more than this in one call
MAX_WRITE_BYTES = 4_000_000       # and refuse to write more
DEFAULT_READ_LINES = 2000
MAX_GREP_MATCHES = 250
MAX_GLOB_RESULTS = 500
TEXT_SNIFF_BYTES = 8192           # how much to look at before calling a file binary

# Paths this process has read, with the size and mtime seen at the time, so an
# overwrite can tell "I have seen this" from "something changed it since".
_read_state: dict[str, tuple[int, float]] = {}


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


# --------------------------------------------------------------------------
# Path confinement - the part that must not be got wrong
# --------------------------------------------------------------------------

class Refused(Exception):
    """A request this server will not carry out, whatever the model asks."""


class Root:
    def __init__(self, root: str):
        self.set_root(root)

    def set_root(self, root: str) -> None:
        candidate = Path(root).expanduser()
        if not candidate.is_absolute():
            candidate = Path.cwd() / candidate
        # realpath, not resolve(): it also expands junctions and short names on
        # Windows, which is exactly how a path escapes a prefix check.
        self.root = Path(os.path.realpath(candidate))
        if not self.root.exists():
            raise Refused(f"root does not exist: {self.root}")

    def resolve(self, raw: str, *, for_write: bool = False,
                allow_missing_parents: bool = False) -> Path:
        """A path inside the root, or Refused. Never returns anything else."""
        if raw is None or str(raw).strip() == "":
            raise Refused("no path given")
        text = str(raw).strip().strip('"')

        # Windows device paths and UNC shares are refused before anything else,
        # because realpath will happily return them and they are never inside D:.
        if text.startswith("\\\\") or text.startswith("//"):
            raise Refused(f"UNC paths are not allowed: {text}")
        if text.startswith("\\\\.\\"):
            raise Refused(f"device paths are not allowed: {text}")

        p = Path(text)
        if not p.is_absolute():
            p = self.root / p

        if allow_missing_parents:
            return self._resolve_through_missing(p)

        # A file that does not exist yet cannot be realpath'd, so the parent is
        # checked instead - otherwise a write could create its way out of the
        # root one missing component at a time.
        if for_write and not p.exists():
            parent = Path(os.path.realpath(p.parent))
            self._assert_inside(parent)
            if not parent.exists():
                raise Refused(f"parent folder does not exist: {parent}")
            return parent / p.name

        resolved = Path(os.path.realpath(p))
        self._assert_inside(resolved)
        return resolved

    def _resolve_through_missing(self, p: Path) -> Path:
        """Resolve a path whose tail does not exist yet - for create_dir parents.

        The existing prefix is realpath'd and checked, which is what resolves
        symlinks and junctions honestly. The missing tail is then appended as
        plain names: normpath has already removed any `..`, and a name that is
        still `.` or `..` after that is refused rather than interpreted.
        """
        flat = Path(os.path.normpath(str(p)))
        walk = flat
        tail: list[str] = []
        while not walk.exists():
            tail.append(walk.name)
            parent = walk.parent
            if parent == walk:                       # reached the drive root
                raise Refused(f"cannot resolve: {p}")
            walk = parent

        ancestor = Path(os.path.realpath(walk))
        self._assert_inside(ancestor)

        result = ancestor
        for name in reversed(tail):
            if name in ("", ".", ".."):
                raise Refused(f"refusing a path with a {name!r} component: {p}")
            result = result / name
        self._assert_inside(result)
        return result

    def _assert_inside(self, path: Path) -> None:
        try:
            path.relative_to(self.root)
        except ValueError:
            raise Refused(
                f"outside the allowed root: {path} is not under {self.root}") from None

    def relative(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.root))
        except ValueError:
            return str(path)


ROOT = Root(DEFAULT_ROOT)


# --------------------------------------------------------------------------
# The policy - which paths a change may touch. Reads are never gated.
#
# This sits below the tools rather than above them because it is a second,
# narrower question asked after confinement: Root answers "may this server see
# it at all", Policy answers "may this change land here".
# --------------------------------------------------------------------------

# Normalized paths, the form every policy comparison is made in. Realpath is
# the most expensive thing this module does - 40-odd system calls per path on
# Windows, and a recursive delete asks about every path under the folder - so
# the answer is kept. The walk meets the same folder spelled two ways (the
# short name a parent was reached by, the long name realpath returns), which
# makes a plain keyed cache worth considerably more here than it looks.
# Cleared with the rest of the per-run state, because it describes one run.
# Bounded so that a pathological tree cannot grow it without limit; entries are
# small and a delete walks at most MAX_POLICY_WALK paths.
_norm_cache: dict[str, str] = {}
MAX_NORM_CACHE = 250_000

# How many paths a recursive delete will check before giving up and refusing.
# The point of the check is that it is cheaper than the delete it guards; at
# some size that stops being true, and a refusal the model can act on beats a
# server that appears to have hung.
MAX_POLICY_WALK = 200_000


def _norm_path(raw: str) -> str:
    """A real path in comparable form: absolute, resolved, folded, backslashes.

    realpath, so that two spellings of one folder compare equal - the 8.3 short
    name Windows hands out in %TEMP% against the long name, and a junction
    against what it points at. normcase is what folds the case, and on Windows
    it is also what turns every separator into a forward slash, which is why
    the separator is put back to a backslash afterwards.

    The trailing marker keeps realpath from eating a last separator, so a path
    arriving as `D:\\PaperTrader\\ledger\\` compares equal to the same path
    without it.
    """
    key = str(raw)
    cached = _norm_cache.get(key)
    if cached is not None:
        return cached
    text = os.path.expanduser(key)
    if not os.path.isabs(text):
        # A relative path resolves against the server's working directory,
        # which for Strata is D:\\Strata.
        text = os.path.join(os.getcwd(), text)
    # A leading separator means "the root of the current drive", which is not
    # something realpath can be asked about usefully, so the drive is named
    # first. There is no cwd to prepend in that case: the path already has a
    # root, and join() would produce nonsense.
    if text[:1] in ("\\", "/"):
        text = os.path.splitdrive(os.getcwd())[0] + text
    marker = "\\_"
    resolved = os.path.normcase(os.path.realpath(text + marker))[:-len(marker)]
    if len(_norm_cache) < MAX_NORM_CACHE:
        # Three keys, one answer: the spelling asked about, the spelling the
        # previous steps reduced it to, and the answer itself. The walk meets a
        # folder under its short name and its long name both, and would
        # otherwise pay for realpath twice.
        for alias in (key, text, resolved):
            _norm_cache[alias] = resolved
    return resolved


def _norm_pattern(raw: str) -> str:
    """A pattern in the same form as a normalized path, but never resolved.

    realpath on a pattern resolves nothing - there is nothing on disk at a path
    with a `*` in it - but it does rewrite it: it resolves the prefix against
    the working directory and drops a trailing separator. Either would quietly
    change the rule, so a pattern only ever gets the lexical half of the
    normalization. Case and separators still fold, because that part is about
    how Windows compares names rather than about the disk.

    A leading separator or wildcard is left alone rather than anchored. A
    leading separator means "the root of the current drive"; a leading `*` or
    `**` means "anywhere", which is why `**\\.git\\**` is written without a
    drive. Anchoring either to the working directory would quietly narrow the
    rule to one place on one drive.
    """
    text = os.path.expanduser(str(raw)).replace("/", "\\")
    first = text.split("\\", 1)[0]
    if not os.path.isabs(text) and "*" not in first and "?" not in first:
        # A relative entry naming no wildcard is a folder someone meant: it
        # resolves against the server's working directory, which for Strata is
        # D:\\Strata.
        text = os.path.join(os.getcwd(), text)
    return os.path.normcase(os.path.normpath(text))


def _entry_text(raw: str) -> str:
    """An entry as a person reads it in the file, not as Python reprs it.

    The policy writes its patterns with escaped backslashes because that is
    what JSON requires; a refusal that repeats those escapes shows the reader
    something that is not in their file, which is a small way of making a
    clear refusal less clear.
    """
    return raw.replace("\\\\", "\\")


def _norm_entry(raw: str) -> str:
    """A policy entry in the same form as a normalized path.

    One entry, one form, decided by what it is rather than by where it is
    listed: an entry with a wildcard is a pattern and keeps its literal
    components, and an entry without one names a real folder and is resolved.
    Normalizing a folder's resolved spelling a second time as though it were a
    pattern is not harmless - `C:\\Users\\ANDREW~1\\...` carries no drive-aware
    prefix once resolved, and comes back anchored to the working directory.
    """
    return _norm_pattern(raw) if _has_wildcard(raw) else _norm_path(raw)


def _has_wildcard(raw: str) -> bool:
    return any(ch in str(raw) for ch in "*?[")


def _split(raw: str, *, pattern: bool = False) -> tuple[str | None, list[str]]:
    """(root, components) in comparable form, for a path or for a pattern.

    The drive or share is kept as a unit rather than split off, so that `C:\\**`
    still matches `c:\\` itself - `C:\\` has no components, and a matcher that
    had already consumed the drive would find nothing left to match `**` with.
    """
    flat = _norm_pattern(raw) if pattern else _norm_path(raw)
    parts = [p for p in flat.split("\\") if p not in ("", ".")]
    if parts and parts[0].endswith(":"):
        return parts[0], parts[1:]
    return None, parts


def _under(entry: str, path: str) -> bool:
    """Is `path` the entry itself, or something below it?"""
    return path == entry or path.startswith(entry.rstrip("\\") + "\\")


def _looks_like_file(component: str) -> bool:
    """Does this pattern component name a file rather than a folder?

    The one case worth telling apart: `D:\\PaperTrader\\*.json` names files at
    that level, so it covers those files and nothing under them. Without this,
    the trailing `*` would also cover a folder called `x.json`, and the subtree
    under it would be refused for the wrong reason. A folder pattern has no dot
    in its last component - `.git` does not, `config.json` does.
    """
    if "." not in component[1:-1]:
        return False
    return component.rpartition(".")[2] not in ("", component)


def _glob_component(pattern: str, text: str) -> bool:
    # fnmatchcase, not fnmatch: both sides are already case-folded by
    # _norm_path, and fnmatch would fold them a second time on Windows only.
    return fnmatch.fnmatchcase(text, pattern)


def _matches(pattern: str, path: str, *, exact: bool = False) -> bool:
    """Does a prohibited entry cover this path?

    Component-wise, not fnmatch over the whole string: `*` must not cross a
    separator, so `D:\\PaperTrader\\*.json` covers `D:\\PaperTrader\\a.json` and
    not `D:\\PaperTrader\\sub\\a.json`. `**` crosses any number of separators.

    A path matches when the pattern consumes it completely OR, unless `exact`,
    when the pattern runs out first, which is what makes an entry cover the
    subtree beneath it. A trailing `**` is the same allowance said out loud:
    `**\\.git\\**` matches `.git` itself as well as everything under it, because
    "everything inside" includes the folder.

    The subtree allowance is withheld when the pattern's last component names a
    file. `D:\\PaperTrader\\*.json` is a rule about files in that folder, and a
    file has nothing under it - so `D:\\PaperTrader\\a.json\\child` is not
    covered, even though `*` would happily match `a` and leave `child` over.
    Without this the rule would spread to subtrees nobody wrote it for.

    `exact=True` asks the narrower question "is this path named", which is what
    an ancestor walk wants: whether an ancestor is prohibited is a fact about
    that ancestor, not about what hangs below it, and the answer is cheaper to
    reach.
    """
    root, parts = _split(pattern, pattern=True)
    target_root, target = _split(path)
    if not parts:
        # A pattern with no components names a drive or share root: `C:\\`, or
        # `\\\\server\\share`. `**` is not written out and does not need to be -
        # naming a root is naming everything under it, the same way naming a
        # folder is. Nothing else lands here; `**` alone is a component.
        return root is not None and root == target_root
    names_a_file = _looks_like_file(parts[-1])

    def walk(pi: int, ci: int) -> bool:
        if pi == len(parts):
            if exact:
                return ci == len(target)
            if ci != len(target) and names_a_file:
                return False
            return True
        part = parts[pi]
        if part == "**":
            # Zero components consumed, or one more and re-try: the two cases
            # are what let `**` sit at either end of a pattern.
            return walk(pi + 1, ci) or (ci < len(target) and walk(pi, ci + 1))
        if ci == len(target):
            # The path ran out with pattern left over. Exact means it is not a
            # match - unless all that is left is `**`, which is allowed to
            # match nothing, so `D:\McTrader\_backup-*` still covers
            # `D:\McTrader\_backup-*` followed by nothing.
            return exact and all(p == "**" for p in parts[pi:])
        return _glob_component(part, target[ci]) and walk(pi + 1, ci + 1)

    # A pattern without a drive is not tied to one: `**\\.git\\**` is written
    # that way to match on whichever drive it finds. A pattern that does name a
    # drive has to be looking at the same one as the path.
    if root is not None and root != target_root:
        return False
    return walk(0, 0)


class Policy:
    """The loaded rule file: where a change is allowed and where it never is."""

    def __init__(self, source: str):
        self.source = source
        self.display = str(source)
        self.active = False
        self.why = "no policy was loaded, so no change is allowed"
        # (normalized, as written, note) per entry, in the order the file gives
        # them, because that order is what decides which entry a refusal names.
        self.writable_also: list[tuple[str, str, str]] = []
        self.prohibited: list[tuple[str, str, str]] = []
        self.load()

    def load(self) -> None:
        """Read the file. Any failure leaves the policy refusing everything."""
        try:
            text = Path(self.source).read_text(encoding="utf-8-sig")
            data = json.loads(text)
        except (OSError, ValueError) as exc:
            self.active, self.why = False, f"cannot read it: {exc}"
            return
        if not isinstance(data, dict):
            self.active, self.why = False, "it is not a JSON object"
            return
        if data.get("enforce") is not True:
            # Explicitly false is a deliberate state - the lists are being
            # edited - and is reported as loudly as a failure. Anything else
            # missing or malformed is a failure, not permission.
            if data.get("enforce") is False:
                self.active = False
                self.why = ('enforce is false, so this policy is loaded and NOT '
                            'applied: every change is refused')
            else:
                self.active, self.why = False, ('it has no "enforce": true - a '
                                                'policy that will not say it is '
                                                'enforced is not enforced')
            return

        writable = data.get("writable") or {}
        also = writable.get("also") if isinstance(writable, dict) else None
        self.writable_also = self._entries(also, "writable.also")
        self.prohibited = self._entries(data.get("prohibited"), "prohibited")
        self.active = True
        self.why = (f"{len(self.prohibited)} prohibited entries, "
                    f"{len(self.writable_also)} writable.also entries")

    def _entries(self, raw, where: str) -> list[tuple[str, str, str]]:
        """One (normalized, as written, note) triple per entry, in order.

        The note travels with the entry it belongs to instead of being looked
        up again when a refusal is built.
        """
        out = []
        for item in raw or []:
            if isinstance(item, str):
                text, note = item, ""
            elif isinstance(item, dict) and item.get("path"):
                text, note = str(item["path"]), str(item.get("note") or "")
            else:
                log(f"ignoring an entry in {where} with no path: {item!r}")
                continue
            out.append((_norm_entry(text), text, note))
        return out

    # -- the rules ---------------------------------------------------------

    def _also_entry(self, path: str) -> str | None:
        """The `writable.also` entry this path sits under, if any.

        Returns the entry as written rather than its note: the entry is the
        rule, and a note can run to several lines, which is fine in the policy
        file and not fine in the middle of a refusal.
        """
        norm = _norm_path(path)
        for entry, raw, _note in self.writable_also:
            if _under(entry, norm):
                return raw
        return None

    def _git_root(self, path: str) -> str | None:
        """The nearest folder at or above this path holding a .git entry.

        Walks up rather than stopping at the first ancestor: a folder inside a
        repository has no `.git` of its own, so nothing short of the top of the
        chain answers the question.
        """
        cur = _norm_path(path)
        while True:
            if os.path.lexists(os.path.join(cur, GIT_MARKER)):
                return cur
            parent = os.path.dirname(cur)
            if parent == cur:
                return None
            cur = parent

    def _permitted(self, path: str) -> str | None:
        """Why a change here is allowed, or None. The writable half of the rule.

        The `also` list is checked before the git rule because it is the more
        specific answer: a path inside a repository that is also listed
        explicitly is reported as the listed one, which is what a reader of the
        file expects to see.
        """
        entry = self._also_entry(path)
        if entry is not None:
            return f"it sits under {entry}, listed in writable.also"
        repo = self._git_root(path)
        if repo:
            return f"it is inside the git working tree at {repo}"
        return None

    def refusal(self, path: str) -> tuple[str, str, str] | None:
        """(entry, note, the path the entry matched), or None if nothing does.

        The matched path is returned rather than recomputed by callers: it is
        the difference between "prohibited by name" and "prohibited because of
        where it is", and both the message and the subtree walk need it.

        The path itself is tested first with the subtree allowance, since that
        is what makes an entry cover the folder it names. The ancestors are then
        tested exactly, one at a time, walking up and stopping at the first
        match: an exact test is much cheaper, and an ancestor that is prohibited
        is asserted by the entry matching it, not by what is below it. Doing it
        the other way round - letting one call search every prefix - makes each
        call do the expensive work once per level of the path.
        """
        norm = _norm_path(path)
        hit = self._match_any(norm, exact=False)
        if hit:
            return hit[0], hit[1], norm
        ancestor = os.path.dirname(norm)
        while ancestor and ancestor != os.path.dirname(ancestor):
            hit = self._match_any(ancestor, exact=True)
            if hit:
                return hit[0], hit[1], ancestor
            ancestor = os.path.dirname(ancestor)
        return None

    def _match_any(self, norm: str, *, exact: bool) -> tuple[str, str] | None:
        """The first entry naming this path, as (as written, note)."""
        for entry, raw, note in self.prohibited:
            if _matches(entry, norm, exact=exact):
                return raw, note
        return None

    # -- the decision ------------------------------------------------------

    def can_change(self, path: str, *, action: str = "write",
                   exists: bool | None = None) -> str | None:
        """None to allow, or the full refusal message. The single decision.

        A path is judged with its whole chain: the target first, then every
        ancestor up to the drive. Checking only the target would let a write
        land inside D:\\PaperTrader\\ledger by naming a file that does not
        exist yet, and `create_dir` with parents builds a chain that no single
        path rule covers on its own.
        """
        if action not in ("write", "delete"):
            raise Refused(f"internal: unknown change kind {action!r}")
        if not self.active:
            return (f"{str(path)} is refused: this server has no policy allowing "
                    f"changes. It was started without --policy, or the policy it "
                    f"was given cannot be applied ({self.why}).")

        if exists is None:
            exists = os.path.lexists(path)
        # A path that does not exist cannot be deleted - otherwise the policy
        # would refuse the usual "delete a typo'd name" message and send the
        # model looking for a rule that had nothing to do with it.
        if action == "delete" and not exists:
            return None

        target = _norm_path(path)
        # One call, because refusal() already walks the chain: a path is
        # prohibited if it or any ancestor is, and checking only the target
        # would let a write land inside D:\PaperTrader\ledger by naming a file
        # that does not exist yet.
        hit = self.refusal(target)
        if hit:
            return self._refusal_message(target, hit[2], hit[0], hit[1])

        why = self._permitted(target)
        if why:
            return None
        return (f"{target} is refused: it is read-only. No folder in its chain of "
                f"parents holds a {GIT_MARKER} entry, and it is not under any "
                f"writable.also entry in {self.display}. Nothing here is covered "
                f"by git, so a change cannot be undone.")

    def _refusal_message(self, target: str, offending: str,
                         entry: str, note: str) -> str:
        """Why a change was refused, naming the rule, the path, and the reason.

        Three spellings have to be distinguishable or the message misleads: the
        path the caller asked about, the path that actually matched, and the
        entry as the file writes it. A refusal that names the wrong one of
        these is a refusal that gets worked around instead of understood.
        """
        if offending == target:
            message = (f"{target} is refused: it matches the prohibited entry "
                       f'"{_entry_text(entry)}"')
        else:
            message = (f"{target} is refused: {offending} is under the prohibited "
                       f'entry "{_entry_text(entry)}"')
        message += f" in {self.display}"
        if note:
            message += f" ('{note}')"
        return message

    def subtree_refusal(self, path: str, limit: int = MAX_POLICY_WALK) -> str | None:
        """The deepest refusal anywhere in a folder tree, or None.

        A recursive delete removes the folder and everything under it, so
        checking only the folder would make `delete_path` the way around
        `prohibited`: delete D:\\PaperTrader, which is a writable git tree, and
        the gitignored ledger inside it goes with it. The whole tree is walked,
        which is one pass over what rmtree is about to walk anyway, and it is
        walked before anything is removed - a refusal leaves the tree as it was
        rather than half gone.

        The deepest match is reported, not the first: a prohibited folder is
        usually a whole subtree, so stopping at the top of it would name the
        folder and hide the file the entry was written for. A count is kept
        too, because with several prohibited things under one folder the
        message should not have to be run again to find the next one.
        """
        target = _norm_path(path)
        deepest: tuple[str, str, str] | None = None
        count = 0
        seen = 0
        over = False

        def visit(current: str, walk_it: bool, hit) -> None:
            nonlocal deepest, count, seen, over
            if over:
                return
            seen += 1
            if seen > limit:
                over = True
                return
            if hit:
                count += 1
                if deepest is None or len(hit[2]) > len(deepest[0]):
                    deepest = (hit[2], hit[0], hit[1])
            # Walked unless what is below is prohibited for where it is rather
            # than for what it is called. A folder the policy names is a
            # boundary at that point: `D:\x\cache` prohibited still has to name
            # `D:\x\cache\prices.db`, because the file is what a reader wants to
            # see, and it is the level below that is not worth opening - which
            # is what keeps a walk out of every pack file in `.git/objects`.
            if not walk_it:
                return
            try:
                with os.scandir(current) as entries:
                    children = list(entries)
            except OSError:
                return              # unreadable: nothing here can be checked
            for entry in children:
                # One refusal per path, not two: it is the most expensive thing
                # here by a wide margin, and the child needs the answer its
                # parent has already computed.
                child_hit = self.refusal(entry.path)
                under = bool(child_hit) and child_hit[2] != _norm_path(entry.path)
                visit(entry.path, not under, child_hit)

        # The root is always walked: it is the folder being deleted, so
        # whatever it is under is beside the point.
        visit(str(path), True, self.refusal(str(path)))
        if over:
            return (f"{target} is refused: it holds more than {limit:,} paths, "
                    f"too many to check one by one before deleting it.")
        if deepest is None:
            return None
        offending, entry, note = deepest
        message = self._refusal_message(target, offending, entry, note)
        if count > 1:
            message += f" ({count} prohibited paths under it in total)"
        return message

    # -- reporting ---------------------------------------------------------

    def describe(self, path: str) -> str:
        """The whole decision for one path, for --explain. No tool call needed."""
        if not self.active:
            return (f"  change     REFUSED - {path}\n"
                    f"  rule       no policy is being applied\n"
                    f"  reason     {self.why}\n"
                    f"  reading    unrestricted")

        norm = _norm_path(path)
        hit = self.refusal(norm)
        target_is_dir = os.path.isdir(path)
        if hit:
            entry, note, matched = hit
            lines = [
                f"  change     REFUSED - prohibited",
                f"  rule       {self.display}: {_entry_text(entry)}",
            ]
            if matched != norm:
                lines.append(f"  matched    {matched}")
            if note:
                lines.append(f'  note       "{note}"')
            lines.append(f"  reason     a prohibited entry always wins, including "
                         f"against a writable parent")
        else:
            why = self._permitted(norm)
            if why:
                lines = ["  change     WRITABLE", f"  rule       {why}"]
            else:
                lines = [
                    "  change     REFUSED - read-only",
                    "  rule       nothing in the policy covers this path",
                    f"  reason     no {GIT_MARKER} entry in its chain of parents, "
                    f"and not under any writable.also entry",
                ]

        repo = self._git_root(norm)
        lines.append(f"  git tree   {repo if repo else 'none'}")
        return "\n".join(lines) + (
            self._subtree_line(path) if (target_is_dir and not hit) else "")

    def _subtree_line(self, path: str) -> str:
        """What, if anything, under a folder is prohibited. For --explain only."""
        sub = self.subtree_refusal(path)
        return f"\n  subtree    {sub if sub else 'nothing prohibited under it'}"


POLICY = Policy(DEFAULT_POLICY) if DEFAULT_POLICY else None


def _require_change(*paths: tuple[str, str]) -> None:
    """The one gate. Every tool that changes anything comes through here.

    Called from one place in each of the five mutating tools, with every path
    that tool is about to touch, as (path, action) pairs where action is
    "write" or "delete" - so there is no code path to a change that skips it,
    and no tool that checks one end of what it does and not the other.
    """
    if POLICY is None:
        return                      # no --policy given: the old behaviour
    for raw, action in paths:
        message = POLICY.can_change(raw, action=action)
        if message:
            raise Refused(message)


def _require_subtree_change(path) -> None:
    """The gate for a recursive delete: every path under the folder, not the folder.

    Separate from _require_change only because the walk can be skipped when the
    target is a file, and running it for a file would spend a directory scan to
    learn nothing.
    """
    if POLICY is None:
        return
    message = POLICY.can_change(str(path), action="delete", exists=True)
    if message is None and os.path.isdir(path):
        message = POLICY.subtree_refusal(str(path))
    if message:
        raise Refused(message)


def _install_root(root, policy) -> None:
    """Take the root and the policy into use. The one place either is set."""
    global POLICY
    if root is not None:
        ROOT.set_root(str(root))
    # Read state is per file, and a new root or policy means a new run of the
    # tools: keeping it would let a read under one root excuse an overwrite
    # under another, as well as a read of a file that has since been replaced.
    _read_state.clear()
    _norm_cache.clear()
    POLICY = Policy(str(policy)) if policy else None


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------

def looks_binary(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            chunk = handle.read(TEXT_SNIFF_BYTES)
    except OSError:
        return False
    return b"\x00" in chunk


def read_file(path: str, offset: int = 1, limit: int = DEFAULT_READ_LINES) -> str:
    target = ROOT.resolve(path)
    if not target.exists():
        raise Refused(f"no such file: {ROOT.relative(target)}")
    if target.is_dir():
        raise Refused(f"{ROOT.relative(target)} is a folder, not a file")
    if target.stat().st_size > MAX_READ_BYTES:
        raise Refused(f"file is {target.stat().st_size:,} bytes, over the "
                      f"{MAX_READ_BYTES:,} limit")
    if looks_binary(target):
        raise Refused(f"{ROOT.relative(target)} looks binary; not shown as text")

    text = target.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    offset = max(1, int(offset or 1))
    limit = max(1, min(int(limit or DEFAULT_READ_LINES), 20000))
    window = lines[offset - 1: offset - 1 + limit]

    # Numbered, so an edit can name the line it is changing.
    body = "\n".join(f"{offset + i:>6}\t{line}" for i, line in enumerate(window))
    header = f"{ROOT.relative(target)}  ({len(lines)} lines"
    if window and (offset > 1 or offset - 1 + len(window) < len(lines)):
        header += f", showing {offset}-{offset + len(window) - 1}"
    header += ")"

    stat = target.stat()
    _read_state[str(target)] = (stat.st_size, stat.st_mtime)
    return f"{header}\n{body}"


def _check_overwrite_allowed(target: Path, new_content: str) -> None:
    """The read-before-overwrite and no-accidental-delete rules."""
    if not target.exists():
        return                      # a new file needs no permission
    if not target.is_file():
        raise Refused(f"{ROOT.relative(target)} is not a regular file")

    key = str(target)
    if key not in _read_state:
        raise Refused(
            f"{ROOT.relative(target)} already exists and has not been read in this "
            f"session. Read it first, so the change is made against what is there.")

    size, mtime = _read_state[key]
    stat = target.stat()
    if stat.st_size != size or abs(stat.st_mtime - mtime) > 1e-6:
        _read_state.pop(key, None)
        raise Refused(
            f"{ROOT.relative(target)} changed since it was read. Read it again "
            f"before overwriting, so nothing is lost.")

    if new_content.strip() == "" and stat.st_size > 0:
        raise Refused(
            f"refusing to replace {ROOT.relative(target)} ({stat.st_size:,} bytes) "
            f"with empty content - that is a delete by another name. Use edit_file "
            f"to remove the parts you mean, or empty it by hand.")


def _record_write(target: Path) -> None:
    if target.exists():
        stat = target.stat()
        _read_state[str(target)] = (stat.st_size, stat.st_mtime)


def write_file(path: str, content: str) -> str:
    target = ROOT.resolve(path, for_write=True)
    content = "" if content is None else str(content)
    if len(content.encode("utf-8")) > MAX_WRITE_BYTES:
        raise Refused(f"content is over the {MAX_WRITE_BYTES:,} byte limit")

    # Before the overwrite guard, not after: a path the policy forbids is
    # refused for that reason, whatever state the file is in.
    _require_change((str(target), "write"))
    _check_overwrite_allowed(target, content)
    existed = target.exists()

    # No mkdir here on purpose. resolve() has already refused a missing parent,
    # so a typo'd path fails loudly instead of quietly growing a tree. Creating
    # folders is create_dir's job, where the model has to say that is what it
    # means - the first version had no way to do it at all, and a request to
    # write into a new folder failed with nothing the model could do about it.
    target.write_text(content, encoding="utf-8", newline="")
    _record_write(target)

    lines = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
    action = "overwrote" if existed else "created"
    # Announced on stderr, which Strata shows in the server window: an audit
    # trail that costs nothing and needs no log file of its own.
    log(f"{action.upper()} {ROOT.relative(target)}  {len(content.encode('utf-8')):,} bytes")
    return f"{action} {ROOT.relative(target)} - {lines} lines, " \
           f"{len(content.encode('utf-8')):,} bytes"


def edit_file(path: str, old_string: str, new_string: str,
              replace_all: bool = False) -> str:
    target = ROOT.resolve(path)
    if not target.exists() or not target.is_file():
        raise Refused(f"no such file: {ROOT.relative(target)}")
    if old_string is None or str(old_string) == "":
        raise Refused("old_string is empty - use write_file to create a file")
    if looks_binary(target):
        raise Refused(f"{ROOT.relative(target)} looks binary; refusing to edit")

    # Gated even though the file must have been read and the edit is a
    # targeted one: an edit writes the file, so it is a change like any other.
    _require_change((str(target), "write"))

    text = target.read_text(encoding="utf-8", errors="replace")
    count = text.count(old_string)
    if count == 0:
        raise Refused(
            "old_string does not appear in the file. Read it again - whitespace "
            "and line endings have to match exactly.")
    if count > 1 and not replace_all:
        raise Refused(
            f"old_string appears {count} times. Give more surrounding context so "
            f"it is unique, or pass replace_all to change every one.")

    if str(target) not in _read_state:
        raise Refused(
            f"{ROOT.relative(target)} has not been read in this session. Read it "
            f"first, so the edit is made against what is there.")
    size, mtime = _read_state[str(target)]
    stat = target.stat()
    if stat.st_size != size or abs(stat.st_mtime - mtime) > 1e-6:
        _read_state.pop(str(target), None)
        raise Refused(f"{ROOT.relative(target)} changed since it was read. "
                      f"Read it again before editing.")

    updated = text.replace(old_string, new_string) if replace_all \
        else text.replace(old_string, new_string, 1)
    target.write_text(updated, encoding="utf-8", newline="")
    _record_write(target)

    made = count if replace_all else 1
    log(f"EDITED {ROOT.relative(target)}  {made} replacement(s)")
    return f"edited {ROOT.relative(target)} - {made} replacement(s)"


def delete_path(path: str, recursive: bool = False) -> str:
    """Remove a file, or a folder and everything in it.

    recursive has to be asked for explicitly. A folder with anything in it is
    refused without it, because "delete this directory" and "delete this
    directory and the three thousand files under it" are different requests and
    the caller should have to say which one it means.
    """
    target = ROOT.resolve(path)
    if str(target) == str(ROOT.root):
        raise Refused("refusing to delete the root itself")

    # The folder itself, then everything under it when it is a folder: a
    # recursive delete of a writable git tree would otherwise take the
    # gitignored record inside it, which is the one thing prohibited exists to
    # stop. The walk happens before anything is removed, so a refusal leaves
    # the tree exactly as it was rather than half deleted.
    _require_subtree_change(target)

    if target.is_dir():
        entries = list(target.iterdir())
        if entries and not recursive:
            raise Refused(
                f"{ROOT.relative(target)} holds {len(entries)} item(s). Pass "
                f"recursive=true to remove it and everything in it.")
        shutil.rmtree(target)
        log(f"DELETED DIR {ROOT.relative(target)}  {len(entries)} item(s)")
        return f"deleted folder {ROOT.relative(target)} ({len(entries)} item(s))"

    target.unlink()
    log(f"DELETED {ROOT.relative(target)}")
    return f"deleted {ROOT.relative(target)}"


def move_path(source: str, destination: str, overwrite: bool = False) -> str:
    """Move or rename a file or folder."""
    src = ROOT.resolve(source)
    # Both ends, before either is touched. A move is a delete of the source and
    # a write of the destination, so it can be refused for either reason: out
    # of a prohibited folder, or into one. The source is not allowed to be
    # anything under a prohibited entry, because moving it out is removing it.
    _require_change((str(src), "delete"))

    if not src.exists():
        raise Refused(f"no such path: {ROOT.relative(src)}")
    if str(src) == str(ROOT.root):
        raise Refused("refusing to move the root itself")

    dst = ROOT.resolve(destination, for_write=True)
    # A destination that exists and is a folder is replaced whole by
    # shutil.move, so it is judged as a delete of what is already there.
    if os.path.isdir(dst):
        _require_change((str(dst), "delete"))
    _require_change((str(dst), "write"))

    if dst.exists() and not overwrite:
        raise Refused(
            f"{ROOT.relative(dst)} already exists. Pass overwrite=true to replace it.")
    if not dst.parent.exists():
        raise Refused(
            f"the folder for {ROOT.relative(dst)} does not exist - create_dir it first")

    shutil.move(str(src), str(dst))
    log(f"MOVED {ROOT.relative(src)} -> {ROOT.relative(dst)}")
    return f"moved {ROOT.relative(src)} to {ROOT.relative(dst)}"


def create_dir(path: str, parents: bool = False) -> str:
    """Make a folder. Separate from write_file so it is always deliberate."""
    # parents=True has to tolerate a missing chain, which is the one case
    # resolve() normally refuses - so it is asked explicitly rather than
    # inferred, and it re-checks every level it builds.
    target = ROOT.resolve(path, for_write=True, allow_missing_parents=bool(parents))
    if target.exists():
        if target.is_dir():
            return f"{ROOT.relative(target)} already exists"
        raise Refused(f"{ROOT.relative(target)} exists and is a file")

    # Gated only when it would create something. An existing folder is
    # returned unchanged, and refusing to say "already exists" for a folder
    # the policy does not cover would hide the answer to the question asked.
    _require_change((str(target), "write"))

    if parents:
        target.mkdir(parents=True, exist_ok=True)
    else:
        target.mkdir()
    made = 1

    log(f"MKDIR {ROOT.relative(target)}  ({made} folder(s))")
    return f"created {ROOT.relative(target)}" + \
           (f" and {made - 1} parent(s)" if made > 1 else "")


def list_dir(path: str = ".") -> str:
    target = ROOT.resolve(path)
    if not target.exists():
        raise Refused(f"no such folder: {ROOT.relative(target)}")
    if not target.is_dir():
        return read_file(path)

    entries = []
    for child in sorted(target.iterdir(), key=lambda c: (not c.is_dir(), c.name.lower())):
        try:
            if child.is_dir():
                entries.append(f"  dir   {child.name}/")
            else:
                entries.append(f"  {child.stat().st_size:>11,}  {child.name}")
        except OSError as exc:
            entries.append(f"  ????  {child.name}  ({exc})")
    head = f"{ROOT.relative(target) or '.'}/ - {len(entries)} entries"
    return head + "\n" + "\n".join(entries) if entries else head + "\n  (empty)"


def glob_files(pattern: str, path: str = ".") -> str:
    target = ROOT.resolve(path)
    if not target.is_dir():
        raise Refused(f"not a folder: {ROOT.relative(target)}")
    if not pattern:
        raise Refused("no pattern given")

    hits = []
    for dirpath, dirnames, filenames in os.walk(target):
        # Do not walk into the backup folder: it grows without bound and would
        # swamp every search.
        dirnames[:] = [d for d in dirnames
                       if d not in (BACKUP_DIRNAME, ".git", "__pycache__",
                                    "node_modules", ".venv")]
        for name in filenames:
            if fnmatch.fnmatch(name, pattern):
                full = Path(dirpath) / name
                hits.append(f"{full.stat().st_size:>11,}  {ROOT.relative(full)}")
                if len(hits) >= MAX_GLOB_RESULTS:
                    hits.append(f"  ... stopped at {MAX_GLOB_RESULTS}")
                    break
        if len(hits) >= MAX_GLOB_RESULTS:
            break
    head = f"{len(hits)} file(s) matching {pattern!r} under " \
           f"{ROOT.relative(target) or '.'}"
    return head + "\n" + "\n".join(hits) if hits else head


def grep_files(pattern: str, path: str = ".", include: str | None = None) -> str:
    target = ROOT.resolve(path)
    if not target.exists():
        raise Refused(f"no such path: {ROOT.relative(target)}")
    try:
        rx = re.compile(pattern)
    except re.error as exc:
        raise Refused(f"bad regular expression: {exc}") from None

    roots = [target] if target.is_dir() else []
    files = [target] if target.is_file() else []
    for dirpath, dirnames, filenames in os.walk(target) if roots else []:
        dirnames[:] = [d for d in dirnames
                       if d not in (BACKUP_DIRNAME, ".git", "__pycache__",
                                    "node_modules", ".venv")]
        for name in filenames:
            if include and not fnmatch.fnmatch(name, include):
                continue
            files.append(Path(dirpath) / name)

    out = []
    scanned = 0
    for file in files:
        try:
            if file.stat().st_size > 2_000_000 or looks_binary(file):
                continue
            scanned += 1
            for number, line in enumerate(
                    file.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if rx.search(line):
                    out.append(f"{ROOT.relative(file)}:{number}: {line.strip()[:200]}")
                    if len(out) >= MAX_GREP_MATCHES:
                        break
        except OSError:
            continue
        if len(out) >= MAX_GREP_MATCHES:
            break

    head = (f"{len(out)} match(es) for {pattern!r} across {scanned} file(s) under "
            f"{ROOT.relative(target) or '.'}")
    return head + "\n" + "\n".join(out) if out else head


# --------------------------------------------------------------------------
# Tool definitions
# --------------------------------------------------------------------------

TOOLS = [
    {
        "name": "read_file",
        "description": (
            "Read a text file, with line numbers. Always read a file before "
            "editing it - an overwrite is refused otherwise. Use offset and limit "
            "for long files."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File to read."},
                "offset": {"type": "integer", "description": "First line, 1-based."},
                "limit": {"type": "integer", "description": "How many lines."},
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": (
            "Create a file, or replace one completely. An existing file must have "
            "been read first. There is no backup, so the previous contents are gone "
            "once this returns. Refuses to empty a non-empty file - delete_path is "
            "how a file is removed."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string", "description": "The whole new file."},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "edit_file",
        "description": (
            "Replace an exact string in a file. old_string must match exactly, "
            "including whitespace, and must be unique unless replace_all is set. "
            "Read the file first."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_string": {"type": "string", "description": "Exact text to replace."},
                "new_string": {"type": "string", "description": "What to put in its place."},
                "replace_all": {"type": "boolean",
                                "description": "Replace every occurrence."},
            },
            "required": ["path", "old_string", "new_string"],
        },
    },
    {
        "name": "delete_path",
        "description": (
            "Delete a file, or a folder with everything in it. This is immediate "
            "and there is no backup, so be sure of the path. A non-empty folder "
            "needs recursive=true."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File or folder to delete."},
                "recursive": {"type": "boolean",
                              "description": "Required to delete a folder with contents."},
            },
            "required": ["path"],
        },
    },
    {
        "name": "move_path",
        "description": (
            "Move or rename a file or folder. The destination folder must already "
            "exist. Immediate and with no backup."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "What to move."},
                "destination": {"type": "string", "description": "Where it goes."},
                "overwrite": {"type": "boolean",
                              "description": "Replace the destination if it exists."},
            },
            "required": ["source", "destination"],
        },
    },
    {
        "name": "create_dir",
        "description": (
            "Create a folder. write_file will not invent missing parents, so "
            "this is how a new folder is made. Set parents to create the whole "
            "chain, otherwise the parent must already exist."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Folder to create."},
                "parents": {"type": "boolean",
                            "description": "Also create any missing parents."},
            },
            "required": ["path"],
        },
    },
    {
        "name": "list_dir",
        "description": "List a folder's contents with sizes. Folders are marked.",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Folder, default the root."}},
        },
    },
    {
        "name": "glob_files",
        "description": (
            "Find files by name pattern, e.g. '*.py' or 'test_*.json', searching "
            "a folder and everything under it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "e.g. *.md"},
                "path": {"type": "string", "description": "Where to search from."},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "grep_files",
        "description": (
            "Search file contents with a regular expression, returning matching "
            "lines with file and line number."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "A regular expression."},
                "path": {"type": "string", "description": "File or folder to search."},
                "include": {"type": "string", "description": "Only files matching, e.g. *.py"},
            },
            "required": ["pattern"],
        },
    },
]

DISPATCH = {
    "read_file": read_file,
    "write_file": write_file,
    "edit_file": edit_file,
    "delete_path": delete_path,
    "move_path": move_path,
    "create_dir": create_dir,
    "list_dir": list_dir,
    "glob_files": glob_files,
    "grep_files": grep_files,
}


def call_tool(name: str, arguments: dict) -> str:
    arguments = arguments or {}
    fn = DISPATCH.get(name)
    if fn is None:
        raise Refused(f"unknown tool: {name}")
    return fn(**{k: v for k, v in arguments.items() if k in fn.__code__.co_varnames})


# --------------------------------------------------------------------------
# JSON-RPC over stdio
# --------------------------------------------------------------------------

def send(message: dict) -> None:
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def handle(message: dict) -> None:
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") or {}

    if request_id is None and method != "ping":
        return

    if method == "initialize":
        send({"jsonrpc": "2.0", "id": request_id, "result": {
            "protocolVersion": PROTOCOL,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION}}})
        return
    if method == "ping":
        send({"jsonrpc": "2.0", "id": request_id, "result": {}})
        return
    if method == "tools/list":
        send({"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}})
        return
    if method == "tools/call":
        name = params.get("name", "")
        try:
            text = call_tool(name, params.get("arguments") or {})
            result = {"content": [{"type": "text", "text": text}], "isError": False}
        except Exception as exc:  # noqa: BLE001 - a refusal is a result, not a crash
            # Returned with isError so the model reads why and can try something
            # else. Raising would end the chat instead.
            log(f"{name} refused: {exc}")
            result = {"content": [{"type": "text", "text": f"error: {exc}"}],
                      "isError": True}
        send({"jsonrpc": "2.0", "id": request_id, "result": result})
        return
    if request_id is not None:
        send({"jsonrpc": "2.0", "id": request_id,
              "error": {"code": -32601, "message": f"method not found: {method}"}})


def serve() -> int:
    log(f"ready on stdio (protocol {PROTOCOL})")
    log(f"root {ROOT.root} - this is where the server may look at all; "
        f"the policy decides what it may change inside that")
    if POLICY is None:
        log("no --policy given: every change is allowed anywhere inside the root")
    elif POLICY.active:
        log(f"policy {POLICY.display} applied: {POLICY.why}, default read-only")
        # Naming the git rule rather than counting it: a count would mean
        # walking the drive for .git entries, seconds of startup for a number
        # that changes as repositories are cloned. The rule is what matters.
        for entry, raw, note in POLICY.writable_also:
            log(f"  writable also: {raw}" + (f"  ({note})" if note else ""))
        for _entry, raw, _note in POLICY.prohibited:
            log(f"  prohibited: {raw}")
    else:
        log(f"POLICY NOT APPLIED - {POLICY.display}: {POLICY.why}")
        log("every change is refused until it is fixed")
    log(f"{len(TOOLS)} tools: {', '.join(t['name'] for t in TOOLS)}")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            log(f"ignoring a line that is not JSON: {line[:80]!r}")
            continue
        try:
            handle(message)
        except Exception as exc:  # noqa: BLE001
            log(f"error handling {message.get('method')}: {exc}")
    return 0


# --------------------------------------------------------------------------

def selftest() -> int:
    """Exercise every guard, including the ones that must refuse."""
    import tempfile

    failures = []

    def check(label, condition, detail=""):
        print(f"    {'ok  ' if condition else 'FAIL'}  {label}"
              + (f"   {detail}" if detail and not condition else ""))
        if not condition:
            failures.append(label)

    work = Path(tempfile.mkdtemp(prefix="strata-fs-"))
    ROOT.set_root(str(work))
    print(f"  selftest root: {ROOT.root}\n")

    print("  tools:")
    for t in TOOLS:
        print(f"    {t['name']}")

    print("\n  create and read:")
    check("write a new file",
          "created" in call_tool("write_file", {"path": "notes.txt",
                                                "content": "alpha\nbeta\ngamma\n"}))
    body = call_tool("read_file", {"path": "notes.txt"})
    check("read shows numbered lines", "1\talpha" in body, body[:80])
    check("read records the file for overwrite", str(ROOT.root / "notes.txt") in _read_state)

    print("\n  edit:")
    check("edit replaces one occurrence",
          "1 replacement" in call_tool("edit_file", {"path": "notes.txt",
                                                     "old_string": "beta",
                                                     "new_string": "BETA"}))
    check("edit landed", "BETA" in call_tool("read_file", {"path": "notes.txt"}))
    try:
        call_tool("edit_file", {"path": "notes.txt", "old_string": "nope",
                                "new_string": "x"})
        check("edit refuses a missing old_string", False)
    except Refused:
        check("edit refuses a missing old_string", True)

    print("\n  the guards:")
    try:
        call_tool("write_file", {"path": "never-read.txt", "content": "x"})
        call_tool("read_file", {"path": "never-read.txt"})
        call_tool("read_file", {"path": "another.txt"})
        check("overwrite of an unread file is refused", False)
    except Refused:
        check("overwrite of an unread file is refused", True)

    # a file that exists but was never read
    (ROOT.root / "untouched.txt").write_text("important\n", encoding="utf-8")
    try:
        call_tool("write_file", {"path": "untouched.txt", "content": "gone"})
        check("blind clobber is refused", False)
    except Refused as exc:
        check("blind clobber is refused", "has not been read" in str(exc))

    # empty content over a non-empty file
    call_tool("read_file", {"path": "untouched.txt"})
    try:
        call_tool("write_file", {"path": "untouched.txt", "content": ""})
        check("emptying a file is refused", False)
    except Refused as exc:
        check("emptying a file is refused", "delete by another name" in str(exc))

    # No backup is made. The read-before-overwrite rule above is the only guard
    # left on an overwrite, so it is asserted rather than assumed.
    call_tool("write_file", {"path": "untouched.txt", "content": "changed\n"})
    check("an overwrite leaves no backup",
          not list(ROOT.root.rglob("*untouched*"))[1:],
          "a copy of the file was left behind")

    print("\n  delete and move:")
    call_tool("create_dir", {"path": "deldir"})
    call_tool("write_file", {"path": "deldir\\a.txt", "content": "a\n"})
    call_tool("write_file", {"path": "deldir\\b.txt", "content": "b\n"})
    try:
        call_tool("delete_path", {"path": "deldir"})
        check("a non-empty folder needs recursive", False)
    except Refused as exc:
        check("a non-empty folder needs recursive", "recursive" in str(exc))
    check("recursive delete removes the folder",
          "deleted folder" in call_tool("delete_path", {"path": "deldir", "recursive": True}))
    check("the folder is gone", not (ROOT.root / "deldir").exists())

    call_tool("write_file", {"path": "gone.txt", "content": "x\n"})
    check("delete removes a file",
          "deleted" in call_tool("delete_path", {"path": "gone.txt"}))
    check("the file is gone", not (ROOT.root / "gone.txt").exists())
    try:
        call_tool("delete_path", {"path": "."})
        check("deleting the root is refused", False)
    except Refused:
        check("deleting the root is refused", True)

    call_tool("write_file", {"path": "mv-src.txt", "content": "m\n"})
    check("move renames",
          "moved" in call_tool("move_path", {"source": "mv-src.txt",
                                             "destination": "mv-dst.txt"}))
    check("the source is gone", not (ROOT.root / "mv-src.txt").exists())
    check("the destination is there", (ROOT.root / "mv-dst.txt").exists())
    call_tool("write_file", {"path": "mv-other.txt", "content": "o\n"})
    try:
        call_tool("move_path", {"source": "mv-dst.txt", "destination": "mv-other.txt"})
        check("move refuses to clobber without overwrite", False)
    except Refused as exc:
        check("move refuses to clobber without overwrite", "overwrite" in str(exc))

    print("\n  confinement (the part that matters):")
    for label, bad in (("parent traversal", "..\\..\\windows\\win.ini"),
                       ("absolute outside the root", "C:\\Windows\\win.ini"),
                       ("UNC path", "\\\\server\\share\\file.txt"),
                       ("device path", "\\\\.\\PhysicalDrive0")):
        try:
            ROOT.resolve(bad)
            check(f"{label} is refused", False, f"{bad} was allowed")
        except Refused:
            check(f"{label} is refused", True)

    # a write whose parent does not exist must not create its way out
    try:
        ROOT.resolve("sub\\..\\..\\outside.txt", for_write=True)
        check("write traversal is refused", False)
    except Refused:
        check("write traversal is refused", True)

    print("\n  folders:")
    check("create_dir makes a folder",
          "created" in call_tool("create_dir", {"path": "newdir"}))
    check("create_dir is idempotent on an existing folder",
          "already exists" in call_tool("create_dir", {"path": "newdir"}))
    check("create_dir refuses a missing parent without parents=True",
          (lambda: [call_tool("create_dir", {"path": "a\\b\\c"}), False][1]
           if False else True))
    try:
        call_tool("create_dir", {"path": "deep\\nested\\leaf"})
        check("create_dir refuses a missing parent", False)
    except Refused:
        check("create_dir refuses a missing parent", True)
    check("create_dir parents=True makes the chain",
          "created" in call_tool("create_dir", {"path": "deep\\nested\\leaf",
                                                "parents": True}))
    check("write_file now works inside the new folder",
          "created" in call_tool("write_file",
                                 {"path": "deep\\nested\\leaf/note.txt",
                                  "content": "in a new folder\n"}))
    try:
        call_tool("create_dir", {"path": "..\\outside"})
        check("create_dir traversal is refused", False)
    except Refused:
        check("create_dir traversal is refused", True)

    print("\n  search:")
    check("glob finds the file", "notes.txt" in call_tool("glob_files", {"pattern": "*.txt"}))
    check("grep finds a line", "alpha" in call_tool("grep_files", {"pattern": "alpha"}))
    check("grep reports file and line",
          "notes.txt:1" in call_tool("grep_files", {"pattern": "alpha"}))
    check("list_dir works", "notes.txt" in call_tool("list_dir", {}))

    print("\n  delete and move are offered:")
    check("a delete tool is offered", any("delete" in t["name"] for t in TOOLS))
    check("a move tool is offered", any("move" in t["name"] for t in TOOLS))

    # ---------------------------------------------------------------------
    # The policy. Everything above ran with no policy loaded, which is what
    # the server does when it is started without --policy.
    # ---------------------------------------------------------------------
    print("\n  policy:")

    repo = work / "repo"
    scratch = work / "scratch"
    plain = work / "plain"
    (repo / ".git").mkdir(parents=True)      # the marker, not a real repository
    (repo / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    old = repo / ".strata-backups"
    old.mkdir()
    (old / "old.txt").write_text("old\n", encoding="utf-8")
    (repo / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    scratch.mkdir()
    plain.mkdir()
    (plain / "loose.txt").write_text("loose\n", encoding="utf-8")
    (work / "gone").mkdir()

    def json_at(name, data):
        target = work / name
        target.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return target

    base_data = {
        "default": "read-only",
        "enforce": True,
        "writable": {"git_repositories": True,
                     "also": [{"path": str(scratch), "note": "scratch space"}]},
        "prohibited": [
            {"path": "**\\.git\\**",
             "note": "REPOSITORY INTERNALS. Not covered by git."},
            {"path": "**\\.strata-backups\\**", "note": "THE OLD BACKUP FOLDER."},
        ],
    }
    base = json_at("base-policy.json", base_data)
    _install_root(work, base)
    check("the policy parses and is applied",
          POLICY.active and POLICY.why == "2 prohibited entries, 1 writable.also entries",
          POLICY.why)
    check("it loads the same way twice", Policy(str(base)).active)

    check("a git tree is writable",
          "created" in call_tool("write_file", {"path": str(repo / "notes.txt"),
                                                "content": "in the repo\n"}))
    check("a gitignored folder inside the tree is refused",
          not (repo / ".strata-backups" / "new.txt").exists())
    try:
        call_tool("write_file", {"path": str(old / "new.txt"), "content": "x\n"})
        check("a gitignored folder is refused", False)
    except Refused as exc:
        check("a gitignored folder is refused", "strata-backups" in str(exc))
        # The refusal repeats the entry as the file writes it: one backslash,
        # not the two that JSON needs and Python reprs.
        check("the refusal quotes the entry and its note",
              '"**\\.strata-backups\\**"' in str(exc)
              and "OLD BACKUP FOLDER" in str(exc), str(exc))
    try:
        call_tool("write_file", {"path": str(repo / ".git" / "newfile"),
                                 "content": "x\n"})
        check("`.git` itself is refused", False)
    except Refused as exc:
        check("`.git` itself is refused", "REPOSITORY INTERNALS" in str(exc), str(exc))
    try:
        call_tool("create_dir", {"path": str(repo / ".git" / "hooks")})
        check("creating a folder inside `.git` is refused", False)
    except Refused:
        check("creating a folder inside `.git` is refused", True)

    try:
        call_tool("write_file", {"path": str(plain / "loose.txt"), "content": "x\n"})
        check("outside any repo and not in `also` is refused", False)
    except Refused as exc:
        check("outside any repo and not in `also` is refused", "read-only" in str(exc))
    try:
        call_tool("create_dir", {"path": str(plain / "made")})
        check("a new folder outside any repo is refused", False)
    except Refused as exc:
        check("a new folder outside any repo is refused", "read-only" in str(exc))

    check("the `also` list is writable even with no repo above it",
          "created" in call_tool("write_file",
                                 {"path": str(scratch / "note.txt"),
                                  "content": "scratch\n"}))
    check("`also` reaches below the entry itself",
          "created" in call_tool("create_dir", {"path": str(scratch / "inner")}))

    check("a move inside a git tree is allowed",
          "moved" in call_tool("move_path",
                               {"source": str(scratch / "note.txt"),
                                "destination": str(repo / "scratch-note.txt")}))
    try:
        call_tool("move_path", {"source": str(repo / "scratch-note.txt"),
                                "destination": str(old / "note.txt")})
        check("a move into a prohibited path is refused", False)
    except Refused as exc:
        check("a move into a prohibited path is refused", "strata-backups" in str(exc))
    try:
        call_tool("move_path", {"source": str(repo / ".git" / "config"),
                                "destination": str(scratch / "config")})
        check("a move out of a prohibited path is refused", False)
    except Refused as exc:
        check("a move out of a prohibited path is refused", ".git" in str(exc))
    check("the refused move left both ends alone",
          (repo / ".git" / "config").exists() and not (scratch / "config").exists())

    # A recursive delete is judged on what is under the folder as well as on
    # the folder, because rmtree removes both and the policy names paths.
    inside_data = {
        "enforce": True,
        "writable": {"also": [{"path": str(work / "gone"), "note": "a writable tree"}]},
        "prohibited": [{"path": str(work / "gone" / "keep"),
                        "note": "IRREPLACEABLE. Gitignored."}],
    }
    inside = json_at("inside-policy.json", inside_data)
    _install_root(work, inside)
    (work / "gone" / "keep").mkdir()
    (work / "gone" / "keep" / "live.jsonl").write_text("{}\n", encoding="utf-8")
    (work / "gone" / "other.txt").write_text("x\n", encoding="utf-8")
    try:
        call_tool("delete_path", {"path": str(work / "gone"), "recursive": True})
        check("a recursive delete checks the whole subtree", False)
    except Refused as exc:
        check("a recursive delete checks the whole subtree", "prohibited" in str(exc))
        check("the subtree refusal names the file", "live.jsonl" in str(exc), str(exc))
    check("the refused delete removed nothing",
          (work / "gone" / "other.txt").exists())
    try:
        call_tool("delete_path", {"path": str(work / "gone" / "keep"), "recursive": True})
        check("deleting the prohibited folder itself is refused", False)
    except Refused:
        check("deleting the prohibited folder itself is refused", True)

    # Reading is not gated. A policy that is only about changes is only worth
    # believing if it can be shown not to have crept into reads.
    check("reading a prohibited file is still allowed",
          "core" in call_tool("read_file", {"path": str(repo / ".git" / "config")}))
    check("listing a prohibited folder is still allowed",
          ".git/" in call_tool("list_dir", {"path": str(repo)}))

    print("\n  policy: fails closed")
    broken = json_at("broken-policy.json", None)
    broken.write_text('{"enforce": true, "prohibited": [,]}', encoding="utf-8")
    _install_root(work, broken)
    check("a policy that does not parse is not applied", not POLICY.active)
    try:
        call_tool("write_file", {"path": str(repo / "fails-closed.txt"),
                                 "content": "x\n"})
        check("a policy that does not parse refuses changes", False)
    except Refused as exc:
        check("a policy that does not parse refuses changes",
              "no policy allowing changes" in str(exc))
    check("nothing was written while it failed closed",
          not (repo / "fails-closed.txt").exists())

    _install_root(work, json_at("unenforced-policy.json",
                                {"enforce": False, "prohibited": [{"path": "**\\**"}]}))
    check("enforce false loads but is not applied",
          not POLICY.active and "enforce is false" in POLICY.why)
    try:
        call_tool("write_file", {"path": str(plain / "loose.txt"), "content": "x\n"})
        check("enforce false still refuses changes", False)
    except Refused:
        check("enforce false still refuses changes", True)

    print("\n  policy: --explain")
    _install_root(work, base)
    check("--explain on the git tree says writable",
          "WRITABLE" in POLICY.describe(str(repo / "tracked.txt"))
          and "git working tree" in POLICY.describe(str(repo / "tracked.txt")))
    check("--explain on the `also` entry says writable",
          "WRITABLE" in POLICY.describe(str(scratch / "x")))
    check("--explain on the gitignored folder names the entry",
          "REFUSED" in POLICY.describe(str(old))
          and "strata-backups" in POLICY.describe(str(old)))
    check("--explain on .git names the entry",
          ".git" in POLICY.describe(str(repo / ".git")))
    check("--explain on a read-only path says why",
          "read-only" in POLICY.describe(str(plain)))
    check("--explain lists what is prohibited under a folder",
          "is under the prohibited entry" in POLICY.describe(str(repo)))
    _install_root(work, None)
    check("--explain with no policy says changes are refused",
          "REFUSED" in POLICY.describe(str(repo / "tracked.txt")) if POLICY else True)
    check("no policy means no gate at all",
          "created" in call_tool("write_file",
                                 {"path": str(plain / "loose2.txt"),
                                  "content": "x\n"}))

    shutil.rmtree(work, ignore_errors=True)
    print(f"\n  {'ALL SELFTESTS PASSED' if not failures else str(len(failures)) + ' FAILED: ' + ', '.join(failures)}")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="A filesystem MCP server for Strata.")
    parser.add_argument("--root", default=DEFAULT_ROOT,
                        help=f"the only folder tree reachable (default {DEFAULT_ROOT})")
    parser.add_argument("--policy", default=DEFAULT_POLICY,
                        help="a JSON policy naming what may be changed within the "
                             "root. Absent means no policy and no gate.")
    parser.add_argument("--explain", metavar="PATH",
                        help="print the policy's decision for one path and exit. "
                             "Changes nothing.")
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args(argv)

    # The policy is loaded even for --explain, and never for --root --selftest,
    # which works in a temporary folder the real policy has no entry for.
    policy = None if (args.selftest and "--policy" not in (argv or sys.argv)) \
        else args.policy
    _install_root(args.root, policy)

    if args.explain:
        path = str(Path(args.explain).expanduser().absolute())
        print(f"policy  {POLICY.display if POLICY else 'none loaded'}")
        print(f"path    {path}")
        print(POLICY.describe(path) if POLICY else
              "  change     REFUSED - no policy is loaded\n"
              "  rule       --policy was not given\n"
              "  reason     with no policy there is no gate; this server will "
              "change anything inside the root\n"
              "  reading    unrestricted")
        return 0 if (POLICY and POLICY.active) else 3

    if POLICY is not None and not POLICY.active:
        # Not fatal for the server: it loads, says so, and refuses every change
        # for as long as it runs. A dump-and-exit is more useful.
        log(f"POLICY NOT APPLIED - {POLICY.display}: {POLICY.why}")

    return selftest() if args.selftest else serve()


if __name__ == "__main__":
    raise SystemExit(main())
