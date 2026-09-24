"""List, or delete, rooms whose names our room-name rule refuses.

Scanners walking /chat/<payload> used to leave a room behind for every
probe (`bs4' UNION ALL SELECT ...`). New names are checked now; this sweeps
out what came before. It only lists unless given --delete, and --delete
only touches rooms holding no messages unless --include-messages is given.

    python prune_rooms.py                 # list
    python prune_rooms.py --delete        # delete the empty ones
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


def delete_room(room):
    Room.query.filter_by(forked_from_id=room.id).update({"forked_from_id": None})
    Message.query.filter_by(room_id=room.id).delete()
    ActivityState.query.filter_by(room_id=room.id).delete()
    UserSession.query.filter_by(room_id=room.id).delete()
    db.session.delete(room)


def prune(delete=False, include_messages=False, out=sys.stdout):
    """Print each junk room; delete the chosen ones. Returns how many went."""
    deleted = 0
    for room, count in junk_rooms():
        doomed = delete and (include_messages or count == 0)
        verdict = "deleted" if doomed else "kept"
        print(f"{verdict:7} {room.id:6} {count:5} msgs  {room.name!r}", file=out)
        if doomed:
            delete_room(room)
            deleted += 1
    db.session.commit()
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
