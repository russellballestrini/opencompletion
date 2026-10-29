"""unsandbox tools (tools/sandbox.py) & the model proxy a sandbox's
uncloseai-cli calls (routes/sandbox_proxy.py). A fake unsandbox stands in
for un.py, so no test reaches the real API or spends its account."""

import base64
import json
import time
from types import SimpleNamespace

import pytest

import tools
from tools import policy, sandbox as tool_sandbox

pytestmark = pytest.mark.integration

FOX = "fox@unturf.com"


class Fakeunsandbox:
    """Records every call; services start running, jobs finish at once."""

    def __init__(self):
        self.calls = []
        self.services = {}
        self.env = {}
        self.job_results = {}
        self.next_result = {"status": "completed", "exit_code": 0, "stdout": "ok\n"}

    def install(self, monkeypatch):
        import un

        for name in (
            "_make_request",
            "wait_for_job",
            "create_service",
            "get_service",
            "unfreeze_service",
            "freeze_service",
            "execute_in_service",
            "set_service_env",
        ):
            monkeypatch.setattr(un, name, getattr(self, name))
        monkeypatch.setattr(un, "_resolve_credentials", lambda pk, sk: ("pk", "sk"))
        return self

    def _job(self, result=None):
        job_id = f"job-{len(self.job_results)}"
        self.job_results[job_id] = result or self.next_result
        return job_id

    def _make_request(self, method, path, public_key, secret_key, data=None):
        self.calls.append(("request", method, path, data))
        return {"job_id": self._job()}

    def wait_for_job(self, job_id, timeout=None):
        self.calls.append(("wait", job_id, timeout))
        return self.job_results[job_id]

    def create_service(self, name, network_mode, golden_image=None, **kwargs):
        self.calls.append(("create", name, network_mode, golden_image))
        service_id = f"unsb-service-{len(self.services)}"
        self.services[service_id] = "running"
        return {"service_id": service_id}

    def get_service(self, service_id):
        return {"state": self.services[service_id]}

    def unfreeze_service(self, service_id):
        self.calls.append(("unfreeze", service_id))
        self.services[service_id] = "running"

    def freeze_service(self, service_id):
        self.calls.append(("freeze", service_id))
        self.services[service_id] = "frozen"

    def execute_in_service(self, service_id, command, timeout=30000):
        self.calls.append(("execute", service_id, command, timeout))
        return {"job_id": self._job()}

    def set_service_env(self, service_id, env_dict):
        self.calls.append(("env", service_id))
        self.env[service_id] = dict(env_dict)

    def commands(self):
        return [c[2] for c in self.calls if c[0] == "execute"]


@pytest.fixture
def unsandbox(test_app, monkeypatch):
    monkeypatch.setenv("UNSANDBOX_PUBLIC_KEY", "unsb-pk-test")
    monkeypatch.setenv("UNSANDBOX_SECRET_KEY", "unsb-sk-test")
    monkeypatch.setenv("OPENCOMPLETION_SERVER_EGRESS_USERS", FOX)
    monkeypatch.setenv("OPENCOMPLETION_PUBLIC_URL", "https://chat.example.org/")
    monkeypatch.delenv("OPENCOMPLETION_GUEST_CODE_EXEC", raising=False)
    # Idle timers would outlive a test; the freeze is tested on its own.
    monkeypatch.setattr(tool_sandbox.gevent, "spawn_later", lambda *a, **k: None)
    return Fakeunsandbox().install(monkeypatch)


@pytest.fixture
def room(test_app):
    from models import Room, db

    room = Room(name="lab")
    db.session.add(room)
    db.session.commit()
    return room


def fox_in(room, model="qwen"):
    return tools.ToolContext(sid="s", email=FOX, room_id=room.id, model=model)


# -- who is offered what -----------------------------------------------------


def test_a_guest_gets_one_off_runs_only(unsandbox, room):
    guest = tools.ToolContext(sid="s", room_id=room.id, model="qwen")
    # (web search is pinned off for the suite in tests/conftest.py)
    assert tools.offered(guest) == {"run_code": "unsandbox"}


def test_guest_code_exec_off_keeps_runs_to_signed_in_people(
    unsandbox, room, monkeypatch
):
    monkeypatch.setenv("OPENCOMPLETION_GUEST_CODE_EXEC", "off")
    assert "run_code" not in tools.offered(tools.ToolContext(sid="s"))
    eve = tools.ToolContext(sid="s", email="eve@unturf.com")
    assert tools.offered(eve)["run_code"] == "unsandbox"
    assert "sandbox_shell" not in tools.offered(eve)


def test_an_egress_user_gets_a_sandbox_and_uncloseai_cli(unsandbox, room):
    offered = tools.offered(fox_in(room))
    assert offered["run_code"] == "unsandbox"
    assert offered["sandbox_shell"] == offered["sandbox_write_file"] == "sandbox"
    assert offered["delegate"] == "sandbox"


def test_delegate_needs_our_public_url(unsandbox, room, monkeypatch):
    monkeypatch.delenv("OPENCOMPLETION_PUBLIC_URL")
    assert "delegate" not in tools.offered(fox_in(room))
    assert "sandbox_shell" in tools.offered(fox_in(room))


def test_no_unsandbox_keys_no_unsandbox_tools(unsandbox, room, monkeypatch):
    monkeypatch.delenv("UNSANDBOX_SECRET_KEY")
    offered = tools.offered(fox_in(room))
    assert not {"run_code", "sandbox_shell", "delegate"} & set(offered)


def test_each_flag_turns_its_tool_off(unsandbox, room, monkeypatch):
    monkeypatch.setenv("OPENCOMPLETION_DELEGATE", "off")
    assert "delegate" not in tools.offered(fox_in(room))
    monkeypatch.setenv("OPENCOMPLETION_SANDBOX", "off")
    assert "sandbox_shell" not in tools.offered(fox_in(room))
    monkeypatch.setenv("OPENCOMPLETION_RUN_CODE", "off")
    assert "run_code" not in tools.offered(fox_in(room))


# -- one-off runs ------------------------------------------------------------


def test_a_run_has_no_network_and_keeps_its_files(unsandbox, room):
    from models import Attachment

    png = base64.b64encode(b"\x89PNG fake").decode()
    unsandbox.next_result = {
        "status": "completed",
        "exit_code": 0,
        "stdout": "x" * 20000,
        "stderr": "",
        "artifacts": [
            {"filename": "plot.png", "mime_type": "image/png", "content_base64": png}
        ],
    }
    result = tools.run(
        fox_in(room), "run_code", {"language": "Python", "code": "print(1)"}
    )
    (_, method, path, body), *_ = unsandbox.calls
    assert (method, path) == ("POST", "/execute")
    assert body["network_mode"] == "zerotrust" and body["return_artifact"] is True
    assert body["language"] == "python" and "os" not in body
    assert len(result["stdout"]) < 8100 and "characters cut" in result["stdout"]
    (artifact,) = result["artifacts"]
    saved = Attachment.query.one()
    assert artifact["url"] == saved.url and saved.room_id == room.id
    assert saved.filename == "plot.png" and saved.username == "unsandbox"
    # The room's tool line links the file.
    assert tools.loop.sources_of(result) == [{"url": saved.url, "title": "plot.png"}]


def test_freebsd_runs_ask_for_freebsd_and_only_its_languages(unsandbox, room):
    tools.run(
        fox_in(room), "run_code", {"language": "sh", "code": "uname", "os": "freebsd"}
    )
    assert unsandbox.calls[0][3]["os"] == "freebsd"
    refused = tools.run(
        fox_in(room),
        "run_code",
        {"language": "rust", "code": "fn main(){}", "os": "freebsd"},
    )
    assert "FreeBSD runs" in refused["error"]
    assert (
        tools.run(
            fox_in(room), "run_code", {"language": "c", "code": "x", "os": "plan9"}
        )["error"]
        == "os is linux or freebsd"
    )


def test_a_failed_run_says_so(unsandbox, room):
    unsandbox.next_result = {"status": "timeout", "stdout": "", "stderr": "slow"}
    result = tools.run(fox_in(room), "run_code", {"language": "python", "code": "1"})
    assert result["error"] == "the run ended timeout"


# -- a sandbox per chat ------------------------------------------------------


def test_a_chat_makes_its_sandbox_once_and_keeps_it(unsandbox, room):
    from models import Sandbox

    first = tools.run(fox_in(room), "sandbox_shell", {"command": "pip install x"})
    tools.run(fox_in(room), "sandbox_shell", {"command": "ls"})
    creates = [c for c in unsandbox.calls if c[0] == "create"]
    assert len(creates) == 1
    _, name, network, image = creates[0]
    assert name.startswith(f"oc-{room.id}-") and network == "semitrusted"
    assert image == "unsandbox-ubuntu"  # our golden image, by default
    assert unsandbox.commands() == ["pip install x", "ls"]
    assert first["stdout"] == "ok\n" and first["exit_code"] == 0
    sandbox = Sandbox.query.one()
    assert sandbox.room_id == room.id and sandbox.created_by == FOX
    assert sandbox.state == "running" and sandbox.last_used_at <= time.time()


def test_another_golden_image_can_be_named(unsandbox, room, monkeypatch):
    monkeypatch.setenv("OPENCOMPLETION_SANDBOX_IMAGE", "unsandbox-ubuntu-next")
    tools.run(fox_in(room), "sandbox_shell", {"command": "true"})
    assert [c[3] for c in unsandbox.calls if c[0] == "create"] == [
        "unsandbox-ubuntu-next"
    ]


def test_a_frozen_sandbox_is_woken_not_remade(unsandbox, room):
    from models import Sandbox, db

    tools.run(fox_in(room), "sandbox_shell", {"command": "true"})
    sandbox = Sandbox.query.one()
    sandbox.state = "frozen"
    unsandbox.services[sandbox.service_id] = "frozen"
    db.session.commit()
    tools.run(fox_in(room), "sandbox_shell", {"command": "true"})
    assert ("unfreeze", sandbox.service_id) in unsandbox.calls
    assert len([c for c in unsandbox.calls if c[0] == "create"]) == 1
    assert Sandbox.query.one().state == "running"


def test_write_file_survives_any_quoting(unsandbox, room):
    content = "print('it\\'s \"quoted\"; $(rm -rf /)')\n"
    unsandbox.next_result = {"status": "completed", "exit_code": 0, "stdout": "37\n"}
    result = tools.run(
        fox_in(room), "sandbox_write_file", {"path": "/root/a b.py", "content": content}
    )
    assert result == {"path": "/root/a b.py", "bytes": 37}
    (command,) = unsandbox.commands()
    assert "$(rm -rf /)" not in command  # it travels base64, never as shell
    assert base64.b64encode(content.encode()).decode() in command
    assert "'/root/a b.py'" in command


def test_idle_sandboxes_freeze_and_busy_ones_do_not(unsandbox, room, test_app):
    from models import Sandbox, db

    tools.run(fox_in(room), "sandbox_shell", {"command": "true"})
    sandbox = Sandbox.query.one()
    sandbox.last_used_at = time.time() + 600  # a long command still running
    db.session.commit()
    tool_sandbox.freeze_if_idle(sandbox.id, app=test_app)
    assert not [c for c in unsandbox.calls if c[0] == "freeze"]
    sandbox.last_used_at = time.time() - policy.sandbox_idle() - 1
    db.session.commit()
    tool_sandbox.freeze_if_idle(sandbox.id, app=test_app)
    assert ("freeze", sandbox.service_id) in unsandbox.calls
    db.session.expire_all()  # the freeze committed in its own app context
    assert db.session.get(Sandbox, sandbox.id).state == "frozen"


def test_a_sandbox_without_a_chat_is_refused(unsandbox):
    no_room = tools.ToolContext(sid="s", email=FOX, model="qwen")
    assert "no chat" in tools.run(no_room, "sandbox_shell", {"command": "ls"})["error"]


# -- uncloseai-cli & our model proxy ------------------------------------------


def delegate(room, unsandbox, task="make tests pass", model="qwen"):
    unsandbox.next_result = {"status": "completed", "exit_code": 0, "stdout": "Done."}
    return tools.run(fox_in(room, model), "delegate", {"task": task})


def test_delegate_runs_unclose_with_a_token_only_its_vault_holds(unsandbox, room):
    from models import Sandbox

    result = delegate(room, unsandbox)
    assert result["answer"] == "Done." and result["exit_code"] == 0
    sandbox = Sandbox.query.one()
    env = unsandbox.env[sandbox.service_id]
    token = env["MODEL_API_KEY_0"]
    assert env["MODEL_ENDPOINT_0"] == "https://chat.example.org/api/sandbox/v1"
    assert env["MODEL_ID_0"] == "qwen" and token.startswith("ocsb-")
    assert sandbox.token_sha256 == tool_sandbox.token_hash(token)
    assert sandbox.model == "qwen"
    (command,) = unsandbox.commands()
    assert token not in command and token not in json.dumps(result)
    assert command.startswith(". /run/unsandbox/vault.sh")
    assert "unclose -p --no-markdown --model qwen 'make tests pass'" in command
    # Our own model keys never reach the sandbox.
    assert not any("MODEL_API_KEY_1" in k or "sk-" in v for k, v in env.items())


def test_a_second_delegate_reuses_its_token_until_the_model_changes(unsandbox, room):
    delegate(room, unsandbox)
    delegate(room, unsandbox)
    assert len([c for c in unsandbox.calls if c[0] == "env"]) == 1
    delegate(room, unsandbox, model="hermes")
    assert len([c for c in unsandbox.calls if c[0] == "env"]) == 2


class Completions:
    def __init__(self):
        self.asks = []

    def create(self, **kwargs):
        self.asks.append(kwargs)
        if kwargs.get("stream"):
            return iter(
                [
                    SimpleNamespace(
                        model_dump=lambda: {"choices": [{"delta": {"content": "Hi"}}]}
                    ),
                    SimpleNamespace(
                        model_dump=lambda: {"choices": [{"delta": {"content": "!"}}]}
                    ),
                ]
            )
        return SimpleNamespace(
            model_dump=lambda: {"choices": [{"message": {"content": "Hi!"}}]}
        )


@pytest.fixture
def proxy(unsandbox, room, test_app, monkeypatch):
    import routes
    from routes import sandbox_proxy

    completions = Completions()
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    asked_for = []

    def lookup(name):
        asked_for.append(name)
        return client, f"resolved/{name}"

    monkeypatch.setitem(routes.DEPS, "get_openai_client_and_model", lookup)
    sandbox_proxy.proxy_limit.reset()
    delegate(room, unsandbox)
    from models import Sandbox

    token = unsandbox.env[Sandbox.query.one().service_id]["MODEL_API_KEY_0"]
    return SimpleNamespace(
        http=test_app.test_client(),
        token=token,
        completions=completions,
        asked_for=asked_for,
        headers={"Authorization": f"Bearer {token}"},
    )


def test_the_proxy_answers_only_a_live_sandbox_token(proxy):
    from models import Sandbox, db

    body = {"messages": [{"role": "user", "content": "hi"}]}
    url = "/api/sandbox/v1/chat/completions"
    assert proxy.http.post(url, json=body).status_code == 401
    wrong = {"Authorization": "Bearer ocsb-guessed"}
    assert proxy.http.post(url, json=body, headers=wrong).status_code == 401
    assert proxy.http.post(url, json=body, headers=proxy.headers).status_code == 200
    sandbox = Sandbox.query.one()
    sandbox.state = "frozen"
    db.session.commit()
    assert proxy.http.post(url, json=body, headers=proxy.headers).status_code == 401
    assert (
        proxy.http.get("/api/sandbox/v1/models", headers=proxy.headers).status_code
        == 401
    )


def test_the_proxy_answers_with_its_chats_model_and_our_client(proxy):
    body = {
        "model": "gpt-5-turbo",  # a sandbox can't pick another model
        "messages": [{"role": "user", "content": "hi"}],
        "temperature": 0.2,
        "api_key": "leak me",  # nor pass anything else through
    }
    got = proxy.http.post(
        "/api/sandbox/v1/chat/completions", json=body, headers=proxy.headers
    )
    assert got.get_json() == {"choices": [{"message": {"content": "Hi!"}}]}
    (ask,) = proxy.completions.asks
    assert proxy.asked_for == ["qwen"] and ask["model"] == "resolved/qwen"
    assert ask["temperature"] == 0.2 and "api_key" not in ask
    listed = proxy.http.get("/api/sandbox/v1/models", headers=proxy.headers).get_json()
    assert [m["id"] for m in listed["data"]] == ["qwen"]


def test_the_proxy_streams(proxy):
    got = proxy.http.post(
        "/api/sandbox/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hi"}], "stream": True},
        headers=proxy.headers,
    )
    assert got.mimetype == "text/event-stream"
    lines = [l for l in got.get_data(as_text=True).split("\n") if l.startswith("data:")]
    assert lines[-1] == "data: [DONE]"
    assert json.loads(lines[0][5:])["choices"][0]["delta"]["content"] == "Hi"


def test_the_proxy_limits_each_sandbox(proxy, monkeypatch):
    from routes import sandbox_proxy

    monkeypatch.setattr(sandbox_proxy.proxy_limit, "count", 2)
    body = {"messages": [{"role": "user", "content": "hi"}]}
    url = "/api/sandbox/v1/chat/completions"
    codes = [
        proxy.http.post(url, json=body, headers=proxy.headers).status_code
        for _ in range(3)
    ]
    assert codes == [200, 200, 429]


def test_the_proxy_refuses_a_body_without_messages(proxy):
    got = proxy.http.post(
        "/api/sandbox/v1/chat/completions", json={"prompt": "x"}, headers=proxy.headers
    )
    assert got.status_code == 400
