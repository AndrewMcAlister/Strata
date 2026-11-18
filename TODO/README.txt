Strata on the MI50 (Radeon Pro Vega 20, gfx906) - what was done, 18 Nov 2026
============================================================================

Built it. Strata's gfx906 engine now compiles, links and starts on Windows:
tools/hip/build_windows_gfx906.bat (ROCm 10.3.0a20261118 from TheRock), engine
0.1.61, 24.9 MB. Two blockers had to be fixed: the Windows SDK was never
installed, and the HIP shim lacked cudaDeviceGetStreamPriorityRange and
cudaStreamCreateWithPriority.

Diagnosed it. The MI50 cannot run HIP on Windows - measured four ways: AMD's HIP
SDK 7.2.3 hipInfo says "no ROCm-capable device"; AMD's own table marks gfx906
unsupported on Windows; the SDK ships gfx906 kernels anyway, so it is not
missing code; the card's driver carries no compute component. The Windows route
is closed.

Found the alternative. The card is a working Vulkan 1.3 device (vulkaninfo).
Strata's engine has no Vulkan path - 0 references, and 50 kernels / 38,302 lines
would need rewriting - so the MI50 can only serve through llama.cpp Vulkan or
Strata's picture generator.

Wired the configs. MI50 variants of IQ2_XS and UD-Q4_K_XL; all four configs pass
native_check.py --config. The q4 "coding" config was broken three ways (dead
unsloth1 path, shard 1 for --native, no --ple-gguf) and is now fixed - which
matters for the RTX 5070 Ti machine as well.

Fixed the plumbing. Three real service bugs: a failed start wedged the service
"Running" and idle forever; the switch reported "Serving" when the stand-in held
the port; Status blamed loading for a failure. Plus a pre-switch port guard, a
Reason line, and a stop that no longer hides its 60-second wait.

Recorded and committed. docs/FORK.md section 13 (a config decides engine and
card); four commits on support_mi50_ROCm-HIP, this page being the last.

Still open: the MI50 runs only Vulkan workloads; Strata's text engine belongs on
the 5070 Ti.

The measurements, the AMD support table, the disk layout and the no-Linux
decision are in vega20-gfx906-engine-plan.md, in this folder.
