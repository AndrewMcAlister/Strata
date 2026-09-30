"""tools/gguf_inventory.py - read-only type inventory of a (possibly split) GGUF model.

    python tools/gguf_inventory.py --gguf "D:\\Models\\...\\...-00001-of-00003.gguf"

The gate for a model that did not come from the setup downloads.  Headers only, nothing written: every shard's
tensor counts and encodings, the per-layer ffn_{gate,up,down}_exps encodings (gate == up, and the three in one
shard, are what tools/iq_pack.py refuses to pack without), the shard holding per_layer_token_embd.weight
(--ple-gguf), the tensors whose kernels read them as BF16, and the routers.

Exit code 0 when the model can be packed AND the engine can load it (tools/native_check.py checks the engine's
side: token_embd.weight in the --native shard, the PLE table in the --ple-gguf shard), 1 with the failing check
when it cannot.
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import gguf_reader as G  # noqa: E402

SHARD_RE = re.compile(r"-(\d{5})-of-(\d{5})\.gguf$", re.IGNORECASE)
try:                                   # iq_pack's contract, imported so the two cannot drift
    import iq_pack as P                # noqa: E402
    ROUTERS, NOT_IN_PACK = P.ROUTERS, P.NOT_IN_PACK
    BF16_PROJECTIONS, BF16_OUTPUT = P.BF16_PROJECTIONS, P.BF16_OUTPUT
    CONTRACT = "tools/iq_pack.py"
except Exception:                      # noqa: BLE001 - no numpy: the reader's own copy
    ROUTERS = ("ffn_gate_inp.weight", "ffn_gate_inp_shexp.weight")
    NOT_IN_PACK = {"per_layer_token_embd.weight"}
    BF16_PROJECTIONS = ("hc_attn_down.weight", "hc_attn_up.weight", "hc_attn_inject.weight", "hc_ffn_down.weight",
                        "hc_ffn_up.weight", "hc_ffn_inject.weight", "ssm_alpha.weight", "ssm_beta.weight",
                        "indexer.q_proj.weight", "indexer.k_proj.weight", "ple_value.weight", *ROUTERS)
    BF16_OUTPUT = {"output_hc_down.weight", "output_hc_up.weight"}
    CONTRACT = "this tool's copy"
try:                                   # the engine's own contract, imported so the two cannot drift
    import native_check as NC           # noqa: E402
    CONTRACT_ENGINE = "tools/native_check.py"
except Exception:                      # noqa: BLE001 - a release install without the tools
    NC, CONTRACT_ENGINE = None, None
# What ggml-cpu has a vec_dot for: src/kernels/cpu/native_expert.cpp asks that of both expert encodings.
PLAUSIBLE = {"Q2_0", "IQ4_NL", "IQ3_XXS", "IQ2_XS", "IQ2_S", "IQ3_S", "IQ1_M", "IQ1_S", "Q8_0", "Q4_0", "Q5_0",
             "Q6_K", "Q4_K", "Q5_K", "Q3_K", "Q8_K", "BF16", "F16", "F32"}


def shards_of(first: pathlib.Path) -> list:
    """Every shard beside `first` (<name>-0000N-of-0000M.gguf), or just `first` when it is not split."""
    m = SHARD_RE.search(first.name)
    if not m:
        return [first]
    total = int(m.group(2))
    paths = [first.with_name("%s-%05d-of-%05d.gguf" % (first.name[:m.start()], i, total)) for i in range(1, total + 1)]
    missing = [p.name for p in paths if not p.is_file()]
    if missing:
        raise SystemExit("missing model shards: " + ", ".join(missing))
    return paths


def human(n: float) -> str:
    for u in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024 or u == "TiB":
            return "%.2f %s" % (n, u)
        n /= 1024
    return str(n)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gguf", required=True, help="the model's shard 1 (...-00001-of-0000M.gguf)")
    a = ap.parse_args()
    first = pathlib.Path(a.gguf).absolute()
    if not first.is_file():
        print("no such file: %s" % first)
        return 1
    m = SHARD_RE.search(first.name)
    if m and m.group(1) != "00001":
        print("note: that is shard %s, not 1: the engine's model_shards() derives the rest from -00001-of-.\n"
              % m.group(1))
    where, all_t, histogram, md = {}, [], {}, None
    print("=== the shards (contract: %s) ===" % CONTRACT)
    for p in shards_of(first):
        g = G.GGUFFile(p)
        md = md or g.metadata
        # src/core/native_dense.cpp requires every shard after the first to repeat shard 1's split.count and
        # split.tensors.count and to carry its own split.no: without them a split model cannot be served natively.
        splits = {k: g.metadata[k] for k in g.metadata if k.startswith("split.")}
        if splits:
            print("  split metadata: %s" % ", ".join("%s=%s" % kv for kv in sorted(splits.items())))
        real = 0
        for t in g.tensors:
            if t.name in where:
                print("  duplicate tensor %s in %s and %s - NO-GO" % (t.name, where[t.name][0].name, p.name))
                return 1
            where[t.name] = (p, t)
            all_t.append(t)
            real += t.expected_bytes() or 0
            e = histogram.setdefault(t.type_name, [0, 0])
            e[0] += 1
            e[1] += t.expected_bytes() or 0
        print("  %-48s %-10s v%d  tensors %4d  kv %3d  data %s"
              % (p.name, human(p.stat().st_size), g.version, len(g.tensors), len(g.metadata), human(real)))
    for k in sorted(md):
        if k in ("general.name", "general.architecture", "general.size_label", "general.file_type") or \
                k.endswith(("block_count", "expert_count", "expert_used_count")):
            print("  %-48s %s" % (k, md[k]))

    print("\n=== tensor types ===")
    for n, (c, b) in sorted(histogram.items(), key=lambda kv: -kv[1][1]):
        print("  %-9s %5d tensors  %s" % (n, c, human(b)))

    print("\n=== read from the GGUF by the engine (--native / --ple-gguf) ===")
    for n in [x for x in sorted(where) if x in NOT_IN_PACK or x in ("token_embd.weight", "output.weight")
              or x.startswith("blk.1.ple_key")]:
        p, t = where[n]
        print("  %-34s %-9s %-22s %s" % (n, t.type_name, "x".join(str(d) for d in t.shape), p.name))

    layers = sorted({int(t.name.split(".")[1]) for t in all_t if t.name.startswith("blk.")
                     and t.name.endswith("_exps.weight") and t.name.split(".")[1].isdigit()})
    r0 = where.get("blk.0.ffn_gate_inp.weight")
    n_expert = int(r0[1].shape[1]) if r0 else 0
    print("\n=== experts per layer (n_expert %d, %d layers) ===" % (n_expert, len(layers)))
    print("  layer  gate      up        down      blob bytes    shard")
    total, bad_gu, bad_shard, unknown, down_types = 0, [], [], [], set()
    for l in layers:
        got = [where.get("blk.%d.ffn_%s_exps.weight" % (l, r)) for r in ("gate", "up", "down")]
        if any(x is None for x in got):
            print("  %5d  MISSING %s - NO-GO" % (l, [r for r, x in zip(("gate", "up", "down"), got) if not x]))
            return 1
        ts = [x[1] for x in got]
        sizes = [t.expected_bytes() for t in ts]
        blob = 0 if any(x is None for x in sizes) else sum(x // n_expert for x in sizes)
        total += blob * n_expert
        if any(x is None for x in sizes):
            unknown.append(l)
        if ts[0].type_name != ts[1].type_name:
            bad_gu.append(l)
        if len({x[0].name for x in got}) != 1:
            bad_shard.append(l)
        down_types.add(ts[2].type_name)
        print("  %5d  %-9s %-9s %-9s %11d   %s" % (l, ts[0].type_name, ts[1].type_name, ts[2].type_name, blob,
                                                   got[0][0].name))
    print("  gate/up: %s   down: %s" % (", ".join(sorted({where["blk.%d.ffn_gate_exps.weight" % l][1].type_name
                                                         for l in layers})), ", ".join(sorted(down_types))))
    print("  experts.bin would be %s" % human(total))

    not_bf16 = [n for n, (p, t) in sorted(where.items())
                if t.type_name != "BF16" and (n in BF16_OUTPUT or
                                              (n.startswith("blk.") and (n.endswith(BF16_PROJECTIONS)
                                                                         or n == "blk.1.ple_key.weight")))]
    rn = [n for n in where if n.endswith(ROUTERS)]
    rtypes = sorted({where[n][1].type_name for n in rn})
    print("\n=== the kernels read these as BF16 ===")
    print("  not BF16 (%d): %s" % (len(not_bf16), ", ".join(not_bf16) if not_bf16 else "-"))
    print("  routers: %d tensors, %s" % (len(rn), ", ".join(rtypes) or "MISSING"))

    ple_paths = sorted({where[n][0] for n in where if n in NOT_IN_PACK})
    ple = [p.name for p in ple_paths]
    routers_ok = bool(rn) and set(rtypes) <= {"F32", "BF16"}
    ok = not (bad_gu or bad_shard or unknown) and bool(ple) and routers_ok
    print("\n=== checks ===")
    print("  gate == up type in every layer:      %s" % ("yes" if not bad_gu else "NO %s" % bad_gu))
    print("  gate/up/down in one shard per layer: %s" % ("yes" if not bad_shard else "NO %s" % bad_shard))
    print("  every expert tensor's size known:    %s" % ("yes" if not unknown else "NO %s" % unknown))
    print("  PLE table (--ple-gguf):              %s" % (", ".join(ple) or "NOT FOUND"))
    print("  routers are F32 or BF16:             %s" % ("yes" if routers_ok else "NO"))
    extra = sorted(x for x in down_types if x not in PLAUSIBLE)
    if extra:
        print("  down encodings outside the usual set: %s" % ", ".join(extra))
    # The engine's side of the contract: it reads token_embd.weight from the --native shard and the PLE table
    # from --ple-gguf, and exits a second after starting when either is wrong.  --native is what setup writes
    # (shard 1) and --ple-gguf is the shard the table is in, wherever that is.
    engine = NC.check(first, ple_paths[0] if ple_paths else None) if NC is not None else None
    if engine is not None:
        print("\n=== the engine's own checks (%s) ===" % CONTRACT_ENGINE)
        for n in engine.notes:
            print("  %s" % n)
        for p in engine.problems:
            print("  %s" % p)
        print("  the engine can load it:              %s" % ("yes" if engine.ok() else "NO"))
    elif not ple:
        print("\n=== the engine's own checks ===\n  skipped: %s is not here" % CONTRACT_ENGINE)
    print("\n%s" % ("GO: tools/iq_pack.py can pack this model, and the engine can load it"
                    + (" (add --compat-bf16: %d tensors are not BF16)" % len(not_bf16) if not_bf16 else "")
                    if ok and (engine is None or engine.ok()) else "NO-GO: see the failing checks above"))
    return 0 if (ok and (engine is None or engine.ok())) else 1


if __name__ == "__main__":
    sys.exit(main())
