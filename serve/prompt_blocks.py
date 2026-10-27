"""Named prompt blocks (fork): text a request refers to by name instead of sending it again.

A client that sends the same text with every request - a long preamble, a document, a running summary - sends
it again as identical bytes and it is cheap; sent again *slightly differently* it is not: the engine's cache is
keyed by tokens, and one changed character in that text makes every token after it a miss.  A block is stored
once under a name and expanded into the prompt from that name, so every request that refers to it renders
exactly the same tokens.

The store is generic - a name, its text, and Strata's own timestamps.  It knows nothing about what the text
means or who sends it:

    POST /cache              {"key": "doc_ab12", "value": "..."}      store (or replace)
    POST /cache              {"key": "doc_ab12", "delete": true}      remove
    GET  /cache                                                       keys, sizes, times (never values)
    GET  /cache/<key>                                                 the value
    in a message:            "{{cache:doc_ab12}}"

Strata stamps every entry with its own clock; a client never supplies the time.  When the budget is full the
entry used longest ago goes first, and *referencing* a block counts as a use: a long document stored once at
start-up is old by write time but is the thing reused most, so evicting by write time would throw the wrong
blocks away.

The store is memory only: a server restart empties it, and a request that names a block which is not there is
refused (400) rather than sent with the placeholder as literal text.
"""
from __future__ import annotations

import re
import threading
import time

KEY = re.compile(r"[A-Za-z0-9._:+-]{1,200}")
PLACEHOLDER = re.compile(r"\{\{cache:([A-Za-z0-9._:+-]{1,200})\}\}")
MAX_VALUE_BYTES = 4 << 20            # one block: 4 MiB of text
DEFAULT_MAX_BYTES = 8 << 20          # the whole store: 8 MiB
DEFAULT_MAX_ENTRIES = 4096


class BlockError(ValueError):
    """A block the request names but cannot have: a bad name or value, or one that was never stored."""


def check_key(key) -> str:
    if not isinstance(key, str) or not KEY.fullmatch(key):
        raise BlockError('key: 1 to 200 characters from letters, digits and . _ : + - (e.g. "doc_ab12")')
    return key


class PromptBlocks:
    def __init__(self, max_bytes: int = DEFAULT_MAX_BYTES, max_entries: int = DEFAULT_MAX_ENTRIES):
        self.max_bytes, self.max_entries = max_bytes, max_entries
        self.evictions = 0
        self._lock = threading.Lock()
        self._entries: dict[str, dict] = {}
        self._bytes = 0
        self._seq = 0                    # order of use, counted: the clock's ticks can tie, this cannot

    @classmethod
    def from_config(cls, obj) -> "PromptBlocks":
        """The config's "prompt_blocks": {"max_mib": 8, "max_entries": 4096} (either may be left out)."""
        if obj is None:
            return cls()
        if not isinstance(obj, dict) or set(obj) - {"max_mib", "max_entries"}:
            raise ValueError('prompt_blocks: {"max_mib": 8, "max_entries": 4096}')
        mib, entries = obj.get("max_mib", DEFAULT_MAX_BYTES >> 20), obj.get("max_entries", DEFAULT_MAX_ENTRIES)
        for name, v, low in (("max_mib", mib, 0), ("max_entries", entries, 1)):
            if isinstance(v, bool) or not isinstance(v, (int, float)) or v < low:
                raise ValueError(f"prompt_blocks.{name}: a number ({low} or more)")
        return cls(int(float(mib) * (1 << 20)), int(entries))

    # -- the store ------------------------------------------------------------------------------
    def put(self, key, text) -> dict:
        key = check_key(key)
        if not isinstance(text, str) or not text:
            raise BlockError("value: the block's text")
        size = len(text.encode("utf-8"))
        if size > MAX_VALUE_BYTES:
            raise BlockError(f"value: {size} bytes; one block may hold {MAX_VALUE_BYTES >> 20} MiB")
        if size > self.max_bytes:
            raise BlockError(f"value: {size} bytes, larger than the whole budget (prompt_blocks.max_mib)")
        now = time.time()
        with self._lock:
            old = self._entries.pop(key, None)
            if old is not None:
                self._bytes -= old["bytes"]
            self._seq += 1
            self._entries[key] = {"text": text, "bytes": size, "written": now, "used": now, "uses": 0,
                                  "seq": self._seq}
            self._bytes += size
            self._evict(keep=key)
            return self._view_one(key)

    def get(self, key, touch: bool = True):
        """The block's text, or None.  A reference touches it: that is what keeps a long-lived block in use."""
        key = check_key(key)
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            if touch:
                self._seq += 1
                entry.update(used=time.time(), uses=entry["uses"] + 1, seq=self._seq)
            return entry["text"]

    def entry(self, key):
        """The value with its metadata (GET /cache/<key>), or None."""
        with self._lock:
            entry = self._entries.get(check_key(key))
            return None if entry is None else {**self._view_one(key), "value": entry["text"]}

    def remove(self, key) -> bool:
        key = check_key(key)
        with self._lock:
            if key not in self._entries:
                return False
            self._drop(key)
            return True

    def view(self, limit: int = 64) -> dict:
        """What /metrics and GET /cache show: sizes and times, never the text."""
        with self._lock:
            newest = sorted(self._entries.items(), key=lambda kv: kv[1]["seq"], reverse=True)[:limit]
            return {"entries": [self._view_one(k) for k, _ in newest], "count": len(self._entries),
                    "bytes": self._bytes, "max_bytes": self.max_bytes, "max_entries": self.max_entries,
                    "evictions": self.evictions}

    def _view_one(self, key) -> dict:
        entry = self._entries[key]
        return {"key": key, "bytes": entry["bytes"], "written": round(entry["written"], 3),
                "used": round(entry["used"], 3), "uses": entry["uses"]}

    def _drop(self, key) -> None:
        self._bytes -= self._entries.pop(key)["bytes"]

    def _evict(self, keep: str) -> None:
        while self._entries and (len(self._entries) > self.max_entries or self._bytes > self.max_bytes):
            victim = None
            for k, e in self._entries.items():
                if k == keep:
                    continue
                if victim is None or e["seq"] < self._entries[victim]["seq"]:
                    victim = k
            if victim is None:
                return
            self._drop(victim)
            self.evictions += 1

    # -- the prompt -----------------------------------------------------------------------------
    def expand(self, messages) -> list:
        """Replace every {{cache:key}} in `messages` with its text, in place.  Returns the blocks it
        expanded as (key, message index, characters into that message's text where the block ends),
        in prompt order: the server can pin a shared prefix there.  A block that is not stored
        raises BlockError (a 400): the placeholder as literal text is worse than a refusal.
        A message whose content is a list (images) is expanded too, but gives no position: only a
        string message can be cut at a character count."""
        hits = []
        for i, m in enumerate(messages):
            if not isinstance(m, dict):
                continue
            content = m.get("content")
            if isinstance(content, str):
                m["content"], found = self._expand_text(content, i)
                hits.extend(found)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        part["text"], _ = self._expand_text(part["text"], None)
        return hits

    def _expand_text(self, text: str, index):
        if "{{cache:" not in text:
            return text, []
        out, last, n, hits = [], 0, 0, []
        for match in PLACEHOLDER.finditer(text):
            piece = text[last:match.start()]
            key = match.group(1)
            value = self.get(key)
            if value is None:
                raise BlockError(f"{{{{cache:{key}}}}}: no cache block named {key!r} is stored "
                                 f"(POST /cache {{\"key\": {key!r}, \"value\": ...}} first)")
            out += [piece, value]
            n += len(piece) + len(value)
            if index is not None:
                hits.append((key, index, n))
            last = match.end()
        out.append(text[last:])
        return "".join(out), hits
