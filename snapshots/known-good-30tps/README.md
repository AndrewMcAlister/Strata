# Known-good snapshot: 30 tok/s expert tier

Taken 2026-11-07 15:05, engine 0.1.56, after the tuning pass that fixed the
20-minute decode decay on this machine.

## What this snapshot is

The complete run config for `Qwen3.8-Flash-Next UD-Q4_K_XL` at 70K context,
as it ran when it was measured at ~30 tok/s. It sits outside the launcher's
`strata-*.json` glob, so it is never offered as a model choice.

## Measured on this snapshot

| | before | this snapshot |
|---|---|---|
| Decode, mean of 131 requests | 12-15 tok/s | **30.1 tok/s** |
| First 5 / last 5 turns | 19 falling to 13 | 30.6 / 32.7 |
| Expert blobs read from SSD per token | 50-60 | 3-12 |
| Prompt range exercised | up to 69K | 18.6K - 71.7K |
| Cold prefill (50K, no prefix reuse) | ~642 tok/s | ~600 tok/s |

The decay is gone: over an hour and 131 requests the effective rate drifted
up, not down, at constant-or-growing context.

## What differs from the shipped default

| setting | value | why |
|---|---|---|
| `--resident-budget-gib` | 40 (was 37) | +1,027 experts in the pinned RAM tier instead of on the SSD |
| `--conversation-cache-mib` | 0 | it parked 2.5-2.9 GB per turn and reused **0 bytes** in 9 parks |
| `STRATA_RAM_ADAPT` | 1 | the RAM tier follows the conversation; 2,300-4,700 experts move per turn, most out of a read already in flight |
| `STRATA_UNBUFFERED_LOAD` | 1 (explicit) | **do not set this to 0.** Through-page-cache reads starved the prompt path: prefill fell 600 -> 221 tok/s and RAM available fell 14.7 GB -> 182 MB |

Context stays at 70,680 cells (`--max-context 71680`). It was never the cause:
a 140K run on the same machine measured the same 13.7 tok/s as the 70K run.

## How to restore

Run `restore.cmd` in this folder, or by hand:

    copy /Y D:\Strata\snapshots\known-good-30tps\strata-unsloth-ud-q4_k_xl-dflash2-tuned-70k.json D:\Strata\
    D:\Strata\Scripts\Strata_Stop.bat /force
    start "Strata" /D "D:\Strata" "D:\Strata\Scripts\start-server-70k.cmd"

`Strata_Start.bat /restart` is avoided on purpose: while the agent's MCP tool
servers run (`tools\filesystem_mcp.py`, `tools\web_search_mcp.py` share
`.venv\Scripts`), its stop probe counts them as leftovers, kills the server and
then refuses to start a replacement.

## Known headroom at the time of the snapshot

- VRAM free with everything loaded: ~388 MiB - thin but not stalling
- RAM available: ~8.2 GB
- If a turn ever hangs instead of running slow, that is VRAM pressure:
  add `--vram-reserve-mib 999` (costs ~330 expert slots)
