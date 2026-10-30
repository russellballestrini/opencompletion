"""Our HTTP routes, as blueprints: pages, accounts, rooms, code, speech,
files, the sandbox proxy & status (/version, /health).

app.py keeps chat itself (the /chat page, Socket.IO events & model
streaming) and calls register() once. The few helpers these routes share
with chat arrive through DEPS rather than `import app`, because a server
started as `python app.py` runs as __main__ and importing `app` would
build a second application.
"""

DEPS = {}


def register(app, **deps):
    """Register every blueprint on `app`.

    deps: get_openai_client_and_model (model client lookup), system_users
    (the live list of names whose messages are system/assistant turns),
    retitle_room(room) & set_room_title(room, title) (save & announce a
    room's title).
    """
    DEPS.update(deps)
    from routes import (
        accounts,
        code,
        files,
        pages,
        rooms,
        sandbox_proxy,
        speech,
        status,
    )

    for module in (
        pages,
        accounts,
        rooms,
        code,
        speech,
        files,
        sandbox_proxy,
        status,
    ):
        app.register_blueprint(module.bp)
