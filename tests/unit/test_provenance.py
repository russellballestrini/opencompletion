"""provenance.py is vendored from uncloseai-cli (merkle-v1 &
unfirehose-chain-v1). These replay the known answers shared with
arborist, unfirehose (TypeScript) & unfirehose-sdks (Go), copied verbatim
into tests/fixtures/provenance/, so a drifted copy fails here instead of
agreeing with itself."""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

import provenance as P  # noqa: E402

FIX = ROOT / "tests" / "fixtures" / "provenance"


def records(name, kind=None):
    out = []
    for line in (FIX / name).read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        rec = json.loads(line)
        if kind is None or rec["kind"] == kind:
            out.append(rec)
    return out


def test_merkle_version_matches_the_vectors():
    assert {r["version"] for r in records("merkle-kat.jsonl")} == {P.MERKLE_VERSION}


@pytest.mark.parametrize(
    "r", records("merkle-kat.jsonl", "leaf"), ids=lambda r: r["label"]
)
def test_leaf_vectors(r):
    assert P.hash_leaf(bytes.fromhex(r["content_hex"])).hex() == r["leaf_hash_hex"]


@pytest.mark.parametrize(
    "r", records("merkle-kat.jsonl", "combine"), ids=lambda r: r["label"]
)
def test_combine_vectors_are_non_commutative(r):
    left, right = bytes.fromhex(r["left_hex"]), bytes.fromhex(r["right_hex"])
    assert P.hash_combine(left, right).hex() == r["combined_hex"]
    if left != right:
        assert P.hash_combine(right, left).hex() != r["combined_hex"]


@pytest.mark.parametrize(
    "r", records("merkle-kat.jsonl", "tree"), ids=lambda r: r["label"]
)
def test_tree_vectors(r):
    tree = P.MerkleTree.build(bytes.fromhex(h) for h in r["leaf_hashes_hex"])
    assert tree.root.hex() == r["root_hex"]
    assert [len(layer) for layer in tree.layers] == r["layer_widths"]


@pytest.mark.parametrize(
    "r", records("merkle-kat.jsonl", "proof"), ids=lambda r: r["label"]
)
def test_merkle_proof_vectors(r):
    proof = P.proof_from_dict(
        {
            "leaf": r["leaf_hex"],
            "leaf_index": r["leaf_index"],
            "siblings": r["siblings"],
            "root": r["root_hex"],
        }
    )
    assert P.verify_proof(proof) == r["expect_valid"]


@pytest.mark.parametrize(
    "r", records("chain-kat.jsonl", "line"), ids=lambda r: r["label"]
)
def test_chain_line_vectors(r):
    pre = r["preimage"].encode("utf-8")
    assert P.line_hash(pre) == r["hash"]
    assert P.split_chained_line(r["line"].encode("utf-8")) == (pre, r["hash"])
    entry = json.loads(pre)
    body = {k: v for k, v in entry.items() if k != "prevHash"}
    assert P.chain_line(body, r["prev"])[0] == r["line"].encode("utf-8")


@pytest.mark.parametrize(
    "r", records("chain-kat.jsonl", "session"), ids=lambda r: r["label"]
)
def test_chain_session_vectors(r):
    report = P.verify_lines(line.encode("utf-8") for line in r["lines"])
    for key, want in r["expect"].items():
        assert report[key] == want, key


def test_every_verdict_is_exercised():
    states = {r["expect"]["state"] for r in records("chain-kat.jsonl", "session")}
    assert states == {P.UNCHAINED, P.OPEN, P.VERIFIED, P.CORRUPTED}


@pytest.mark.parametrize(
    "r", records("chain-kat.jsonl", "proof"), ids=lambda r: r["label"]
)
def test_chain_proof_vectors(r):
    proof = P.proof_from_dict(r["proof"])
    assert proof.leaf == P.hash_leaf(bytes.fromhex(r["line_hash"]))
    assert P.verify_proof(proof) == r["expect_valid"]
