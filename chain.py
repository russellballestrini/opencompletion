"""A room's history as an append-only, hash-chained, Merkle-rooted log.

Every event in a room is one unfirehose-chain-v1 line (provenance.py,
vendored from uncloseai-cli: the rule unfirehose & arborist verify too),
hash-linked to the room's previous event & kept in RoomEvent, insert-only:

    user        a person's message (its sha256, never its words)
    tool        a tool call's result, as the model was handed it, chained
                BEFORE the reply that cites it (resultSha256, status, error)
    assistant   a model's reply, naming the tool events it sat beside
    edit        a message changed in place; delete: one removed
    supersede   an edit & resend: the revision & the messages it hid
    fork        the first event of a forked room: where it came from
    attachment  a file a message carries (its blob's sha256)
    room_deleted  the room is gone; its chain stays (forensics)

Lines carry digests, not text, so a chain can be shared or witnessed
without the conversation. A RoomCheckpoint is a merkle-v1 root over a
chain's event hashes, so one event can be proved without the rest.
fox, 2026-11-02: a model said a 404 repo was live; its tool's 404 is now
evidence chained before the claim, & a chain outlives its room so abuse
of our system can be looked into.

A chain is one room's life, `<room id>@<created_at>`: SQLite may give a
deleted room's id to a new room, which starts its own chain.

Best effort, never in the way: a failure to chain logs & the action it
records goes on. Callers commit their own change first (append commits).
OPENCOMPLETION_CHAIN_MIRROR names a directory each line is also appended
to (<dir>/opencompletion-<chain>.jsonl), for an outside witness such as
unfirehose's ingester.
"""

import hashlib
import json
import os
import re
from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError

import provenance as P
from models import Message, Room, RoomCheckpoint, RoomEvent, db

SCHEMA = "opencompletion-room/1"
ATTEMPTS = 5  # two writers racing for one seq: the loser re-reads the head
_build = {}


def sha256_text(text):
    """sha256 hex of text, or None for no text."""
    if text is None:
        return None
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()


def build_commit():
    """The commit we run from, as /version reports it."""
    if "commit" not in _build:
        try:
            from routes.status import running_commit

            _build["commit"] = running_commit(
                os.path.dirname(os.path.abspath(__file__))
            )["commit"]
        except Exception:  # noqa: BLE001 — a chain line goes on without it
            _build["commit"] = None
    return _build["commit"]


def chain_of(room):
    """The chain key of a room's life: `<id>@<created_at>`."""
    created = room.created_at.isoformat() if room.created_at else "0"
    return f"{room.id}@{created}"


def current_chain(room_id):
    """The chain of the room that holds `room_id` now, or None."""
    room = db.session.get(Room, room_id) if room_id is not None else None
    return chain_of(room) if room else None


def _head(chain):
    return RoomEvent.query.filter_by(chain=chain).order_by(RoomEvent.seq.desc()).first()


def append(room_id, room_name, kind, **fields):
    """Chain one event into a room's history; its hash, or None when it
    couldn't be (logged). `fields` go into the hashed line as they are."""
    for _ in range(ATTEMPTS):
        try:
            chain = current_chain(room_id)
            if not chain:
                return None
            head = _head(chain)
            seq = head.seq + 1 if head else 0
            body = {
                "$schema": SCHEMA,
                "type": kind,
                "timestamp": datetime.now(timezone.utc).isoformat(
                    timespec="milliseconds"
                ),
                "chain": chain,
                "room": room_name,
                "roomId": room_id,
                "seq": seq,
                "build": build_commit(),
                **P.version_fields(),
                **fields,
            }
            line, digest = P.chain_line(body, head.hash if head else None, default=str)
            db.session.add(
                RoomEvent(
                    chain=chain,
                    room_id=room_id,
                    room_name=room_name or "",
                    seq=seq,
                    type=kind,
                    message_id=fields.get("messageId"),
                    line=line.decode("utf-8"),
                    hash=digest,
                )
            )
            db.session.commit()
        except IntegrityError:
            db.session.rollback()  # another writer took this seq: retry
            continue
        except Exception as e:  # noqa: BLE001 — evidence, never an outage
            db.session.rollback()
            print(f"[WARN] chain: {kind} event for room {room_id} not chained: {e}")
            return None
        _mirror(chain, line)
        return digest
    print(f"[WARN] chain: {kind} event for room {room_id} lost {ATTEMPTS} races")
    return None


def _mirror(chain, line):
    folder = os.environ.get("OPENCOMPLETION_CHAIN_MIRROR", "").strip()
    if not folder:
        return
    try:
        os.makedirs(folder, mode=0o700, exist_ok=True)
        name = re.sub(r"[^0-9A-Za-z@._-]", "_", chain)
        path = os.path.join(folder, f"opencompletion-{name}.jsonl")
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "ab") as out:
            out.write(line + b"\n")
    except OSError as e:
        print(f"[WARN] chain mirror: {e}")


def events(chain):
    """A chain's events, oldest first."""
    return RoomEvent.query.filter_by(chain=chain).order_by(RoomEvent.seq).all()


def checkpoint(room_id):
    """A merkle-v1 root over the room's event hashes so far, kept; the
    RoomCheckpoint, or None for a room with no events."""
    try:
        chain = current_chain(room_id)
        chained = events(chain) if chain else []
        if not chained:
            return None
        mark = RoomCheckpoint(
            chain=chain,
            room_id=room_id,
            upto_seq=chained[-1].seq,
            root=P.session_root(e.hash for e in chained),
        )
        db.session.add(mark)
        db.session.commit()
        return mark
    except Exception as e:  # noqa: BLE001
        db.session.rollback()
        print(f"[WARN] chain: no checkpoint for room {room_id}: {e}")
        return None


def proof(chain, event_hash):
    """A merkle-v1 inclusion proof for one event against the chain's latest
    checkpoint that covers it: {"proof", "checkpoint"}, or None."""
    chained = events(chain)
    index = next((i for i, e in enumerate(chained) if e.hash == event_hash), None)
    if index is None:
        return None
    mark = (
        RoomCheckpoint.query.filter(
            RoomCheckpoint.chain == chain,
            RoomCheckpoint.upto_seq >= chained[index].seq,
        )
        .order_by(RoomCheckpoint.id.desc())
        .first()
    )
    if not mark:
        return None
    covered = [e.hash for e in chained if e.seq <= mark.upto_seq]
    tree = P.MerkleTree.build(P.hash_leaf(bytes.fromhex(h)) for h in covered)
    return {
        "proof": P.proof_to_dict(tree.proof(covered.index(event_hash))),
        "checkpoint": {"upto_seq": mark.upto_seq, "root": mark.root},
    }


def verify(chain):
    """A chain checked: the line rule & links (ChainState), seq order, every
    checkpoint's root, &, while its room lives, its messages against the
    digests their events recorded (a direct database edit shows)."""
    chained = events(chain)
    report = P.verify_lines(e.line.encode("utf-8") for e in chained)
    report["chain"] = chain
    report["room"] = chained[-1].room_name if chained else None
    report["seq_gaps"] = [e.seq for i, e in enumerate(chained) if e.seq != i]
    hashes = [e.hash for e in chained]
    bad_marks = []
    for mark in RoomCheckpoint.query.filter_by(chain=chain).all():
        covered = hashes[: mark.upto_seq + 1]
        if len(covered) != mark.upto_seq + 1 or P.session_root(covered) != mark.root:
            bad_marks.append(mark.upto_seq)
    report["bad_checkpoints"] = bad_marks
    room_id = chained[0].room_id if chained else None
    alive = room_id is not None and current_chain(room_id) == chain
    report["room_alive"] = alive
    report["content_mismatches"] = (
        _content_mismatches(chained, room_id) if alive else []
    )
    report["ok"] = (
        report["state"] in (P.OPEN, P.VERIFIED)
        and not report["seq_gaps"]
        and not bad_marks
        and not report["content_mismatches"]
    )
    return report


def _content_mismatches(chained, room_id):
    """Ids of the room's messages whose words no longer match the last
    digest chained for them; deleted ones are skipped."""
    latest = {}
    for e in chained:
        try:
            body = json.loads(e.line)
        except ValueError:
            continue
        kind = body.get("type")
        if kind in ("user", "assistant", "edit") and body.get("messageId"):
            latest[body["messageId"]] = body.get("contentSha256")
        elif kind == "fork":
            for copy in body.get("copied") or []:
                latest[copy["messageId"]] = copy.get("contentSha256")
        elif kind == "delete" and body.get("messageId"):
            latest.pop(body["messageId"], None)
    mismatched = []
    for message_id, digest in latest.items():
        message = db.session.get(Message, message_id)
        if message is None or message.room_id != room_id:
            continue
        if sha256_text(message.content) != digest:
            mismatched.append(message_id)
    return mismatched


def chains():
    """Every chain, rooms long deleted included, oldest room first."""
    return [
        r[0]
        for r in db.session.query(RoomEvent.chain, db.func.min(RoomEvent.id))
        .group_by(RoomEvent.chain)
        .order_by(db.func.min(RoomEvent.id))
    ]
