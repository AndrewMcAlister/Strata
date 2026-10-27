# Fork notes and change plan

This fork tracks `Niko1221/Strata`. The rule is to stay as close to upstream as possible:
**upstream files are not edited for documentation**, and every change here is either a
new file or a small, deliberate edit to `serve/server.py` / `setup.py`.

Kept here so the work can be re-implemented on a newer upstream, or sent upstream, without
carrying a diverging branch forever.

| | |
|---|---|
| Upstream | `Niko1221/Strata`, `upstream/main` = `4b9e05a2` (25 Oct 2026) |
| Fork | `AndrewMcAlister/Strata`, `origin/develop` |
| Last merge | `c16a44f2` (27 Oct 2026), closing a 2,337-commit gap |
| Working branch | `develop` only. `main` mirrors `upstream/main` and is unused. |

## What this fork adds

| Change | Files | Upstream edits |
|---|---|---|
| Start/stop wrappers | `Scripts/Strata_Start.bat`, `Strata_Stop.bat`, `Strata_DefaultModel.ps1`, `tools/test_start_script.py` | none |
| Windows service | `Scripts/Strata_Service.py`, `Scripts/Strata_Service.ps1`, `tools/test_service.py` | none |
| Pre-load model check | `tools/native_check.py`, `tools/gguf_inventory.py`, `tools/test_native_check.py` | `serve/server.py` hook |
| Refusal diagnostics | — | `serve/server.py`, `serve/test_server.py`, `serve/web/app.js` |
| Browser start page | `Scripts/Strata_Start.bat` | `serve/server.py` |
| Cache ids (`cache_system`, `cache_client`) | — | `serve/server.py`, `serve/test_server.py` |
| Named prompt blocks (`POST /cache`, `{{cache:NAME}}`) | `serve/prompt_blocks.py` | `serve/server.py`, `serve/test_server.py`, `tools/strata_mcp.py`, `tools/test_strata_mcp.py` |
| MCP tools in use | `tools/filesystem_mcp.py`, `tools/web_search_mcp.py` | none |

Everything else is upstream's.

## 1. Start/stop scripts

Strata could be started but not stopped: a 47 GB model pinned in VRAM had to be killed
from Task Manager.

- `Strata_Stop.bat` kills the server first, which closes the engine's input; the engine
  leaves its serve loop on stdin EOF and returns RAM and locked VRAM. Options: `/force`,
  `/timeout N`, `/status`.
- `Strata_Start.bat` starts a model, lists the installed `strata-*.json` files when there
  is more than one and asks which to start (Enter takes the newest; `/newest` skips),
  refuses a second copy, reports an engine left behind by a dead server, ignores the
  `strata-<tag>.shared-settings.json` sidecar, refuses to start the mock engine when no
  model is installed, and times out the model prompt. Options: `/restart`, `/status`,
  `/window`, `/noopen`.
- `Strata_DefaultModel.ps1` is the start script's source of "what is the default model";
  the script calls it if present.

The stop order is the whole point: the engine blocks on the server's input, so the server
must die first for the arena to be released.

**Verify:** `python -m pytest tools/test_start_script.py -q`.

## 2. Pre-load model check

An unsloth "UD" GGUF can be packed but can never start the engine, and the only symptom
was `native embedding: ...` in the log about a second after start.

- `tools/native_check.py` reads GGUF headers only. It checks `token_embd.weight` (must be
  an encoding `is_iq()` handles) and `per_layer_token_embd.weight` (must be IQ4_NL), plus
  the vision encoder, and returns problems with a remedy.
- `tools/gguf_inventory.py` lists a shard's tensors, types and offsets without a model.
- The `serve/server.py` hook runs `check_config(cfg)` in the `--engine strata` branch,
  after the card-role lines and before anything loads, and fails with the reason.
  `serve/server.py` already puts `ROOT/tools` on `sys.path`.
- `docs/ORCA.md`'s native-check note was folded into this file instead of upstream's.

**Verify:** `python -m pytest tools/test_native_check.py -q` (17 tests).

**Re-implementation:** the hook is self-contained. If upstream refactors the start
branch, re-place it immediately before the model loads; the import failing degrades
gracefully (`NC is None`).

## 3. Refusal diagnostics

A refused prompt returned only a 400 to the client: no server line, no breakdown, no
Monitor row.

- `REMEDY_NO_ROOM` / `REMEDY_EXCEEDS` next to `CTX_SLACK`, shared by the 400 text and the
  log line.
- `prepare(..., cancel=None, api="")` — `api` is `openai` / `anthropic` / `mcp`.
- `img_tokens` counts the real encoded expansion of images.
- `refuse()` prints `[strata] refused <api>: prompt N tokens, asked for M tokens, context
  C (reason); <remedy>`, prints a breakdown (system / tools / images / three largest
  messages) via `refusal_detail()`, and appends a history row with `"finish": "rejected"`.
  Totals and speed cards are deliberately untouched.
- `serve/web/app.js` renders the Refused row in the Monitor.

Merged with upstream's newer refusal handling (both kept): `forget_prompt()` on context
refusals (#567), `ClientGone` around the MCP prepare, and the `cancel` plumbing.

**Merge regression fixed (27 Oct 2026).** The merge dropped upstream's two lines

```
cancel = threading.Event()
self._watch_client(cancel)
```

from `_openai` and `_anthropic`. Every POST to those routes died with
`NameError: name 'cancel' is not defined`, and the client saw the connection close
with no response. Restored in both, keeping this fork's `api=` argument. Upstream's
default is now `fit_max_tokens=True`, which shortens the answer instead of refusing;
the two strict refusal tests now set it `False` explicitly, as
`test_strict_mode_keeps_the_llama_cpp_400` already did.

**Verify:** `python -m pytest serve/test_server.py -q -k "refus or context"`.

**Re-implementation:** most conflict-prone change. Apply last, after reading upstream's
current refusal code.

## 4. MCP tools (in use)

- `tools/filesystem_mcp.py`, `tools/web_search_mcp.py`

Both are referenced from the live `strata-iq3_xxs.json`:

```json
"mcp_servers": {
  "web":   {"command": "D:\\Strata\\.venv\\Scripts\\python.exe", "args": ["D:\\Strata\\tools\\web_search_mcp.py"]},
  "files": {"command": "D:\\Strata\\.venv\\Scripts\\python.exe", "args": ["D:\\Strata\\tools\\filesystem_mcp.py", "--root", "D:\\", "--policy", "D:\\strata-data.json"]}
}
```

If they are ever removed, update that config in the same change.

## 5. Deliberately removed (27 Oct 2026)

- `_probe/` (13 scratch files), `strata-wrote-this.txt`, `setup-iq2_xs-run.err`,
  `.diag1.txt`, `strata-iq3_s.json.bak-20261016-031651` — accidental commits.
- The large-pages experiment: `tools/large_pages_ab.py`, its test,
  `Scripts/Strata_LargePages.ps1`, `bench/results/2026-10-03-large-pages/` and the
  `tools/calibrate.py` tweak. **Finding, kept here:** 9 interleaved rounds on an
  RTX 5070 Ti + Ryzen 7 8845HS (IQ3_XXS) measured 64.6 vs 64.2 tok/s decode (0.7%), with
  the sign reversing between runs — an order of magnitude under the 3% bar, so
  SeLockMemoryPrivilege is not worth granting. Windows reports `VirtualAlloc error 1314`
  without the right; the engine then walks the arena on 4 KB pages and says so at start.

## 6. Merge playbook

Do this monthly, or on each upstream release. The 27 Oct merge was painful only because
two weeks and 2,337 commits had accumulated.

```
git fetch upstream
git merge upstream/main
```

Expected conflict files — only two now that upstream docs are untouched:

- `serve/server.py` — our hook + refusal diagnostics vs upstream's ongoing work.
- `setup.py` — model discovery and `--compat-bf16` handling.

Resolution rule: **keep both sides' semantics**, not one side. The last merge's shape:

- `prepare(...)` keeps upstream's `cancel` and `forget_prompt`/`ClientGone` handling
  *and* our `api` parameter and `refuse()` calls.
- `setup.py` keeps upstream's `ready`/NVFP4 branch *and* our `discover_shards` +
  per-user shard-set label, with `label = None` for the ready path.

Verify before committing the merge:

```
python -m py_compile serve/server.py setup.py
python -m pytest serve/test_server.py -q           # full file: py_compile cannot see a NameError in a handler
python -m pytest tools/test_native_check.py -q      # 17
python -m pytest tools/test_start_script.py -q      # 12
git grep -n -E '^(<<<<<<<|>>>>>>>)'                 # none
```

Commit the merge with a message naming what was resolved, push `develop`.

**Operational note.** The server on port 8080 keeps the code it loaded at start; restart
it after pulling or merging (`Scripts\Strata_Start.bat /restart`).

## 7. Patch series

Outside the repo, a re-appliable export can be regenerated at any time:

```
git format-patch upstream/main..develop -o <dir>
git diff upstream/main develop > fork-cumulative.diff
```

Re-apply on a fresh upstream with `git am <dir>/*.patch`, or cherry-pick single commits
with `git cherry-pick -x <sha>`.

A change that is not committed yet is stashed as a plain diff as well, so it can be
re-applied after an upstream merge without the commit:

```
D:\Strata-data\patches\2026-10-27-cache-ids-and-prompt-blocks.patch   # sections 11-12
git apply --3way D:\Strata-data\patches\2026-10-27-cache-ids-and-prompt-blocks.patch
```

It holds sections 11 and 12 together - `serve/server.py`, `serve/test_server.py`, the new
`serve/prompt_blocks.py`, `tools/strata_mcp.py`, `tools/test_strata_mcp.py` and this file (the earlier
`2026-10-27-cache-ids.patch` is the same work without section 12). It does not touch the model config.

## 8. Open items

- Consider sending the start/stop scripts and `native_check.py` upstream. Both are
  generally useful, and an accepted PR permanently removes the two biggest conflict
  sources.
- `tools/test_start_script.py` was updated for the `choice`-based picker (the old test
  asserted a `set /p` guard that no longer exists).

## 9. Windows service (27 Oct 2026)

The start script needs a signed-in person. The service starts Strata at boot, keeps
serving after sign-out, and starts the server again if it exits.

- `Scripts\Strata_Service.ps1` is the control panel: `Install`, `Uninstall`, `Start`,
  `Stop`, `Restart`, `Status`, `Logs`, `Edit`. Actions that change the system ask for
  elevation once; their output is shown in the calling window afterwards.
- `Scripts\Strata_Service.py` is the service. A Windows service cannot run
  `strata launch` (it detaches and returns, and the SCM would call the service dead),
  so the service runs Strata's own owner host (`strata_control.runtime`) and adds the
  watchdog the host does not have. No pywin32, no NSSM, no new package: the SCM calls
  are ctypes, like `winproc.py`.
- Registration: name `Strata`, display `Strata AI Server`, LocalSystem, automatic
  start, SCM restart actions 5 s / 30 s / 60 s. `install` refuses to replace a
  same-named service it did not create unless `-Force` is given.
- Its own state, not the user's: `C:\ProgramData\Strata\service` holds `service.conf`,
  daily logs in `logs\`, and `state\` (runtime and profiles), which Strata restricts to
  SYSTEM. The profile is `strata-service`; the installing person's
  `%APPDATA%\Strata` profiles are not used. `Status` therefore reports the SCM state
  and `/health`, not the private runtime files.
- The model is one editable file, `service.conf`, written on the first install with
  every installed model listed above the chosen line:

```
# Model configs found in D:\Strata when this file was written:
#  D:\Strata\strata-iq2_xs-20k.json  qwen3.8-flash-next-iq2_xs  context 20480  port 8080
#  D:\Strata\strata-iq3_s.json       qwen3.8-flash-next-iq3_s   context 131072  port 8080
#  D:\Strata\strata-iq3_xxs.json     qwen3.8-flash-next-iq3_xxs  context 204800  port 8080
config=D:\Strata\strata-iq3_xxs.json
#port=8080
#host=127.0.0.1
#shutdown_timeout=30
```

  Edit `config=`, then `-Action Restart`. The installing account is granted Modify on
  the file, so Notepad needs no elevation. A reinstall keeps an existing `config=`.
- Watchdog: `ready` waits; `starting` waits; `failed` or `stale` stops what is left and
  starts again; backoff 5 s doubling to 60 s, reset on `ready`. A failed start copies
  the runtime's error, exit codes and the server's last lines into the daily log.
- Stop is the documented drain: the SCM STOP sets a flag, the host closes the server's
  stdin, `serve/owner_guard.py` turns EOF into Ctrl+C, and the engine is released.
  `strata launch --stop` does not know about the service; use `-Action Stop`.
- One server per port. `Install` warns about a scheduled task or Run entry that also
  starts Strata; disable it before the service starts, or the second copy fails on the
  busy port.
- Not done: a real reboot. The registration is AUTO_START and the SCM start path was
  exercised; boot itself was not. A change made here was always run from an already
  elevated console, so the wrapper's own hand-off to an elevated copy was checked only
  on its own (the `Start-Process -Verb RunAs` line, with a stand-in command), not as a
  full run from a normal console.

**Measured here (27 Oct 2026, RTX 5070 Ti + Ryzen 7 8845HS, IQ3_XXS, context 204800):**
service start to `/health` with `loaded: true`: 36-65 s. A 1-token completion returned
200 on both `/v1/chat/completions` and `/v1/messages`. Stop drained and a restart
reloaded. The first attempts failed because of the `cancel` merge regression above
(the readiness probe's POST was dropped); after that fix the service stayed up.

**Verify:** `python -m pytest tools/test_service.py -q` (32 tests; no Administrator
rights and no service-manager changes).

## 10. Browser start page (27 Oct 2026)

The launcher opened `http://127.0.0.1:8080/`. Opening a URL the browser already has in a
tab only focuses that tab — it does not reload it — so the old llama.cpp page cached at
that origin (from before Strata replaced the engine) stayed until Ctrl+F5. A hash-only
`/#monitor` would not have reloaded the document either.

- `serve/server.py --open` opens `/?open=<epoch>#monitor`: the query is a new URL every
  start, so the browser performs a real navigation, and the hash selects the Monitor tab.
- `/` and `/api-monitor` are sent with `Cache-Control: no-cache`.
- `Scripts/Strata_Start.bat`'s "already running" branch opens the same URL.

**Verify:** restart the server, then check the page opens on the Monitor tab; or
`Invoke-WebRequest http://127.0.0.1:8080/` reports a `Cache-Control: no-cache` header.

## 11. Prompt reads, the conversation cache, and cache ids (27 Oct 2026)

A character chatbot - several characters, several people, each browser client sending its own
id - waited about 8 s on every prompt. The prompt is the cost: reading one is 337-559 tokens/s
on this card, and a 1,900-token character card is 5-12 s.

Upstream already has the three pieces this needs; none is on by default:

- **Checkpoints** (`--prompt-cache`, default 6): the engine keeps the state at turn boundaries,
  so a prompt that starts with one reads only what follows. `--prompt-cache-root` (default
  2048) is the shortest system prompt that gets a root checkpoint: a short character card was
  never checkpointed at all, so `--prompt-cache-root 256` is in the live config.
- **Parked conversations** (`--conversation-cache-mib`, `--conversation-cache-slots`): when the
  next request is a different conversation, the engine parks the current one (host RAM) and
  restores it when that client comes back. 0.1.51 only - the packaged 0.1.28 engine answers
  "unknown argument: --conversation-cache-mib" and never gets to serving.
- **A pinned prefix** (`pin=N`, sent for `strata_prefix`): a checkpoint at N that retention never
  evicts, and a parked conversation holding it stays parked.

**What this fork adds** (`serve/server.py`, server only - no engine change):

```
POST /v1/chat/completions
{"cache_system": "character-mira", "cache_client": "client-a73ce89e5f2e46d",
 "messages": [{"role": "system", "content": "<the card>"}, ...]}
```

- `cache_system` names the character's card. The leading `system`/`developer` messages are
  marked as the shared prefix, which `resolve_prefix()` turns into the engine's `pin=N` at the
  card's end (the exact token boundary is decided by the rendered text, never by the name).
  An explicit `strata_prefix` in the same request wins.
- `cache_client` names one client's conversation. Nothing is sent to the engine for it - the
  conversation cache matches by tokens, not names - it names the client in `/metrics`
  (`cache_ids`) and in one log line per new name.
- Absent fields leave the request exactly as it was. A malformed name (empty, spaces, over 200
  characters or not a string) is a 400. `POST /v1/messages` takes the same fields; its
  top-level `system` is already message 0 there.

**Measured here** (RTX 5070 Ti + Ryzen 7 8845HS, IQ3_XXS, context 204800, int8 K/V, engine
0.1.51 built from this tree: CMake VS 2022, CUDA 13.4, `-DCMAKE_CUDA_ARCHITECTURES=120`):

| Run | Result |
|---|---|
| Three characters, ~3.7k-token cards, three clients, parking on | first turns 6.66 / 9.17 / 6.46 s; every later request 0.36-0.45 s, a second person behind the same card included |
| Turn 2 of a 5,450-token prompt | engine restored 2,895 tokens from its parked conversation in 42.8 ms, then read the 2,555 new tokens at ~410 tokens/s |
| 2,048 MiB budget | evictions after ~6 conversations; parked entries dropped |
| 3,072 MiB budget, 6 slots | 6 parked, **0 evictions**; 37,859 of 70,183 prompt tokens reused over 25 requests |
| Same traffic, `cache_system` + `cache_client` vs none | no measurable difference at this size (0.32-0.49 s either way): parking alone reuses the card |

The budget is RAM, not VRAM, and it is the limit that matters, not the slot count. Parked
snapshots measured 347 MiB at 624 tokens, 380 MiB at 2,929 and 531 MiB at 5,477: most of a
snapshot is a fixed cost, so the budget buys a handful of *conversations*, not a token count.
3 GiB held six parked conversations with no evictions on this machine (~4.7 GB free with the
engine loaded); 2 GiB did not hold five. `--conversation-cache-slots` only caps how many
conversations.

**The pin's trade-off, read from the engine's code and visible in the log:** a match that reaches
a pinned checkpoint is *taken* from the parked conversation rather than *borrowed* from it
(`ConversationCache::borrows`). So a second person's first message to a character consumes the
first person's parked history; that history is read again on their next turn (the card itself
stays pinned). Sending only `cache_client` keeps the borrow, which is the better behaviour when
the cache is not under pressure. Neither puts a card in a queue of its own: a pin protects the
whole parked conversation that holds it.

**Not done:** the ~10k-token "system prompt queue" as a separate store. Section 12 adds a named store of
*prompt text* (the blocks a request is built from), which keeps every request behind a block byte-identical;
what is still missing is a pool of pinned *engine state*, one entry per card, so a card is reused without
taking a whole parked conversation.

**Verify:** `python -m unittest serve.test_server.CacheIds -v` (6 tests), and
`python -m unittest serve.test_server` (361 tests, 114 s on this machine).

**Re-implementation:** the change is two new top-level fields and one helper family
(`cache_id`, `note_cache_ids`, `record_cache_ids`, `cache_id_view`) plus its call in
`prepare()`; upstream clients never send the fields, so a merge can keep both sides.

## 12. Named prompt blocks (27 Oct 2026)

Section 11 makes the engine keep a card it has already read. That only helps if the card arrives again as
the *same tokens*: a client that rebuilds its prompt every turn, with one character different in the
preamble, makes everything after that character a miss and reads the card again.

So Strata keeps the text itself, under a name the client chooses, and expands it into the prompt:

```
POST /cache   {"key": "character_a73ce89e5f2e46d0807369a5fd62e74f", "value": "<the card>"}
POST /v1/chat/completions
{"cache_pin": "character_a73ce89e5f2e46d0807369a5fd62e74f",
 "cache_client": "a73ce89e5f2e46d0807369a5fd62e74f",
 "messages": [
   {"role": "system", "content": "{{cache:character_a73ce89e5f2e46d0807369a5fd62e74f}}\n\n{{cache:user_info_a73ce89e5f2e46d0807369a5fd62e74f}}\n\n{{cache:conversation_summary_a73ce89e5f2e46d0807369a5fd62e74f}}"},
   {"role": "user", "content": "the new message"}]}
```

**The store knows nothing about what the text is.** A block is a name, some text, and Strata's own
timestamps. There is no notion of a character, a person or a summary anywhere in `serve/prompt_blocks.py`
or in the server code that uses it; those names exist only in the client.

- `POST /cache {"key", "value"}` stores or replaces a block; `{"key", "delete": true}` removes it.
  Names are 1-200 characters of letters, digits and `. _ : + -`; one block holds at most 4 MiB, the
  whole store 8 MiB in 4096 blocks (the config's `"prompt_blocks": {"max_mib": 8, "max_entries": 4096}`
  changes it). A malformed request is a 400.
- `GET /cache` lists each block's name, size, the time Strata stored it and last used it, and how often -
  never the text. `GET /cache/<key>` returns one block's text with the same figures.
- `{{cache:NAME}}` in a message (or in the text part of a message with images) is replaced by the block's
  text before the prompt is rendered. A name the store does not hold is a **400**, never the literal
  placeholder sent to the model: a client that forgot to store its card learns it from the answer, and the
  model never sees `{{cache:`.
- A reference counts as a *use*. When the store is full, the block used longest ago goes first - not the
  one stored longest ago, which would evict the long-lived preamble that every request uses in favour of a
  summary written a minute ago. Order is kept by a counter, so two blocks written in the same clock tick
  still evict in the right order.
- `cache_pin: "<name>"` names the block that ends the shared start of the prompt: the server marks
  `strata_prefix={"message": i, "chars": n}` there, which `resolve_prefix()` turns into the engine's
  `pin=N` (section 11). With one message holding the card and then the client's own text, the card's block
  is where the pin belongs; the leading-system-message rule of `cache_system` would pin past it, at a
  boundary only one client shares. `cache_system: "<name>"` does the same when its name is a block in the
  prompt and keeps its old rule when it is not; an explicit `strata_prefix` still wins; a `cache_pin` that
  names no block in the prompt is a 400 (pinning nothing would be silently slower).
- The store is memory only. A restart empties it; the client stores its blocks again, and until it does,
  its requests are refused with the block's name in the message. Values are never logged; the log line on
  a store holds the name, its size and the store's totals.
- `/metrics` gains `prompt_blocks` (the same figures as `GET /cache`), and `tools/strata_mcp.py` gains
  `strata_cache_put`, `strata_cache_list` and `strata_cache_get` for an agent with no HTTP client of its own.

**Measured here** (RTX 5070 Ti + Ryzen 7 8845HS, IQ3_XXS, context 204800, int8 K/V, engine 0.1.51 from this
tree, the live config's 3 GiB conversation cache and 6 slots): one card (~3,600 tokens), a per-client block
of the client's own text and a per-client summary block; two clients, five requests in each phase; phase A
stored the three texts as blocks and sent `cache_pin` naming the card, phase B sent exactly the same texts
inline (`D:\Strata-data\probe\blocks.py`).

| The engine's own log | Blocks + `cache_pin` | The same texts inline |
|---|---|---|
| A returning client's next turn | restored 3,688 tokens at the pinned checkpoint in 45.4 ms | restored 452 tokens at a turn-boundary checkpoint |
| A second client's first request, same card | reused 3,688 tokens | reused 452 tokens |
| Parked conversations evicted while parking | 0 | 2, on byte pressure |

Wall clock did **not** separate the phases on this machine, so no speed is claimed from it: the same
4,510-token cold read took 9.43 s in phase A and 6.45 s in phase B, and inside phase A the same 3,688-token
reuse took 2.59 s once and 11.08 s another time, with per-request prefill between 210 and 750 tokens/s for
identical work. What the blocks bought is in the log and in the token counts: the shared 3,688-token start
came from a checkpoint on every request, where the inline shape matched 452 tokens and cost two parked
conversations.

The text that is genuinely new on every request (a client's own details and its summary, 800-1,600 tokens
here) is not made cheaper by any of this, and it is what the remaining seconds are spent on. Keeping the
part that changes last in the prompt leaves it the only part that has to be read again.

**Verify:** `python -m unittest serve.test_server.NamedBlocks serve.test_server.CacheEndpoints -v` (16 tests),
`python -m unittest serve.test_server` (377 tests, 113 s on this machine),
`python -m unittest tools.test_strata_mcp` (53 tests; stop Strata first - the start/stop tests need port 8080
free).

**Re-implementation:** `serve/prompt_blocks.py` is a new file and depends on nothing else in the tree; the
server changes are the import, `self.blocks` in `__init__`, `expand()` and the `blocks` argument in
`prepare()`, `block_pin()` used by `note_cache_ids()`, the `/cache` routes, one `/metrics` key and the
config line. Upstream requests carry no `{{cache:` and no `cache_pin`, so a merge can keep both sides; the
one thing to re-check is that `expand()` still runs before `encode_prompt()` in `prepare()`.

