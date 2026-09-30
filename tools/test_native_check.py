"""tools/native_check.py's checks, over synthetic GGUFs (no model, no GPU, no download):

    python -m unittest tools.test_native_check

The cases are the ways a model stops the engine a second after it starts: the wrong architecture, a --native
shard without token_embd.weight (the unsloth UD splits keep shard 1 as a header alone), an embedding whose
encoding has no GPU dequantizer (those releases store it as Q6_K or Q8_0), and a --ple-gguf shard without the
table.  The last test parses is_iq() out of the CUDA source, so the two lists cannot drift apart.
"""
from __future__ import annotations

import contextlib
import io
import json
import re
import struct
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT))
import gguf_reader  # noqa: E402
import native_check as NC  # noqa: E402

QWEN4EXP = (("general.architecture", "qwen4exp"), ("qwen4exp.block_count", 48),
            ("qwen4exp.embedding_length", 2560), ("qwen4exp.expert_count", 512),
            ("qwen4exp.expert_used_count", 10), ("qwen4exp.attention.head_count", 24),
            ("qwen4exp.attention.head_count_kv", 2))
Q6_K, IQ3_XXS, IQ4_NL = 14, 18, 20          # the ggml type ids tools/gguf_reader.py spells those names with
EMBED = ("token_embd.weight", 2560, 248320)  # the shape the engine demands (src/core/native_head.cpp:113)


def write_gguf(path: Path, meta=(), tensors=()) -> Path:
    """A GGUF v3 header: (key, str|int) metadata and (name, type_id, shape) tensors with no real data.

    Every reader here looks at the header alone, so one alignment's worth of zero bytes per tensor is enough.
    """
    b = bytearray(struct.pack("<IIQQ", 0x46554747, 3, len(tensors), len(meta)))
    for key, value in meta:
        b += struct.pack("<Q", len(key)) + key.encode()
        if isinstance(value, str):
            b += struct.pack("<I", 8) + struct.pack("<Q", len(value)) + value.encode()
        else:
            b += struct.pack("<II", 4, value)
    offset = 0
    for name, type_id, shape in tensors:
        b += struct.pack("<Q", len(name)) + name.encode()
        b += struct.pack("<I", len(shape)) + struct.pack("<%dQ" % len(shape), *shape)
        b += struct.pack("<IQ", type_id, offset)
        offset += 32
    b += bytes(-len(b) % 32 + 32 * len(tensors))
    path.write_bytes(bytes(b))
    return path


class NativeChecks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        # the supported shape of a model: shard 1 with the metadata and the embedding, shard 2 with the table
        self.good1 = write_gguf(self.dir / "good-00001-of-00002.gguf", QWEN4EXP,
                                (("token_embd.weight", IQ3_XXS, (EMBED[1], EMBED[2])),
                                 ("output.weight", IQ3_XXS, (EMBED[1], EMBED[2]))))
        self.good2 = write_gguf(self.dir / "good-00002-of-00002.gguf", (("split.count", 2),),
                                (("per_layer_token_embd.weight", IQ4_NL, (160, 320001536)),))
        # what the unsloth UD splits look like: shard 1 is a header with no tensors, and the embedding is Q6_K
        self.ud1 = write_gguf(self.dir / "ud-00001-of-00003.gguf", QWEN4EXP)
        self.ud2 = write_gguf(self.dir / "ud-00002-of-00003.gguf", (("split.count", 3),),
                              (("token_embd.weight", Q6_K, (EMBED[1], EMBED[2])),
                               ("output.weight", Q6_K, (EMBED[1], EMBED[2])),
                               ("per_layer_token_embd.weight", IQ4_NL, (160, 320001536)),))

    def tearDown(self):
        self.tmp.cleanup()

    # ---- the shape of a model the engine runs -----------------------------------
    def test_a_supported_model_passes(self):
        r = NC.check(self.good1, self.good2)
        self.assertEqual(r.problems, [])
        self.assertTrue(any("the embedding loads" in n for n in r.notes))
        self.assertTrue(any("the PLE table loads" in n for n in r.notes))

    def test_the_metadata_is_read_from_shard_1_when_the_native_shard_has_none(self):
        # --native can be shard 2 (its tensors are there) while the metadata only exists in shard 1
        r = NC.check(self.ud2, self.ud2)
        self.assertIn("architecture qwen4exp, read from ud-00001-of-00003.gguf", " ".join(r.notes))

    # ---- the failures this tool exists for --------------------------------------
    def test_native_shard_without_the_embedding_names_the_shard_that_has_it(self):
        r = NC.check(self.ud1, self.ud2)
        text = " ".join(r.problems)
        self.assertIn("token_embd.weight is not in the --native shard", text)
        self.assertIn("ud-00001-of-00003.gguf holds 0 tensors", text)
        self.assertIn("it is in ud-00002-of-00003.gguf", text)
        self.assertIn(NC.EMBED_MESSAGE, " ".join(r.notes))        # the line the engine itself prints
        self.assertNotIn("per_layer_token_embd", text)            # the PLE shard is right: do not blame it

    def test_k_quant_embedding_has_no_gpu_dequantizer(self):
        r = NC.check(self.ud2, self.ud2)
        text = " ".join(r.problems)
        self.assertIn("token_embd.weight is Q6_K", text)
        self.assertIn(NC.IQ_SOURCE, text)
        self.assertIn("Q2_0", text)                              # what it would take is named
        self.assertIn("IQ4_NL", text)
        self.assertFalse(r.ok())

    def test_wrong_architecture_is_the_guards_message(self):
        other = write_gguf(self.dir / "other-00001-of-00002.gguf", (("general.architecture", "qwen3next"),))
        r = NC.check(other, self.good2)
        self.assertIn("architecture is 'qwen3next', this engine requires 'qwen4exp'", " ".join(r.problems))

    def test_wrong_geometry_is_named_with_its_source(self):
        meta = tuple((k, 2048 if k == "qwen4exp.embedding_length" else v) for k, v in QWEN4EXP)
        bad = write_gguf(self.dir / "geo-00001-of-00002.gguf", meta,
                         (("token_embd.weight", IQ3_XXS, (EMBED[1], EMBED[2])),))
        r = NC.check(bad, self.good2)
        self.assertIn("qwen4exp.embedding_length = 2048, expected 2560", " ".join(r.problems))

    def test_embedding_of_another_shape_and_a_missing_ple(self):
        bad = write_gguf(self.dir / "shape-00001-of-00002.gguf", QWEN4EXP,
                         (("token_embd.weight", IQ3_XXS, (2560, 151936)),))
        text = " ".join(NC.check(bad, bad).problems)
        self.assertIn("token_embd.weight is [2560, 151936], not [2560, 248320]", text)
        self.assertIn("per_layer_token_embd.weight is not in the --ple-gguf shard", text)

    def test_ple_of_another_encoding_is_refused(self):
        bad = write_gguf(self.dir / "ple-00002-of-00002.gguf", (("split.count", 2),),
                         (("per_layer_token_embd.weight", Q6_K, (160, 320001536)),))
        self.assertIn("per_layer_token_embd.weight is Q6_K, not IQ4_NL",
                      " ".join(NC.check(self.good1, bad).problems))

    def test_a_native_pack_without_native_is_refused(self):
        pack = self.dir / "pack"
        pack.mkdir()
        (pack / "native_experts.txt").write_text("# strata native experts v3\n", encoding="utf-8")
        self.assertIn("no --native SHARD", " ".join(NC.check(None, self.good2, pack).problems))

    def test_a_native_pack_needs_the_embedding_in_native(self):
        pack = self.dir / "nativepack"
        pack.mkdir()
        (pack / "native_experts.txt").write_text("# strata native experts v3\n", encoding="utf-8")
        self.assertIn("not in the --native shard", " ".join(NC.check(self.ud1, self.ud2, pack).problems))

    def test_a_canonical_pack_reads_the_embedding_from_the_pack(self):
        pack = self.dir / "canonical"                 # index.txt and no native_experts.txt: setup's Q2_0 pack
        pack.mkdir()
        (pack / "index.txt").write_text("# strata pack index v3\n", encoding="utf-8")
        r = NC.check(self.ud1, self.ud2, pack)        # shard 1 holds no tensors at all
        self.assertEqual(r.problems, [])
        self.assertTrue(any("canonical pack" in n for n in r.notes))

    def test_no_ple_gguf_is_refused(self):
        self.assertIn("no --ple-gguf PATH", " ".join(NC.check(self.good1, None).problems))

    # ---- what the start scripts and the server call -------------------------------
    def test_config_paths_are_resolved_against_the_config_cwd(self):
        cfg = {"exe": "strata.exe", "cwd": str(self.dir), "args": [
            "--pack", "pack", "--native", self.ud1.name, "--ple-gguf", self.ud2.name]}
        self.assertIn("ud-00001-of-00003.gguf holds 0 tensors", " ".join(NC.check_config(cfg).problems))

    def test_cli_verdict_and_exit_code(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(NC.main(["--native", str(self.ud1), "--ple-gguf", str(self.ud2)]), 1)
        self.assertIn("NO-GO", out.getvalue())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(NC.main(["--native", str(self.good1), "--ple-gguf", str(self.good2)]), 0)
        self.assertIn("GO: the engine can load this model", out.getvalue())

    def test_config_json_is_read(self):
        cfg = self.dir / "strata-good.json"
        cfg.write_text(json.dumps({"exe": "strata.exe", "args": ["--native", str(self.good1),
                                                                 "--ple-gguf", str(self.good2)]}),
                       encoding="utf-8")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(NC.main(["--config", str(cfg)]), 0)
        self.assertIn("strata-good.json", out.getvalue())

    def test_shard_set_lists_every_shard(self):
        self.assertEqual([p.name for p in NC.shard_set(self.ud1)],
                         ["ud-00001-of-00003.gguf", "ud-00002-of-00003.gguf", "ud-00003-of-00003.gguf"])
        self.assertEqual([p.name for p in NC.shard_set(self.good1)],
                         ["good-00001-of-00002.gguf", "good-00002-of-00002.gguf"])

    def test_iq_types_match_the_cuda_source(self):
        cu = ROOT / "src" / "kernels" / "cuda" / "iq_kernels.cu"
        if not cu.is_file():
            self.skipTest("a release install has no CUDA source to compare with")
        line = next((l for l in cu.read_text(encoding="utf-8").splitlines() if l.startswith("bool is_iq(int t)")), "")
        self.assertTrue(line, "is_iq() moved: update native_check.IQ_TYPES and this test")
        ids = sorted(int(x) for x in re.findall(r"t == (\d+)", line))
        self.assertEqual(ids, sorted(i for i, n in gguf_reader.GGML_TYPES.items() if n in NC.IQ_TYPES))


if __name__ == "__main__":
    unittest.main()
