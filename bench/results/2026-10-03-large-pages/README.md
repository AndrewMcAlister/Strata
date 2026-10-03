# 2026-10-03: 2 MB pages for the expert arena (Windows `SeLockMemoryPrivilege`)

The expert arena is the ~40 GB of host memory the CPU walks expert by expert and the GPU DMAs out of.
`include/strata/core/pinned.hpp` says why the engine would rather have 2 MB pages than 4 KB: *"33.97 GB at
4 KB pages is 8.3 million TLB entries, which does not fit in any TLB, so every block of every expert matvec
takes TLB misses."* On Windows that needs `SeLockMemoryPrivilege`, which a normal account does not hold, so
every stock start logs

```
expert arena: cudaHostRegister PORTABLE ok; large pages refused for 42916118528 B
              (GetLargePageMinimum=2097152, VirtualAlloc error 1314); using 4 KB pages
```

`Scripts/Strata_LargePages.ps1` grants it. This is the measurement of what that grant is worth.

## What was measured, and on what

Windows 11 Pro (10.0.26300), RTX 5070 Ti (16 GB, driver 616.92), Ryzen 7 8845HS, 62.8 GiB RAM, IQ3_XXS
pack. Decode only, by `tools/large_pages_ab.py strata-iq3_xxs.json --rounds 9`:

```
.venv\Scripts\python tools/large_pages_ab.py strata-iq3_xxs.json --rounds 9
```

The same median-of-three-fixed-prompts decode rate `tools/calibrate.py` reports, with the two
configurations **interleaved** (large, 4 KB, large, 4 KB, ...) because the adaptive expert tier makes single
measurements noisy by a few percent. The engine logs which backing it actually got on every start and the
script refuses a round that did not get what it asked for, so a silent fallback cannot be averaged in.

## Result: no measurable difference

| | large pages | 4 KB pages |
|---|---|---|
| decode tok/s, 9 rounds | 59.5, 64.4, 64.6, 64.5, 64.7, 64.8, 64.4, 64.8, 64.7 | 64.0, 63.8, 62.5, 64.2, 64.5, 64.6, 64.1, 64.8, 64.4 |
| **median** | **64.6** | **64.2** |
| prompt read | 70.5 tok/s | 70.8 tok/s |

- **0.7% between the medians** — an order of magnitude below the 3% bar `tools/calibrate.py` itself uses.
- **All 18 starts got the backing they asked for** (0 dropped rounds), so both halves are what they claim.
- **The point estimate reverses between runs.** An earlier 3-round run on the same machine put 4 KB 4.8%
  *ahead*; this 9-round run puts large pages 0.7% ahead. A real effect does not change sign.
- The script prints `INCONCLUSIVE` because large pages spread 8.2% across their own nine runs — but that is
  entirely round 1's 59.5, the first load after a boot with the page cache cold. The other eight sit in
  64.4-64.8, a 0.6% band. Either way the conclusion is the same: 0.7% is not an effect.

The VRAM plan is untouched by page size: 5,213 experts / 8.46 GiB of expert cache either way.

## Decision

`Scripts/Strata_LargePages.ps1 /remove` — the privilege is not granted. It buys nothing measurable on this
machine, and it adds a startup failure mode that 4 KB pages do not have: a 40 GB large-page allocation needs
roughly 20,480 free 2 MB-granular pages and intermittently fails with `VirtualAlloc` 1450
(`ERROR_NO_SYSTEM_RESOURCES`) even on a machine that has just booted cleanly. The engine falls back to 4 KB
and logs it, so it was never a crash — but it is a flaky experiment with no upside.

That the CPU half of this engine does not care is consistent with where decode time actually goes here: the
GPU and PCIe side dominate, and the arena walk is one half of the work.

## Notes for anyone repeating this

- The privilege is **assigned but disabled** in every token until a process calls
  `AdjustTokenPrivileges(SeLockMemoryPrivilege)`; `src/core/pinned.cu:71-83` does this before `VirtualAlloc`.
  A probe that calls `VirtualAlloc` cold gets 1314 for an account that holds the right perfectly well.
- Grant it to the **account**, not to `Administrators`: a right on the account survives into the UAC-filtered
  non-elevated token, and one arriving through group membership does not.
- `/add` and `/remove` take effect at the **next logon**, not the next start — the token is built at logon.
- Strata must be stopped first: the A/B starts its own engine and the arena cannot be committed twice.
