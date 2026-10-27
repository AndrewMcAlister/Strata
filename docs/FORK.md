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
