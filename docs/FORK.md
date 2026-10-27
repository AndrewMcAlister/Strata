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
| Pre-load model check | `tools/native_check.py`, `tools/gguf_inventory.py`, `tools/test_native_check.py` | `serve/server.py` hook |
| Refusal diagnostics | — | `serve/server.py`, `serve/test_server.py`, `serve/web/app.js` |
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

## 8. Open items

- Consider sending the start/stop scripts and `native_check.py` upstream. Both are
  generally useful, and an accepted PR permanently removes the two biggest conflict
  sources.
- `tools/test_start_script.py` was updated for the `choice`-based picker (the old test
  asserted a `set /p` guard that no longer exists).
