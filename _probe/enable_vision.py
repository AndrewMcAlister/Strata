"""Turn the image encoder on or off in an installed model's run config.

On (`python _probe/enable_vision.py strata-iq3_s.json`) writes exactly what setup.py
writes for `--vision gpu` (setup.py:5722/5823): `--vision` before the `--vram-reserve-mib`
value in "args", plus a "vision" section naming the engine's strata-vision.exe, the mmproj
file, the config's first shard as the model, gpu=true and the 1024-token default.
Off (`... strata-iq3_s.json --off`) takes those two things back out, which leaves the
config as setup.py writes it without `--vision`.

Nothing else in the config is touched: user keys ("sampling", "fit_max_tokens") and the
engine arguments that were already there stay as they are.  serve\\server.py reads the
file at startup, so the change takes effect on the next start: Scripts\\Strata_Start.bat
/restart, or Strata_Stop.bat followed by Strata_Start.bat.
"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MMPROJ = Path(r"D:\Strata-data\models\mmproj-Qwen3.8-Flash-Next-BF16.gguf")
MAX_TOKENS = 1024            # setup.py VISION["gpu"]["max_tokens"]
RESERVE_MIB = 700            # setup.py VISION["gpu"]["reserve_mib"]


def main() -> int:
    off = "--off" in sys.argv[1:]
    rest = [a for a in sys.argv[1:] if a != "--off"]
    if len(rest) != 1:
        print("usage: python _probe/enable_vision.py <config.json> [--off]")
        return 2
    cfg_path = Path(rest[0])
    if not cfg_path.is_absolute():
        cfg_path = ROOT / cfg_path
    cfg = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
    args = cfg["args"]
    if off:
        while "--vision" in args:
            args.remove("--vision")
        cfg.pop("vision", None)
        said = "images off"
    else:
        vis_exe = ROOT / "engine" / ("strata-vision.exe" if os.name == "nt" else "strata-vision")
        if not vis_exe.is_file():
            print(f"no image encoder at {vis_exe} (install it with setup --vision gpu)")
            return 1
        if not MMPROJ.is_file():
            print(f"no mmproj at {MMPROJ} (install it with setup --vision gpu)")
            return 1
        if "--vision" not in args:
            if "--vram-reserve-mib" in args:
                args.insert(args.index("--vram-reserve-mib"), "--vision")
            else:
                args += ["--vision", "--vram-reserve-mib", str(RESERVE_MIB)]
        model = next(a for a in args if str(a).endswith(".gguf"))   # the first shard, as setup does
        cfg["vision"] = {"exe": str(vis_exe), "mmproj": str(MMPROJ), "model": model,
                         "gpu": True, "max_tokens": MAX_TOKENS}
        said = "images on"
    tmp = cfg_path.with_name(cfg_path.name + ".tmp")   # a half-written config is never left behind
    tmp.write_text(json.dumps(cfg, indent=1), encoding="utf-8")
    os.replace(tmp, cfg_path)
    print(f"{said}: {cfg_path}")
    print(json.dumps(cfg["vision"] if not off else {"vision": "removed", "args now end": args[-2:]},
                     indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
