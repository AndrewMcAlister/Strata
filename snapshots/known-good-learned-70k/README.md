# Known-good snapshot: learned expert profile

Taken 2026-11-07 18:20, engine 0.1.56.

## What this is

The same 70K configuration as `known-good-30tps`, with one difference:
`--expert-profile` points at a profile learned from ~2 hours of real traffic
(the Rust port of "The Entity In The Shed", Stage 1-2) instead of the shipped
`expert-profile.bin`. The save flags are off, so the profile is frozen.

## Measured on this machine, same workload

| | shipped profile | learned profile |
|---|---|---|
| Requests | 159 | 124 |
| Decode mean | 26.2 tok/s | **28.9 tok/s** |
| SSD expert blobs per token (median) | 9.6 | **7.5** |

+10% decode and -22% disk traffic. The blob drop is the mechanism, not a
coincidence: the ranking now matches the experts this workload actually routes
to, so fewer misses fall through to the file tier.

Honest caveat: this is **not a controlled A/B**. The workload moved between the
two runs (different code, different stage of the port). Treat +10% as a good
estimate, not a guarantee.

## When to prefer this one

The profile was learned from code porting. If the work shifts to something
else — documents, chat, data analysis — load `known-good-30tps` instead: a
profile overspecialised to code can lose on other traffic.

## Restore

Run `restore.cmd`, or by hand:

    copy /Y strata-unsloth-ud-q4_k_xl-dflash2-tuned-70k.json D:\Strata\
    copy /Y expert-profile-learned-70k-2026-11-07.bin D:\Strata\data\
    D:\Strata\Scripts\Strata_Stop.bat /force
    start "Strata" /D "D:\Strata" "D:\Strata\Scripts\start-server-70k.cmd"

## Note on the launcher

`Strata_Start.bat /restart` fails while the agent's MCP tool servers are running
(`tools\filesystem_mcp.py`, `tools\web_search_mcp.py` share `.venv\Scripts`, and
the stop probe counts them as leftovers). Start through
`Scripts\start-server-70k.cmd` instead.
