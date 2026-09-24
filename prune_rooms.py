"""List, or delete, rooms whose names our room-name rule refuses.

Scanners walking /chat/<payload> used to leave a room behind for every
probe (`bs4' UNION ALL SELECT ...`). New names are checked now; this sweeps
out what came before. It only lists unless given --delete, and --delete
only touches rooms holding no messages or activity unless --include-messages is given.

    . ./vars.sh && python prune_rooms.py             # list
    . ./vars.sh && python prune_rooms.py --delete    # delete the empty ones
"""

import argparse
import sys

from models import ActivityState, Message, Room, UserSession, db
from routes.rooms import valid_room_name


def junk_rooms():
    """[(room, message count)] for every room a new room couldn't be named."""
    rooms = [
        r for r in Room.query.order_by(Room.id).all() if not valid_room_name(r.name)
    ]
    return [(r, Message.query.filter_by(room_id=r.id).count()) for r in rooms]


def holds_content(room):
    """True when `room` holds messages or a running activity."""
    return (
        Message.query.filter_by(room_id=room.id).first() is not None
        or ActivityState.query.filter_by(room_id=room.id).first() is not None
    )


def delete_room(room, include_messages=False):
    """Delete `room`; unless `include_messages`, only while it holds nothing.

    The first write takes our database's write lock, so the emptiness check
    after it sees every message committed before & none can land until our
    commit: a message posted mid-prune keeps its room. Returns True if gone.
    """
    UserSession.query.filter_by(room_id=room.id).delete()
    if not include_messages and holds_content(room):
        db.session.rollback()
        return False
    Room.query.filter_by(forked_from_id=room.id).update({"forked_from_id": None})
    Message.query.filter_by(room_id=room.id).delete()
    ActivityState.query.filter_by(room_id=room.id).delete()
    db.session.delete(room)
    db.session.commit()
    return True


def prune(delete=False, include_messages=False, out=sys.stdout):
    """Print each junk room; delete the chosen ones. Returns how many went.

    Without `include_messages` a room holding messages or a running activity
    is kept.
    """
    deleted = 0
    for room, count in junk_rooms():
        name, room_id = room.name, room.id
        gone = delete and delete_room(room, include_messages)
        verdict = "deleted" if gone else "kept"
        print(f"{verdict:7} {room_id:6} {count:5} msgs  {name!r}", file=out)
        deleted += gone
    return deleted


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--delete", action="store_true", help="delete, not list")
    parser.add_argument(
        "--include-messages",
        action="store_true",
        help="with --delete, also delete junk rooms that hold messages",
    )
    args = parser.parse_args(argv)
    from app import app

    with app.app_context():
        deleted = prune(args.delete, args.include_messages)
    print(f"{deleted} room(s) deleted")


if __name__ == "__main__":
    main()
