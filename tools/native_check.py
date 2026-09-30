"""tools/native_check.py - can the engine load this model, or will it exit before READY?

    python tools/native_check.py --config strata-iq3_xxs-ud.json
    python tools/native_check.py --native <model>-00001-of-00003.gguf --ple-gguf <model>-00002-of-00003.gguf

A native (IQ) pack is served from the model's GGUF shards, not from the pack: the engine reads
`token_embd.weight` from the `--native` shard and the PLE table from the `--ple-gguf` shard, and it has no
fallback for either.  When one of the checks below fails the engine prints ONE line and exits 1 about a second
after it started, so the server reports "the engine exited before it was ready" and the app shows the model as
not loaded.  Nothing else says which of the checks it was.

  architecture  include/strata/artifact/gguf_reader.hpp:468  general.architecture == "qwen4exp" + the geometry
  token_embd    src/core/native_head.cpp:107                 in --native, 2-D [2560, 248320], an IQ encoding
  PLE table     src/kernels/ngram.cpp:141                    in --ple-gguf, IQ4_NL, rows of 160 values
  --ple-gguf    src/program/generate.cpp:1261                required unless --no-ple says it is an ablation

The IQ encodings are `is_iq()` in src/kernels/cuda/iq_kernels.cu:603.  A k-quant embedding has no kernel
there: the unsloth "UD" releases store `token_embd.weight` as Q6_K (their UD-Q4_K_XL files as Q8_0), so those
shards can be packed but cannot start the engine.  The releases whose `token_embd.weight` is an IQ encoding -
the GSQ-RCO files setup downloads, and OrcaRouter's IQ3_XXS - are the ones this engine runs.

Headers only, nothing written.  Exit code 0 when the engine can load the model, 1 with the failing check when
it cannot.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import pathlib
import re
import struct
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import gguf_reader as G  # noqa: E402

# The engine's constants: one architecture, one geometry, and a vocabulary generate.cpp hands to
# NativeEmbed::load rather than reading it from the file.
ARCH = "qwen4exp"
N_EMBD, N_VOCAB = 2560, 248320                    # src/program/generate.cpp:1374 (g0.n_embd, 248320)
PLE_HEAD_DIM = 160                                # include/strata/kernels/ngram.hpp:32
GEOMETRY = (("qwen4exp.block_count", 48), ("qwen4exp.embedding_length", N_EMBD),
            ("qwen4exp.attention.head_count", 24), ("qwen4exp.attention.head_count_kv", 2))
PRESENT_ONLY = ("qwen4exp.expert_count", "qwen4exp.expert_used_count")   # 0 in the guard: pruned variants ship less
# is_iq() in src/kernels/cuda/iq_kernels.cu:603, spelled as tools/gguf_reader.py spells those type ids
IQ_TYPES = ("Q2_0", "Q3_K", "IQ1_M", "IQ2_XXS", "IQ2_XS", "IQ2_S", "IQ3_XXS", "IQ3_S", "IQ4_NL", "IQ4_XS")
IQ_SOURCE = "is_iq() in src/kernels/cuda/iq_kernels.cu:603"
EMBED_MESSAGE = ("native embedding: token_embd.weight is absent, of another shape, or of a type without a GPU "
                 "dequantizer")
REMEDY = ("this engine runs models whose token_embd.weight is an IQ encoding - the GSQ-RCO releases setup "
          "downloads and OrcaRouter's IQ3_XXS (docs/ORCA.md) - not a k-quant embedding")
SHARD_RE = re.compile(r"^(?P<stem>.+)-(?P<no>\d{5})-of-(?P<count>\d{5})\.gguf$", re.IGNORECASE)


@dataclasses.dataclass
class Report:
    """What is wrong (problems) and what was seen (notes); no problems means the engine can load the model."""

    problems: list[str] = dataclasses.field(default_factory=list)
    notes: list[str] = dataclasses.field(default_factory=list)

    def ok(self) -> bool:
        return not self.problems


def shard_set(first: pathlib.Path) -> list[pathlib.Path]:
    """Every shard beside `first` (<stem>-0000N-of-0000M.gguf), or just `first` when its name is not a split
    one - the same rule as setup.shard_set and tools/gguf_inventory.py."""
    m = SHARD_RE.match(first.name)
    if not m:
        return [first]
    count = int(m.group("count"))
    return [first.with_name("%s-%05d-of-%05d.gguf" % (m.group("stem"), i, count)) for i in range(1, count + 1)]


def value_of(args, flag: str):
    """The value after `flag` in an engine argument list, or None when the flag is not there."""
    for i, a in enumerate(args):
        if a == flag and i + 1 < len(args):
            return args[i + 1]
    return None


def resolved(path, cwd):
    """A config's argument path, resolved against the config's cwd exactly as the engine resolves it (one file
    per flag, whatever the shard count - --native takes a single shard)."""
    p = pathlib.Path(path)
    return p if p.is_absolute() or not cwd else pathlib.Path(cwd) / p


def _header(path: pathlib.Path):
    """The file's metadata and tensor directory, or None when it cannot be read (headers only, never data)."""
    try:
        return G.GGUFFile(path)
    except (OSError, ValueError, KeyError, IndexError, struct.error):
        return None


def _tensor_in(path: pathlib.Path, name: str):
    g = _header(path)
    if g is None:
        return None
    return next((t for t in g.tensors if t.name == name), None)


def _shard_with(shards, name: str):
    """The shard holding `name`, or None.  Shard 1 of these models can be a header alone with no tensors."""
    for s in shards:
        if s.is_file() and _tensor_in(s, name) is not None:
            return s
    return None


def _ple_table(ple, r: Report) -> None:
    """The PLE table's checks: src/kernels/ngram.cpp:141, and --ple-gguf itself (src/program/generate.cpp:1261)."""
    if ple is None:
        r.problems.append("no --ple-gguf PATH: the engine refuses to start without it (src/program/generate.cpp:1261;"
                          " the table is not in the pack - tools/iq_pack.py NOT_IN_PACK)")
        return
    ple = pathlib.Path(ple)
    if not ple.is_file():
        r.problems.append("there is no such file: %s (--ple-gguf)" % ple)
        return
    pg = _header(ple)
    table = None if pg is None else next((t for t in pg.tensors if t.name == "per_layer_token_embd.weight"), None)
    before = len(r.problems)
    if pg is None:
        r.problems.append("%s is not a GGUF header the engine can read" % ple.name)
    elif table is None:
        holder = _shard_with(shard_set(ple), "per_layer_token_embd.weight")
        r.problems.append("per_layer_token_embd.weight is not in the --ple-gguf shard (%s)" % ple.name)
        if holder is not None:
            r.problems.append("    it is in %s: that file is the one --ple-gguf needs" % holder.name)
    else:
        if len(table.shape) != 2 or table.shape[0] != PLE_HEAD_DIM:
            r.problems.append("per_layer_token_embd.weight is %s; a row is %d values (src/kernels/ngram.cpp:148)"
                              % (table.shape, PLE_HEAD_DIM))
        if table.type_name != "IQ4_NL":
            r.problems.append("per_layer_token_embd.weight is %s, not IQ4_NL (src/kernels/ngram.cpp:153)"
                              % table.type_name)
        if len(r.problems) == before:
            r.notes.append("per_layer_token_embd.weight %s %s in %s: the PLE table loads"
                           % (table.type_name, table.shape, ple.name))


def check_config(cfg: dict) -> Report:
    """The same checks for a server/engine config (the JSON setup.py writes: exe, args, cwd, ...)."""
    args = list(cfg.get("args") or [])
    cwd = cfg.get("cwd") or "."
    got = [value_of(args, f) for f in ("--native", "--ple-gguf", "--pack")]
    return check(*[resolved(p, cwd) if p else None for p in got])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", help="a strata engine config JSON (exe, args, cwd, ...), as the start scripts pass it")
    ap.add_argument("--native", help="the shard the engine reads the embedding from (--native in the config)")
    ap.add_argument("--ple-gguf", help="the shard holding per_layer_token_embd.weight (--ple-gguf in the config)")
    ap.add_argument("--pack", help="the pack directory (--pack), to tell a native pack from the canonical one")
    a = ap.parse_args(argv)
    cwd, native, ple, pack = None, a.native, a.ple_gguf, a.pack
    if a.config:
        try:
            cfg = json.loads(pathlib.Path(a.config).read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as e:
            print("cannot read %s: %s" % (a.config, e))
            return 1
        args = list(cfg.get("args") or [])
        cwd = cfg.get("cwd")
        native = native or value_of(args, "--native")
        ple = ple or value_of(args, "--ple-gguf")
        pack = pack or value_of(args, "--pack")
        print("config %s" % a.config)
        print("  exe  %s" % cfg.get("exe"))
        print("  args %s" % (" ".join(args) or "(none)"))
    if not (native or ple or pack):
        ap.error("nothing to check: give --config, or --native and --ple-gguf (and --pack if you have one)")
    r = check(*[resolved(p, cwd) if p else None for p in (native, ple, pack)])
    print()
    for n in r.notes:
        print("  %s" % n)
    for p in r.problems:
        print("  %s" % p)
    print()
    if r.ok():
        print("GO: the engine can load this model")
        return 0
    print("NO-GO: the engine would exit about a second after starting, before READY - %s" % REMEDY)
    return 1


def check(native=None, ple=None, pack=None, n_embd: int = N_EMBD, n_vocab: int = N_VOCAB) -> Report:
    """Every way the engine refuses this model, in the engine's own terms.  Empty `problems` == it will load.

    `n_embd`/`n_vocab` are the two values the engine compares the embedding's shape against; they are arguments
    only so a test can use a synthetic model instead of a 521 MB tensor.
    """
    r = Report()
    if native is None:
        # a native pack cannot start without --native (generate.cpp:1367); the canonical Q2_0 pack does not need one
        if pack is not None and (pathlib.Path(pack) / "native_experts.txt").exists():
            r.problems.append("no --native SHARD, and this is a native (IQ) pack: the engine reads "
                              "token_embd.weight from --native (src/program/generate.cpp:1367)")
        else:
            r.notes.append("no --native SHARD: the embedding is read from the pack, not from a GGUF")
        return r
    native = pathlib.Path(native)
    if not native.is_file():
        r.problems.append("there is no such file: %s (--native)" % native)
        return r
    g = _header(native)
    if g is None:
        r.problems.append("%s is not a GGUF header the engine can read" % native.name)
        return r

    # The metadata lives in shard 1 (<stem>-00001-of-): the other shards carry none, so the guard is read there.
    shards = shard_set(native)
    meta, home = g.metadata, native
    if "general.architecture" not in meta:
        for s in shards:
            if s != native and s.is_file():
                other = _header(s)
                if other is not None and "general.architecture" in other.metadata:
                    meta, home = other.metadata, s
                    break
    arch = meta.get("general.architecture")
    if arch is None:
        r.problems.append("no general.architecture in %s or in its shard 1 (include/strata/artifact/"
                          "gguf_reader.hpp:468)" % native.name)
    elif arch != ARCH:
        r.problems.append("architecture is '%s', this engine requires '%s' (include/strata/artifact/"
                          "gguf_reader.hpp:471)" % (arch, ARCH))
    else:
        r.notes.append("architecture %s, read from %s" % (ARCH, home.name))
        for key, want in GEOMETRY:
            got = meta.get(key)
            if got is None:
                r.problems.append("missing %s, which the engine requires to be %d" % (key, want))
            elif got != want:
                r.problems.append("%s = %s, expected %d (include/strata/artifact/gguf_reader.hpp:484)"
                                  % (key, got, want))
        r.problems += ["missing %s (the guard reads its presence)" % k for k in PRESENT_ONLY if k not in meta]

    # ---- the native embedding: the check that stops these models, src/core/native_head.cpp:107.  Only a native
    # (IQ) pack reads it from the GGUF: a canonical pack carries token_embd.weight itself (generate.cpp:1396
    # skips the pack's copy only when the expert layout says the pack is native).  A canonical pack is one with an
    # index.txt and no native_experts.txt; a pack we cannot see keeps the check, because the embedding is what
    # decides whether the engine starts.
    packdir = pathlib.Path(pack) if pack else None
    canonical = (packdir is not None and (packdir / "index.txt").exists()
                 and not (packdir / "native_experts.txt").exists())
    before = len(r.problems)
    embed = _tensor_in(native, "token_embd.weight")
    if canonical:
        r.notes.append("the pack has no native_experts.txt (a canonical pack): the engine reads token_embd.weight "
                       "from the pack, not from --native")
    elif embed is None:
        # which shard does hold it, when one does: the fix is to put THAT file in the config as --native
        holder = _shard_with(shards, "token_embd.weight")
        r.problems.append("token_embd.weight is not in the --native shard (%s holds %d tensors)"
                          % (native.name, len(g.tensors)))
        r.problems.append("    it is in %s: that file is the one --native needs" % holder.name if holder is not None
                          else "    no shard of this model has token_embd.weight")
        r.notes.append("the engine's line: %s" % EMBED_MESSAGE)
    else:
        if len(embed.shape) != 2 or embed.shape[0] != n_embd or embed.shape[1] != n_vocab:
            r.problems.append("token_embd.weight is %s, not [%d, %d] (src/core/native_head.cpp:113)"
                              % (embed.shape, n_embd, n_vocab))
        if embed.type_name not in IQ_TYPES:
            r.problems.append("token_embd.weight is %s, and the GPU dequantizer has no kernel for it (%s takes %s)"
                              % (embed.type_name, IQ_SOURCE, ", ".join(IQ_TYPES)))
        if n_embd % 256:
            r.problems.append("n_embd %d is not a multiple of 256 (src/core/native_head.cpp:114)" % n_embd)
        if len(r.problems) == before:
            r.notes.append("token_embd.weight %s %s in %s: the embedding loads"
                           % (embed.type_name, embed.shape, native.name))
    out = _tensor_in(native, "output.weight")
    if out is not None:
        r.notes.append("output.weight %s %s in %s (the head is read from the same shard)"
                       % (out.type_name, out.shape, native.name))

    _ple_table(ple, r)
    return r


if __name__ == "__main__":
    sys.exit(main())

