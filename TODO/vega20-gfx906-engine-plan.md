# Strata on the Radeon Pro Vega 20 (gfx906): build the engine on Windows

Working plan, written 18 Nov 2026. Not an agreed spec, not upstream's.
Steps that change the machine are marked **[changes this PC]**.

**Status, 18 Nov 2026:** the engine is **built** (`build-906-win\strata.exe`, engine 0.1.61) and it
runs (`--version` answers). It cannot start on the card yet: the Windows HIP runtime reports **no
ROCm-capable device**, because the AMD driver installed for the card carries no compute component (no
`amdocl64.dll`). Step 2 stopped there; steps 3 and 4 were not started, and the Linux fallback below is
the next move.

## Goal

Serve Strata's own engine on the **Radeon Pro Vega 20 (32 GB, gfx906)** of this PC, so that:

- the Windows service `Strata AI Server` answers on **http://127.0.0.1:8080** with a real model
  (IQ2_XS first, which is already on disk), not the scripted stand-in;
- the four Desktop shortcuts switch models instead of ending in
  `ProfileError: No engine for this PC was found ...`.

The Quadro M2000 is not part of this: sm_52 has no engine in the signed feed and none can be built
here. It stays a display card.

## Measured facts (18 Nov 2026, this PC)

| Item | Value | Where it came from |
| --- | --- | --- |
| Radeon Pro Vega 20 memory | 34,342,961,152 B = **32.0 GB** | `HKLM\SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}\0003` → `HardwareInformation.qwMemorySize` |
| the same card's chip | **1002:66A1** (Vega 20 = Instinct MI50, gfx906), driver 31.0.21924.61 | `Get-PnpDevice -Class Display` instance id `PCI\VEN_1002&DEV_66A1&SUBSYS_08341002`. The `DEV_69AF` in that registry key is AMD's *driver match string* ("Vega 12 [Radeon Pro Vega 20]"), not the chip, and "Radeon Pro Vega 20" is AMD's display name for both |
| the same card through WMI | 4,293,918,720 = 4.0 GB | `Win32_VideoController.AdapterRAM`, a capped 32-bit field — **wrong** |
| Quadro M2000 | 4 GB, sm_52, driver 556.39 | registry (`qwMemorySize`) + `nvidia-smi` |
| IQ2_XS, RAM + VRAM | **39.2 GB** (its 29 GB lookup table stays on the SSD) | `docs/MODELS.md` |
| IQ3_S, RAM + VRAM | 54.8 GB | `docs/MODELS.md` |
| System RAM | 63.7 GB total | `Win32_ComputerSystem` |
| Packs already prepared | `D:\Strata-data\packs\{iq2_xs,iq3_s,iq3_xxs,q3_k_xl,unsloth-ud-q4_k_xl}`, models in `D:\Strata-data\models` | directory listing |
| HIP/ROCm on this PC | none (no SDK folder, no `hipcc`, no `clang`) | `Get-Command`, Program Files |
| C++ toolchain | VS 2022 Community + Build Tools (MSVC, CMake, Ninja ship with it) | `vswhere` |
| Feed engine for this card | none: `strata update --check` → "the feed has no engine for this PC" | Terminal output |

**Consequence:** IQ2_XS fits this machine (32 GB VRAM + 64 GB RAM). An earlier note in this work said
it did not fit; that was wrong on two counts — it used the capped 4 GB WMI figure and the on-disk
size of the pack (63.4 GB) instead of the resident figure (39.2 GB).

## The route: gfx906 HIP build, Windows, from AMD's TheRock wheels

`docs/AMD_HIP.md` § *gfx906* says the Linux recipe needed a community ROCm image "because AMD's
current ROCm releases no longer ship gfx906 libraries". That is no longer true of TheRock nightlies,
and they ship **win_amd64** — checked on the index:

```
https://nightly.repo.amd.com/rocm/whl-next/rocm-sdk-device-gfx906/
  rocm_sdk_device_gfx906-10.3.0a20261118-py3-none-win_amd64.whl   (newest seen; win_amd64 exists back to 10.1.0a20260823)
also on the index: rocm, rocm-sdk-core, rocm-sdk-devel, rocm-sdk-libraries, rocm-sdk-device-<arch>
```

Strata's Windows HIP builder already installs exactly these wheels, one device wheel per arch:

```
tools\hip\build_windows.bat
  STRATA_ROCM_INDEX    https://nightly.repo.amd.com/rocm/whl-next/
  STRATA_ROCM_VERSION  10.2.0a20260930        (default)
  STRATA_HIP_ARCHS     gfx1100;gfx1101;gfx1102;gfx1200;gfx1201;gfx1030;gfx1031;gfx1151   ← no gfx906
  ROCM_VENV            <repo>\.rocm-win        BUILD_DIR <repo>\build-hip-win    DIST_DIR <repo>\dist
```

So the work item is a gfx906 variant of that script, using the documented opt-in switch
(`-DSTRATA_HIP_GFX906=ON`, **not** `-DSTRATA_ENABLE_HIP`; no hipBLASLt on gfx906).


## Rejected routes (and the numbers that settled them)

1. **A Vulkan backend for Strata's engine.** Upstream v0.2.15 has none: no `STRATA_ENABLE_VULKAN`,
   no `VkDevice`/`vkCreateInstance` in `src/` or `include/`. Its Vulkan is the picture generator
   (`serve/images.py`, `--image-backend vulkan`). Writing one means re-implementing the 58 public
   headers in `include/strata/kernels/` (49 `.cu`, **39,478 lines**) in GLSL/SPIR-V: months of work,
   and it needs an upstream issue agreed first (`CONTRIBUTING.md`). Measured again 18 Nov 2026 in this
   tree: **0** matches for `VULKAN`/`vkCreateInstance`/`VkDevice` in `src/`, `include/`, `CMakeLists.txt`
   and `cmake/`; `src/kernels` holds **50 `.cu` files, 38,302 lines**. The vendored ggml has only
   `ggml-cpu`, `ggml-cuda`, `ggml-cuda-tq`, so there is nowhere to drop a ggml Vulkan backend either.
   **But the card itself does Vulkan**: `vulkaninfo --summary` on this PC lists it —
   `GPU1: vendorID 0x1002, deviceID 0x66a1, deviceName "Radeon Pro Vega 20", driverName "AMD
   proprietary driver", apiVersion 1.3.260` — beside the Quadro M2000. So the hardware is capable; it is
   Strata's engine that has no Vulkan path. That is what fallback 2 (llama.cpp with `-DGGML_VULKAN=ON`)
   uses, and it works on this card today, with a different engine.
2. **Porting llama.cpp's Vulkan code** from `D:\Code\llama-server-ajm`. That backend is
   `ggml-vulkan.cpp` (**16,684 lines**) plus **155 `.comp` shaders / 26,530 lines**, written against
   ggml's op set (`MUL_MAT`, `ROPE`, `FLASH_ATTN_EXT`, …). Strata's engine calls its own kernel API,
   and its vendored ggml (`third_party/strata-ggml/ggml/src`) holds only `ggml-cpu`, `ggml-cuda`,
   `ggml-cuda-tq` — there is no place a ggml Vulkan backend would be used. The shaders are useful
   **reference** for quant layouts; they are not droppable code.
3. **JR-Strata** (`Goldlionren/JR-Strata`) — Intel **SYCL** (oneAPI 2026.1.0 / Level Zero 1.32.0) on a
   v0.1.39 base, frozen 9 Oct 2026, qualified on an Arc Pro B60. Wrong backend for an AMD card,
   ~880 commits stale, nothing to take.
4. **One Vulkan + one NVIDIA card on one model.** Not possible: one backend per engine binary
   (CMake refuses SYCL with CUDA/HIP). With 32 GB on the AMD card it is also unnecessary; the M2000
   is display only. The only heterogeneous arrangement Strata has is role separation — a text engine
   on one card and the Vulkan `sd-cli` doing pictures on another (see step 4).

## Plan

### Step 1 — the build script (written 18 Nov 2026; the build has not been run yet)  **[changes this PC]**

New file (fork rule: new files, no edits to upstream's): `tools\hip\build_windows_gfx906.bat`, a copy
of `build_windows.bat` with:

```
STRATA_HIP_ARCHS     gfx906
STRATA_ROCM_VERSION  10.3.0a20261118     (checked: the win_amd64 wheel exists, see below)
STRATA_ROCM_INDEX    https://nightly.repo.amd.com/rocm/whl-next/
ROCM_VENV            D:\Strata\.rocm-win          (~7 GB of wheels)
BUILD_DIR            D:\Strata\build-906-win
cmake flags          -DSTRATA_HIP_GFX906=ON -DCMAKE_HIP_ARCHITECTURES=gfx906
                     -DSTRATA_NATIVE_EXPERTS=ON -DSTRATA_PORTABLE=ON -DSTRATA_MMQ=OFF
                     (no hipBLASLt; ROCm's clang compiles the host code too, targeting the MSVC ABI)
```

How to run it, and what was checked without installing anything (18 Nov 2026):

- `tools\hip\build_windows_gfx906.bat check` prints every path, the wheel set and the cmake folder,
  then stops. It was run on this PC and exits 0. The run without `check` does the work.
- The pinned set resolves on the index without installing: `rocm`, `rocm-sdk-core`,
  `rocm-sdk-devel`, `rocm-sdk-libraries` and `rocm-sdk-device-gfx906`, all `10.3.0a20261118`
  (`pip install --dry-run`); `rocm_sdk_device_gfx906-10.3.0a20261118-py3-none-win_amd64.whl` is
  listed under `https://nightly.repo.amd.com/rocm/whl-next/rocm-sdk-device-gfx906/`.
- CMake **3.31.6-msvc6** and Ninja **1.12.1** are **not on PATH** on this PC: they are inside
  `C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\Common7\IDE\CommonExtensions\Microsoft\CMake\`.
  `build_windows.bat` assumes both are on PATH, so this script finds them there and puts Ninja on
  PATH itself.
- Two flags of the wave32 script are left out on purpose: `-DSTRATA_ENABLE_HIP` (CMake refuses it
  with the gfx906 switch, `CMakeLists.txt:98-103`) and `-DSTRATA_PREFILL_MMQ` (it refuses without
  `STRATA_ENABLE_HIP`, `CMakeLists.txt:172-174`).
- Not built by this script: the picture readers (`tools\vision`) and the release zip. The zip needs a
  rocBLAS `.kpack` for gfx906, and `package_windows.py` exits naming the wheel when one is missing.
- Output: `build-906-win\strata.exe` and `build-906-win\strata-device.exe`.

**Result, 18 Nov 2026.** Built on this PC: `strata.exe` is 24,982,016 bytes (21:16:43) and
`strata-device.exe` 71,680 bytes, from engine source 0.1.61 and ROCm `10.3.0a20261118`. Two things had
to be put right first, both found here:

1. **The Windows SDK was not installed.** `C:\Program Files (x86)\Windows Kits\10` held only
   `Catalogs` and `Redist`; CMake stopped at configure with "No CMAKE_RC_COMPILER could be found", and
   the link would have failed the same way (no `kernel32.lib`). Installed with
   `winget install --id Microsoft.WindowsSDK.10.0.26100` (10.0.26100.7705). Visual Studio's C++
   workload does not carry the SDK, so the script now checks for `rc.exe` and names what to install.
2. **The gfx906 shim lacked two CUDA names.** `src/core/dflash2.cpp` takes its CUDA branch in this
   route (the route defines `STRATA_HIP_GFX906`, not `STRATA_USE_HIP`) and calls
   `cudaDeviceGetStreamPriorityRange` / `cudaStreamCreateWithPriority`; the build stopped with "use of
   undeclared identifier". The two mappings were added to
   `include/strata/platform/hip_compat/strata_hip.h` (`hipDeviceGetStreamPriorityRange` and
   `hipStreamCreateWithPriority`). A two-line portability fix that the gfx906 route needs with ROCm
   10.3: worth offering upstream.

### Step 2 — prove it on the card (stopped: the runtime finds no device)

Run on 18 Nov 2026, with the ROCm `bin` (`_rocm_sdk_devel\bin`) on PATH:

```
strata-device.exe --list-devices  ->  (no GPU device)                                                     exit 0
strata-device.exe --selftest      ->  strata-device: cudaGetDeviceCount: no ROCm-capable device is detected exit 1
hipInfo.exe (ROCm's own sample)   ->  HIP API error = 0100 "no ROCm-capable device is detected"
strata.exe --version              ->  strata 0.1.61                                                       (the engine runs)
```

- The same three messages with `HSA_OVERRIDE_GFX_VERSION=9.0.6`.
- **Cause:** the driver installed for the card has no compute runtime at all. `amdocl64.dll` is not in
  `C:\Windows\System32`, and the card reports driver `31.0.21924.61` (`oem9.inf`, dated 12 Nov 2025):
  a display-only install. HIP cannot open a device the driver does not offer.
- **AMD's own answer, 18 Nov 2026.** The HIP SDK for Windows 7.2.0 release notes point at
  *System requirements (Windows)* (`rocm.docs.amd.com/projects/install-on-windows/en/docs-7.2/`), whose
  GPU table lists **"AMD Radeon PRO VII — GCN5.1 — gfx906 — Runtime ❌ — HIP SDK ❌ — ROCm Debugger ❌"**,
  and says *"If a GPU is not listed on this table, it is not officially supported by AMD"* and
  *"❌: Unsupported - the current HIP SDK release does not support this hardware"*. The same table marks
  RDNA2 (gfx1030/1031/1032) unsupported on Windows as well. So installing that SDK changes nothing for
  this card: it brings a compiler, headers and runtime DLLs, not a driver AMD will enumerate gfx906 on.
- **Tried AMD's own SDK, 18 Nov 2026 (installed, then tested).** HIP SDK for Windows **7.2.3** installed
  (components dated 2026-11-18, `C:\Program Files\AMD\ROCm\7.2`). Its own `hipInfo.exe` answers
  `HIP API error = 0100 "no ROCm-capable device is detected"`, and the built engine says `(no GPU device)`
  with either runtime on PATH (the TheRock 10.3 DLLs in `.rocm-win`, or the SDK's 7.2 `bin`). The SDK
  *does* ship gfx906 library code — `bin\rocblas\library\*gfx906*.hsaco|.dat|.co` — so this is not missing
  kernels: on Windows the runtime cannot open the device at all. The SDK install changed nothing about the
  driver and was left in place.
- **Next step on Windows: closed.** No driver install on Windows can help, because AMD's own table marks
  the architecture unsupported on that platform; nothing was installed or changed on the display driver.
- Fallback 1 below (Linux with the community gfx906 image) is the route these results point to: the same
  CMake flags, a runtime that can see gfx906, and the published 2× MI50 numbers.

### Step 3 — make it the service  **[changes this PC]**

- **Ready, 18 Nov 2026:** `strata-iq2_xs-20k-mi50.json` is a copy of the 20K config that names the built
  engine (`"exe": "D:\\Strata\\build-906-win\\strata.exe"`, `"backend": "hip"`, `lib_dirs` at the ROCm
  `bin`). Started by hand on port 8099, that config did run the MI50 build: the server's log shows
  `engine started: D:\Strata\build-906-win\strata.exe --serve ...` and the engine stopped only at
  `native embedding: cannot pin 322 MiB: no ROCm-capable device is detected`. `service.conf` lists the
  config; the live `config=` line and the active config are unchanged.
- Stop the stand-in first: `powershell -File C:\ProgramData\Strata\mock-8080.ps1 -Action Stop`
  (it holds 8080 at the time of writing).
- Point the run config at the built engine: `"exe": "D:\\Strata\\build-906-win\\strata.exe"` plus
  `"lib_dirs": ["D:\\Strata\\.rocm-win\\...\\bin"]` (the ROCm `bin` that holds `amdhip64*.dll`).
  Keep `component:strata-engine` only once the engine is installed as a component, which a hand
  build is not.
- `powershell -File D:\Strata\Scripts\Strata_Service.ps1 -Action Serve -Config D:\Strata\strata-iq2_xs-20k.json`
- Verify `-Action Status` shows the endpoint answering; decide the start type afterwards
  (`Set-ServiceResilience.ps1` keeps it Manual/demand today).

### Step 4 — Vulkan pictures on the same card (optional, after text works)

Separate from the engine: upstream v0.2.15's image path uses `sd-cli` and runs for any engine,
including the mock (`serve/server.py:10302` sets `"images"` up outside the `--engine strata` branch).

- The v0.2.15 tree is already staged: `D:\Strata-0215` (detached at `c77572806`).
- Needs the LunarG **Vulkan SDK** on Windows, then
  `python setup.py --images qwen-image --image-backend vulkan` (~10 GB: denoiser Q4_K_M 4.2 GB,
  text encoder 5.1 GB, VAE 0.7 GB; D: has 656 GB free).
- With 32 GB the denoiser and encoder fit on the card (the published 179 s at 1024×1024 is a
  780M iGPU on shared memory).
- To have both in one tree later: rebase this fork's 35 commits onto v0.2.15 (`docs/FORK.md` §6;
  conflicts in `setup.py`, `serve/server.py`, `tests/terminal/test_launch.py`).

## Risks and unknowns

- **gfx906 is community-tested, Linux only.** `docs/AMD_HIP.md`: the maintainers have no gfx906 card
  and "the engine here is checked to compile for gfx906 and nothing more". Windows + WDDM on gfx906
  has not been run by anyone in the project.
- Published numbers are **2× Instinct MI50 16 GB, Linux**, IQ3_XXS, 128K: 49.2 tok/s decode /
  353 tok/s prompt; 66.9 tok/s with `--pipeline-windows 2` (two cards). One 32 GB card is a different
  shape: treat these as an order of magnitude, not a promise.
- `docs/AMD_HIP.md` warns about Windows costs generally: KV streaming off, and the WDDM video-memory
  budget is subtracted from the free figure (`STRATA_WDDM_BUDGET=0` turns that off).
- The TheRock nightly is a moving target: record the exact `STRATA_ROCM_VERSION` used, in this file.
- First start loads ~39 GB; expect a long warm-up before `/health` says `loaded: true`.

## Fallbacks

1. **The Windows route stops early** — the build fails, or (as measured on 18 Nov 2026) the runtime
   finds no ROCm-capable device: Linux with the documented community image
   `mixa3607/rocm-gfx906:7.14-complete`, `--device=/dev/kfd --device=/dev/dri`, and
   `HSA_OVERRIDE_GFX_VERSION=9.0.6` on some boards. The engine built here would be built again there
   by the same CMake flags; only the ROCm provider changes.
2. **gfx906 works but is too slow or unstable:** llama.cpp (`D:\Code\llama-server-ajm`) with
   `-DGGML_VULKAN=ON` runs a smaller model on the same card today; it is a different engine, so
   Strata's expert streaming and its UI do not apply.
3. **Stick with the stand-in** for UI/client work: `C:\ProgramData\Strata\mock-8080.ps1 -Action Start`
   (scripted answers, clearly not the model).

## Open decisions

- First run at 20K context (cheap, likely to fit) or 94K (the fork's coding config)?
- Where the engine lives once built: keep `build-906-win\strata.exe` in place, or copy the exe and its
  ROCm DLLs into an `engine\` folder the way `setup.py` lays one out.
- Whether to keep a mock endpoint on a second port (8095) while the real engine owns 8080.
- Whether to open an upstream issue for gfx906-on-Windows (the project's rule is to agree scope before
  large work; a working build report is welcome per `docs/TEST_REQUESTS.md`).

## State of this machine when the plan was written

- `D:\Strata` on branch `develop` @ `dbbf939e` (pushed to `origin/main` as well, so the two match).
- Service `Strata AI Server` installed, LocalSystem, **Manual/demand**, stopped.
- `http://127.0.0.1:8080` answered by the stand-in engine
  (`python -m serve.server --engine mock`), pid 25832, started from
  `C:\ProgramData\Strata\mock-8080.ps1` — it must be stopped before a real engine takes the port.
- `D:\Strata-0215`: git worktree at upstream tag `v0.2.15` (detached), for the Vulkan image path.
- Run configs present: `strata-iq2_xs-20k.json` (20K, MTP) and
  `strata-q4_k_xl_dflash2_94k_novision.json` (94K, DFlash2), both with the MCP tools block; both name
  `component:strata-engine` today, which resolves only when an engine is installed for the card.

## If this PC switches to Linux (NOT CHOSEN — decision, 18 Nov 2026)

**Decided: no Linux on this PC.** The owner does not want it, so this section is kept only as the record
of what the alternative was, and of the facts that made it non-trivial. Nothing below is planned work.

What that leaves on Windows: Strata's text engine cannot use the MI50 (HIP cannot open the card, AMD marks
gfx906 unsupported on Windows, and the engine has no Vulkan path). The card *is* a working Vulkan 1.3
device (`vulkaninfo`), so the only ways to put it to work are Vulkan programs — llama.cpp built with
`-DGGML_VULKAN=ON` (a different engine), or Strata's own picture generator with `--image-backend vulkan`
(`serve/images.py`, `sd-cli`). Both need the LunarG Vulkan SDK on this PC, which is **not installed**
(no `glslc.exe`, no `C:\VulkanSDK`) — the loader in System32 is not enough to build shaders.

### The Linux route, for the record

Nothing here is done yet; this is the checklist, in the order that de-risks it.

**There is no Linux on this PC today (checked 18 Nov 2026).** No Linux partition on any disk; the UEFI
boot menu lists only *Windows Boot Manager*; WSL is installed but has **no distributions**
(`wsl -l -v` → "no installed distributions"). So "switching to Linux" means installing one, and WSL is
not a candidate: WSL2 passes only D3D12, never `/dev/kfd`, and `docs/AMD_HIP.md` records that the AMD
engines hang under it.

**Storage, which decides how this is done:**

| Volume | What it is | Linux? |
| --- | --- | --- |
| `C:` 931 GB NTFS, 515 GB free | one 932 GB NVMe (`CT1000P3PSSD8`), a plain partition | shrinkable; Linux can install beside it and mount it |
| `D:` 1,397 GB NTFS, label "Raid0" | a **Windows dynamic striped volume** across three 466 GB NVMe SSDs (it is not even a partition: `Get-Partition -DriveLetter D` finds nothing) | **no** — that needs LDM/`ldmtool` and striped reassembly, not something to trust with the only copy of the models |
| `E:` 111 GB NTFS | an empty USB disk | free to use as a live-USB stick |

The repo (`D:\Strata`) and every model (`D:\Strata-data`) sit on that striped volume, so a Linux install
**cannot read them**: the model has to be copied into the Linux filesystem (IQ2_XS ≈ 63 GB on disk,
UD-Q4_K_XL = 111.3 GB), on the space C: gives up or on a new SSD.

1. **Prove the card in ten minutes, before touching any disk.** Write Ubuntu to the empty 111 GB USB, boot
   it, and in the live session install docker and run:
   `docker run --rm --device=/dev/kfd --device=/dev/dri --group-add video mixa3607/rocm-gfx906:7.14-complete rocminfo`
   It must list `gfx906` (and `hipInfo` must name the card). If it does, Linux works on this card; if it
   does not, stop and do not repartition anything.
2. **Build the engine there** with the flags this plan already uses (they are the documented Linux ones):
   `cmake -S . -B build-906 -G Ninja -DSTRATA_HIP_GFX906=ON -DCMAKE_HIP_ARCHITECTURES=gfx906 -DSTRATA_NATIVE_EXPERTS=ON -DSTRATA_PORTABLE=ON`
   Keep the two `strata_hip.h` mappings from step 1: `src/core/dflash2.cpp` takes its CUDA branch on both
   platforms, so the build needs them on Linux too.
3. **Run it by hand first:** `strata-device --list-devices`, `--selftest`, then
   `python serve/server.py --engine strata --config <config> --port 8080`. `HSA_OVERRIDE_GFX_VERSION=9.0.6`
   on some boards.
4. **Then** the service and the Desktop shortcuts, as on Windows.

What the plan's measurements do and do not promise: the published gfx906 run is **2× MI50 16 GB** (49.2 tok/s
decode / 353 tok/s prompt, IQ3_XXS, 131K, engine 0.1.43.1), not one 32 GB card; one card is a different
shape, so treat the numbers as an order of magnitude. Our engine (0.1.61) has never been run on gfx906 by
anyone — it has been compiled, linked and started, and it stopped only at Windows' device enumeration.

## What changed on this PC, 18 Nov 2026 (do not revert blindly)

- **Windows SDK 10.0.26100** installed (`winget ... Microsoft.WindowsSDK.10.0.26100`, version
  10.0.26100.7705). It was absent; it supplies `rc.exe` and `kernel32.lib`. Nothing was uninstalled.
- **`D:\Strata\.rocm-win`** created: a venv holding ROCm `10.3.0a20261118` (`rocm`,
  `rocm-sdk-core`, `-devel`, `-libraries`, `-device-gfx906`, ~1.7 GB of wheels, cached by pip), stamp
  `strata-rocm.txt`. No system PATH change: the script sets PATH per run.
- **`D:\Strata\build-906-win`** created: the cmake build tree and `strata.exe` (24,982,016 bytes) and
  `strata-device.exe` (71,680 bytes). `CMakeCache.txt` there says `STRATA_HIP_GFX906:BOOL=ON`,
  `CMAKE_HIP_ARCHITECTURES:UNINITIALIZED=gfx906`, `STRATA_ENABLE_HIP:BOOL=OFF`.
- **`include/strata/platform/hip_compat/strata_hip.h`**: two CUDA-to-HIP mappings added
  (`cudaDeviceGetStreamPriorityRange`, `cudaStreamCreateWithPriority`).
- **`tools/hip/build_windows_gfx906.bat`**: new file.
- **`strata-iq2_xs-20k-mi50.json`**: new run config (copy of the 20K one) naming the built engine, with
  `"backend": "hip"` and `lib_dirs` at the ROCm `bin`. `service.conf` now lists it in its comment block;
  the active `config=` line still names the 94K config.
- **`strata-q4_k_xl_dflash2_94k_mi50.json`**: the same for Unsloth's UD-Q4_K_XL (the model wanted most on
  this card), and `Strata (q4 MI50).lnk` on the Desktop starts it.
- **`strata-q4_k_xl_dflash2_94k_novision.json` was broken and is fixed**: it pointed at
  `models\unsloth1\...`, a folder that does not exist, and it named shard 1 for `--native` with no
  `--ple-gguf`. It now points at `models\unsloth\...` with `--native` = shard 2 and `--ple-gguf` = shard 4;
  `tools/native_check.py --config` reports GO for all four configs.
- **`docs/FORK.md` §13**: what a run config's `exe`, `backend`, `lib_dirs`, `gpu`, `hip_ordinal` and `env`
  decide, and the two limits (no component package, no compute driver).
- **Not changed:** the AMD display driver (still `31.0.21924.61`, `oem9.inf`, no compute component), the
  components folder (no engine installed as a component), the run configs, `http://127.0.0.1:8080`
  (still the stand-in), and any CUDA or feed file.

