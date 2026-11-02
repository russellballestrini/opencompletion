"""Provenance chain & Merkle roots, VENDORED: never edit here.

Lines below the marker are uncloseai-cli's provenance.py, lines 71-386 at
commit b862bb1, verbatim (merkle-v1 & unfirehose-chain-v1, both from
arborist's merkle.py; design: uncloseai-cli docs/LANE-PROVENANCE.md). The
same rules run in unfirehose (TypeScript) & unfirehose-sdks (Go);
tests/fixtures/provenance/*-kat.jsonl are their shared known answers.
Refresh by re-copying that range; tests/unit/test_provenance.py replays
both files, so a drifted copy fails instead of agreeing with itself.
Our chain of a room's events is chain.py.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Iterable

# ── verbatim from uncloseai-cli provenance.py:71-386 (b862bb1) ──────

LEAF_PREFIX = b"\x00"
NODE_PREFIX = b"\x03"
ZERO_HASH = b"\x00" * 32
HASH_LEN = 32

# Names the wire conventions, not the Python. Any change to a prefix
# byte, the odd-element rule, or sibling ordering produces different
# roots for the same input and MUST bump this — merkle-kat.jsonl
# records it.
MERKLE_VERSION = "merkle-v1"


def _sha256(*parts: bytes) -> bytes:
    h = hashlib.sha256()
    for p in parts:
        h.update(p)
    return h.digest()


def hash_leaf(content: bytes) -> bytes:
    """Hash a leaf with domain prefix 0x00."""
    return _sha256(LEAF_PREFIX, content)


def hash_combine(left: bytes, right: bytes) -> bytes:
    """Non-commutative interior combine with domain prefix 0x03."""
    if len(left) != HASH_LEN or len(right) != HASH_LEN:
        raise ValueError("hash inputs must be 32 bytes")
    return _sha256(NODE_PREFIX, left, right)


@dataclass(frozen=True)
class ProofNode:
    """One sibling step. is_left=True: the sibling sits to the LEFT of
    the running hash, so the step is HashCombine(sibling, current)."""

    hash: bytes
    is_left: bool


@dataclass(frozen=True)
class MerkleProof:
    leaf: bytes
    leaf_index: int
    siblings: tuple[ProofNode, ...]
    root: bytes


@dataclass
class MerkleTree:
    """Layered tree. layers[0] = leaves, layers[-1] = [root]."""

    layers: list[list[bytes]] = field(default_factory=list)

    @property
    def root(self) -> bytes:
        if not self.layers or not self.layers[-1]:
            return ZERO_HASH
        return self.layers[-1][0]

    @property
    def leaves(self) -> list[bytes]:
        return self.layers[0] if self.layers else []

    @classmethod
    def build(cls, leaves: Iterable[bytes]) -> MerkleTree:
        """Build from leaf HASHES (already domain-prefixed)."""
        leaves = list(leaves)
        if not leaves:
            return cls(layers=[[]])
        layers: list[list[bytes]] = [list(leaves)]
        current = list(leaves)
        while len(current) > 1:
            nxt: list[bytes] = []
            for i in range(0, len(current), 2):
                left = current[i]
                right = current[i + 1] if i + 1 < len(current) else current[i]
                nxt.append(hash_combine(left, right))
            layers.append(nxt)
            current = nxt
        return cls(layers=layers)

    def proof(self, leaf_index: int) -> MerkleProof:
        if not self.layers or not self.layers[0]:
            raise IndexError("empty tree has no proofs")
        if leaf_index < 0 or leaf_index >= len(self.layers[0]):
            raise IndexError(f"leaf_index {leaf_index} out of range")
        siblings: list[ProofNode] = []
        idx = leaf_index
        for layer in self.layers[:-1]:
            if idx % 2 == 0:
                sibling_idx, is_left = idx + 1, False
            else:
                sibling_idx, is_left = idx - 1, True
            if sibling_idx >= len(layer):
                sibling_idx = idx          # odd-element rule: self-duplicate
            siblings.append(ProofNode(hash=layer[sibling_idx], is_left=is_left))
            idx //= 2
        return MerkleProof(leaf=self.layers[0][leaf_index], leaf_index=leaf_index,
                           siblings=tuple(siblings), root=self.root)


def verify_proof(proof: MerkleProof) -> bool:
    current = proof.leaf
    for node in proof.siblings:
        current = (hash_combine(node.hash, current) if node.is_left
                   else hash_combine(current, node.hash))
    return current == proof.root


def proof_to_dict(proof: MerkleProof) -> dict:
    return {
        "leaf": proof.leaf.hex(),
        "leaf_index": proof.leaf_index,
        "siblings": [{"hash": s.hash.hex(), "is_left": s.is_left}
                     for s in proof.siblings],
        "root": proof.root.hex(),
    }


def proof_from_dict(d: dict) -> MerkleProof:
    return MerkleProof(
        leaf=bytes.fromhex(d["leaf"]),
        leaf_index=int(d["leaf_index"]),
        siblings=tuple(ProofNode(hash=bytes.fromhex(s["hash"]),
                                 is_left=bool(s["is_left"]))
                       for s in d["siblings"]),
        root=bytes.fromhex(d["root"]),
    )


# ── unfirehose-chain-v1: the line rule ───────────────────────────

CHAIN_VERSION = "unfirehose-chain-v1"
# The preimage is the exact on-disk bytes of the line: no canonical JSON
# anywhere in this chain (P-LANE-05).
ENCODING_VERSION = "jsonl-bytes-v1"
# What a root over line hashes commits to. SEQUENCE: every leaf is a
# chained event hash, so order and multiplicity are part of the identity;
# the same lines in another order are another root (P-LANE-06).
ROOT_SEMANTICS_SEQUENCE = "SEQUENCE"


def version_fields() -> dict:
    """The identity of the rules a record was written under; goes on the
    session header and the closed record next to `sessionRoot`."""
    return {"hashVersion": CHAIN_VERSION, "merkleVersion": MERKLE_VERSION,
            "encodingVersion": ENCODING_VERSION,
            "rootSemantics": ROOT_SEMANTICS_SEQUENCE}
_HASH_KEY = b',"hash":"'
_TAIL_LEN = len(_HASH_KEY) + 64 + 2      # ,"hash":"<64 hex>"}  → 75
_HEX64 = re.compile(rb'^[0-9a-f]{64}$')


def chain_line(entry: dict, prev: str | None, *, default=None) -> tuple[bytes, str]:
    """Serialize one chained line. Returns (line_bytes_without_newline, hash).

    The writer owns `prevHash` and `hash`: whatever the caller put
    under those keys is discarded, so a false binding cannot be
    smuggled in and a replayed row re-chains to THIS sequence.
    """
    body = {k: v for k, v in entry.items() if k not in ("hash", "prevHash")}
    body["prevHash"] = prev
    text = json.dumps(body, ensure_ascii=False, default=default)
    preimage = text.encode("utf-8", errors="replace")
    digest = hashlib.sha256(preimage).hexdigest()
    return preimage[:-1] + _HASH_KEY + digest.encode() + b'"}', digest


def split_chained_line(line: bytes) -> tuple[bytes, str] | None:
    """(preimage, claimed_hash) for a line in chain-v1 shape, else None.

    Shape only — a line whose tail parses but whose hash is wrong
    still splits; deciding is `verify_lines`' job.
    """
    line = line.rstrip(b"\r\n")
    if len(line) < _TAIL_LEN + 1 or not line.endswith(b'"}'):
        return None
    tail = line[-_TAIL_LEN:]
    if not tail.startswith(_HASH_KEY):
        return None
    digest = tail[len(_HASH_KEY):-2]
    if not _HEX64.match(digest):
        return None
    return line[:-_TAIL_LEN] + b"}", digest.decode()


def line_hash(preimage: bytes) -> str:
    return hashlib.sha256(preimage).hexdigest()


def file_root(path: str) -> tuple[str, int]:
    """merkle-v1 root over a JSONL file's lines in file order (each
    leaf = hash_leaf(line bytes, newline stripped); blank lines
    skipped) and the line count. One rule for the fixture manifest
    and the archive sidecar: a file's identity is its bytes, chained
    or not. Semantics: SEQUENCE — order and duplicates both count."""
    leaves = []
    with open(path, "rb") as fh:
        for line in fh:
            line = line.rstrip(b"\r\n")
            if line.strip():
                leaves.append(hash_leaf(line))
    return MerkleTree.build(leaves).root.hex(), len(leaves)


def session_root(hashes_hex: Iterable[str]) -> str:
    """merkle-v1 root over line hashes (as 32-byte leaves), hex."""
    leaves = [hash_leaf(bytes.fromhex(h)) for h in hashes_hex]
    return MerkleTree.build(leaves).root.hex()


# ── The verifier ─────────────────────────────────────────────────

# Verdicts, shared with the TypeScript verifier word for word.
UNCHAINED = "unchained"     # no line carried a hash — an older writer
OPEN = "open"               # chained so far, no closed record yet
VERIFIED = "verified"       # closed record present, root matched, no break
CORRUPTED = "corrupted"     # a break anywhere, or the root disagreed


def _is_closed_record(entry) -> bool:
    return (isinstance(entry, dict) and entry.get("type") == "session"
            and entry.get("status") == "closed")


@dataclass
class ChainState:
    """Incremental verifier state — one per session or per lane writer.

    Feed lines in order with `feed`; read `verdict` at any point. The
    TypeScript ingester persists this shape per session between
    incremental passes, so every field is plain data.
    """

    entries: int = 0
    hashed: int = 0
    breaks: int = 0
    first_break: int | None = None
    first_break_reason: str | None = None
    last_hash: str | None = None
    leaves: list[str] = field(default_factory=list)
    root_expected: str | None = None
    root_computed: str | None = None
    root_seq: int | None = None
    hash_version: str | None = None

    def _break(self, seq: int, reason: str):
        self.breaks += 1
        if self.first_break is None:
            self.first_break = seq
            self.first_break_reason = reason

    def feed(self, line: bytes):
        seq = self.entries
        self.entries += 1
        split = split_chained_line(line)
        if split is None:
            if self.hashed:
                self._break(seq, "unchained_line")
            return
        preimage, claimed = split
        try:
            entry = json.loads(preimage)
        except ValueError:
            entry = None
        self.hashed += 1
        if line_hash(preimage) != claimed:
            self._break(seq, "hash_mismatch")
        elif not isinstance(entry, dict):
            self._break(seq, "unparseable")
        elif entry.get("prevHash") != self.last_hash:
            self._break(seq, "prev_mismatch")
        elif seq and self.hashed == 1:
            self._break(seq, "late_genesis")
        if isinstance(entry, dict) and _is_closed_record(entry) \
                and isinstance(entry.get("sessionRoot"), str):
            self.root_expected = entry["sessionRoot"]
            self.root_seq = seq
            self.hash_version = entry.get("hashVersion")
            self.root_computed = session_root(self.leaves)
            if self.root_computed != self.root_expected:
                self._break(seq, "root_mismatch")
        # Follow the claimed hash whatever the verdict, as arborist's
        # verify_audit_chain does: one bad row is one break, not N.
        self.last_hash = claimed
        self.leaves.append(claimed)

    @property
    def verdict(self) -> str:
        if not self.hashed:
            return UNCHAINED
        if self.breaks:
            return CORRUPTED
        return VERIFIED if self.root_expected is not None else OPEN

    def report(self) -> dict:
        return {
            "state": self.verdict, "entries": self.entries,
            "hashed": self.hashed, "breaks": self.breaks,
            "first_break": self.first_break,
            "first_break_reason": self.first_break_reason,
            "root_expected": self.root_expected,
            "root_computed": self.root_computed,
            "root_seq": self.root_seq, "hash_version": self.hash_version,
        }


def verify_lines(lines: Iterable[bytes]) -> dict:
    """Verdict over one writer's lines (a session journal)."""
    state = ChainState()
    for line in lines:
        if line.strip():
            state.feed(line)
    return state.report()

