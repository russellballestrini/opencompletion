"""unsandbox tools (tools/sandbox.py) & the model proxy a sandbox's
uncloseai-cli calls (routes/sandbox_proxy.py). A fake unsandbox stands in
for un.py, so no test reaches the real API or spends its account."""

import base64
import dataclasses
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
        self.polls = []
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
            "get_job",
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

    def get_job(self, job_id):
        self.calls.append(("poll", job_id))
        return self.job_results[job_id]

    def create_service(self, name, network_mode, golden_image=None, **kwargs):
        self.calls.append(("create", name, network_mode, golden_image))
        service_id = f"unsb-service-{len(self.services)}"
        self.services[service_id] = "running"
        return {"service_id": service_id}

    def get_service(self, service_id):
        """The next of `states_ahead[service_id]` (then that state stays),
        else what services says."""
        ahead = self.states_ahead.get(service_id)
        if ahead:
            self.services[service_id] = ahead.pop(0)
        return {"state": self.services[service_id]}

    def unfreeze_service(self, service_id):
        self.calls.append(("unfreeze", service_id))
        if self.unfreeze_refuses:
            import requests

            response = requests.Response()
            response.status_code = 400
            response._content = self.unfreeze_refuses.encode()
            raise requests.HTTPError("400 Client Error: Bad Request", response=response)
        self.services[service_id] = "running"

    states_ahead = {}
    unfreeze_refuses = ""

    def freeze_service(self, service_id):
        self.calls.append(("freeze", service_id))
        self.services[service_id] = "frozen"

    def execute_in_service(self, service_id, command, timeout=30000):
        """Plays a background delegate too: its start answers a pid, each
        poll the next of `polls`, then its exit & answer."""
        self.calls.append(("execute", service_id, command, timeout))
        if "setsid nohup" in command:
            return {"job_id": self._job({"status": "completed", "stdout": "4242\n"})}
        if "__EXIT__" in command:
            out = (
                self.polls.pop(0)
                if self.polls
                else (f"__EXIT__{self.delegate_exit}\n{self.answer}\n__STDERR__\n")
            )
            return {"job_id": self._job({"status": "completed", "stdout": out})}
        if command.rstrip().endswith("echo stopped"):
            return {"job_id": self._job({"status": "completed", "stdout": "stopped"})}
        return {"job_id": self._job()}

    polls = ()
    answer = "Done."
    delegate_exit = 0

    def delegate_commands(self):
        """The command that started uncloseai-cli, & every poll."""
        commands = self.commands()
        start = [c for c in commands if "setsid nohup" in c]
        return start, [c for c in commands if "__EXIT__" in c]

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
    monkeypatch.setattr(tool_sandbox.gevent, "sleep", lambda s: None)
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
    # No image named: unsandbox's default (unsandbox-ubuntu, with
    # uncloseai-cli). Its API refuses any name but golden-* (2026-10-29).
    assert image is None
    assert unsandbox.commands() == ["pip install x", "ls"]
    assert first["stdout"] == "ok\n" and first["exit_code"] == 0
    sandbox = Sandbox.query.one()
    assert sandbox.room_id == room.id and sandbox.created_by == FOX
    assert sandbox.state == "running" and sandbox.last_used_at <= time.time()


def test_another_golden_image_can_be_named(unsandbox, room, monkeypatch):
    monkeypatch.setenv("OPENCOMPLETION_SANDBOX_IMAGE", "golden-alpine-3.24")
    tools.run(fox_in(room), "sandbox_shell", {"command": "true"})
    assert [c[3] for c in unsandbox.calls if c[0] == "create"] == ["golden-alpine-3.24"]


def test_an_unsandbox_refusal_says_why(unsandbox, room, monkeypatch):
    import requests
    import un

    response = requests.Response()
    response.status_code = 400
    response._content = b'{"error": "golden_image must be a platform golden image"}'

    def refuse(**kwargs):
        raise requests.HTTPError("400 Client Error: Bad Request", response=response)

    monkeypatch.setattr(un, "create_service", refuse)
    error = tools.run(fox_in(room), "sandbox_shell", {"command": "ls"})["error"]
    assert "400 Client Error" in error
    assert "(golden_image must be a platform golden image)" in error


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


def _frozen_sandbox(unsandbox, room):
    """A chat whose sandbox our row calls frozen."""
    from models import Sandbox, db

    tools.run(fox_in(room), "sandbox_shell", {"command": "true"})
    sandbox = Sandbox.query.one()
    sandbox.state = "frozen"
    db.session.commit()
    unsandbox.states_ahead = {}
    return sandbox


def test_a_sandbox_mid_upgrade_is_waited_for_not_unfrozen(unsandbox, room):
    # 2026-10-30, demo: "Cannot unfreeze service in state: upgrading" failed
    # a delegate right after the golden rollout.
    sandbox = _frozen_sandbox(unsandbox, room)
    unsandbox.states_ahead[sandbox.service_id] = ["upgrading", "upgrading", "running"]
    result = tools.run(fox_in(room), "sandbox_shell", {"command": "echo hi"})
    assert "error" not in result
    assert ("unfreeze", sandbox.service_id) not in unsandbox.calls
    assert unsandbox.commands()[-1] == "echo hi"


def test_an_unfreeze_that_loses_a_race_with_an_upgrade_waits(unsandbox, room):
    sandbox = _frozen_sandbox(unsandbox, room)
    unsandbox.states_ahead[sandbox.service_id] = ["frozen", "upgrading", "running"]
    unsandbox.unfreeze_refuses = (
        '{"error":"Cannot unfreeze service in state: upgrading"}'
    )
    result = tools.run(fox_in(room), "sandbox_shell", {"command": "echo hi"})
    assert "error" not in result
    assert ("unfreeze", sandbox.service_id) in unsandbox.calls


def test_any_other_unfreeze_refusal_still_fails(unsandbox, room):
    sandbox = _frozen_sandbox(unsandbox, room)
    unsandbox.services[sandbox.service_id] = "frozen"
    unsandbox.unfreeze_refuses = '{"error":"quota exceeded"}'
    result = tools.run(fox_in(room), "sandbox_shell", {"command": "echo hi"})
    assert "quota exceeded" in result["error"]


def test_an_upgrade_may_take_longer_than_a_start(unsandbox, room, monkeypatch):
    sandbox = _frozen_sandbox(unsandbox, room)
    clock = [1000.0]
    monkeypatch.setattr(tool_sandbox.time, "time", lambda: clock[0])
    monkeypatch.setattr(
        tool_sandbox.gevent, "sleep", lambda s: clock.__setitem__(0, clock[0] + 60)
    )
    # 5 minutes upgrading: past READY_TIMEOUT_S (180s), inside UPGRADE_TIMEOUT_S
    unsandbox.states_ahead[sandbox.service_id] = ["upgrading"] * 5 + ["running"]
    result = tools.run(fox_in(room), "sandbox_shell", {"command": "echo hi"})
    assert "error" not in result


def test_an_upgrade_that_never_ends_says_so(unsandbox, room, monkeypatch):
    sandbox = _frozen_sandbox(unsandbox, room)
    clock = [1000.0]
    monkeypatch.setattr(tool_sandbox.time, "time", lambda: clock[0])
    monkeypatch.setattr(
        tool_sandbox.gevent, "sleep", lambda s: clock.__setitem__(0, clock[0] + 60)
    )
    unsandbox.services[sandbox.service_id] = "upgrading"
    result = tools.run(fox_in(room), "sandbox_shell", {"command": "echo hi"})
    assert "last state: upgrading" in result["error"]


def test_an_unreachable_sandbox_is_woken(unsandbox, room):
    sandbox = _frozen_sandbox(unsandbox, room)
    unsandbox.services[sandbox.service_id] = "unreachable"
    result = tools.run(fox_in(room), "sandbox_shell", {"command": "echo hi"})
    assert "error" not in result
    assert ("unfreeze", sandbox.service_id) in unsandbox.calls


def test_a_deep_upgrade_may_take_longer_than_a_start(unsandbox, room, monkeypatch):
    sandbox = _frozen_sandbox(unsandbox, room)
    clock = [1000.0]
    monkeypatch.setattr(tool_sandbox.time, "time", lambda: clock[0])
    monkeypatch.setattr(
        tool_sandbox.gevent, "sleep", lambda s: clock.__setitem__(0, clock[0] + 60)
    )
    unsandbox.states_ahead[sandbox.service_id] = ["deep_upgrading"] * 5 + ["running"]
    result = tools.run(fox_in(room), "sandbox_shell", {"command": "echo hi"})
    assert "error" not in result


def test_a_busy_code_from_the_api_contract_waits_too(unsandbox, room):
    sandbox = _frozen_sandbox(unsandbox, room)
    unsandbox.states_ahead[sandbox.service_id] = ["frozen", "upgrading", "running"]
    unsandbox.unfreeze_refuses = (
        '{"error":"Cannot unfreeze service in state: upgrading",'
        '"code":"service_busy","state":"upgrading","retryable":true}'
    )
    result = tools.run(fox_in(room), "sandbox_shell", {"command": "echo hi"})
    assert "error" not in result


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


# -- handing a sandbox's file to the person ----------------------------------


@pytest.fixture
def sandbox_disk(unsandbox, monkeypatch, tmp_path):
    """The sandbox's commands run in a real local shell, so a file in
    tmp_path is a file in the sandbox: what share_file asks of `stat`,
    `tail`, `head` & `base64` is what it gets."""
    import subprocess

    import un

    real = unsandbox.execute_in_service

    def execute(service_id, command, timeout=30000):
        if command == "true":  # the sandbox being made
            return real(service_id, command, timeout)
        unsandbox.calls.append(("execute", service_id, command, timeout))
        done = subprocess.run(["bash", "-c", command], capture_output=True, text=True)
        result = {"status": "completed", "exit_code": done.returncode}
        result |= {"stdout": done.stdout, "stderr": done.stderr}
        return {"job_id": unsandbox._job(result)}

    monkeypatch.setattr(un, "execute_in_service", execute)
    return tmp_path


def test_a_sandbox_file_becomes_a_link_in_its_chat(sandbox_disk, room, monkeypatch):
    from models import Attachment
    from blob_store import read_attachment

    monkeypatch.setattr(tool_sandbox, "SHARE_CHUNK_BYTES", 1000)  # several reads
    deck = bytes(range(256)) * 20  # 5120 bytes, every byte value
    (sandbox_disk / "my deck.pptx").write_bytes(deck)
    path = str(sandbox_disk / "my deck.pptx")
    result = tools.run(fox_in(room), "sandbox_share_file", {"path": path})
    (item,) = result["artifacts"]
    attachment = Attachment.query.one()
    assert read_attachment(attachment) == deck
    assert attachment.room_id == room.id
    assert item["url"] == attachment.url and item["size"] == len(deck)
    assert item["mime_type"] == (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    )
    assert item["markdown"].startswith("[📎 my deck\\.pptx · 5 KB](/attachments/")
    assert "markdown" in result["note"]
    # its tool line links the file, as a run_code artifact's does
    from tools.loop import sources_of

    assert sources_of(result) == [{"url": attachment.url, "title": item["filename"]}]


def test_sharing_says_what_is_wrong_with_a_path(sandbox_disk, room, monkeypatch):
    missing = tools.run(
        fox_in(room), "sandbox_share_file", {"path": str(sandbox_disk / "nope")}
    )
    assert "no file at" in missing["error"]
    folder = tools.run(fox_in(room), "sandbox_share_file", {"path": str(sandbox_disk)})
    assert "zip it first" in folder["error"]
    monkeypatch.setenv("OPENCOMPLETION_UPLOAD_MAX_MB", "0.001")  # ~1 KB
    (sandbox_disk / "big.bin").write_bytes(b"x" * 5000)
    big = tools.run(
        fox_in(room), "sandbox_share_file", {"path": str(sandbox_disk / "big.bin")}
    )
    assert "5000 bytes" in big["error"]
    from models import Attachment

    assert Attachment.query.count() == 0


def test_a_shared_path_never_runs_as_shell(sandbox_disk, room):
    odd = sandbox_disk / "it's $(touch pwned) a;b.txt"
    odd.write_text("hello")
    result = tools.run(fox_in(room), "sandbox_share_file", {"path": str(odd)})
    assert result["artifacts"][0]["size"] == 5
    assert not (sandbox_disk / "pwned").exists()  # nor in the sandbox's cwd:
    assert not (sandbox_disk.parent / "pwned").exists()


def test_the_model_is_told_files_reach_the_person_by_sharing(unsandbox, room):
    assert tools.offered(fox_in(room))["sandbox_share_file"] == "sandbox"
    assert "sandbox_share_file" in tools.guidance(fox_in(room))["content"]
    guest = tools.ToolContext(sid="s", room_id=room.id, model="qwen")
    assert "sandbox_share_file" not in tools.offered(guest)


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


def delegate(room, unsandbox, task="make tests pass", model="qwen", context=None):
    context = context or fox_in(room, model)
    return tools.run(context, "delegate", {"task": task})


def test_delegate_runs_unclose_with_a_token_only_its_vault_holds(unsandbox, room):
    from models import Sandbox

    result = delegate(room, unsandbox)
    assert result["answer"] == "Done." and result["exit_code"] == 0
    sandbox = Sandbox.query.one()
    env = unsandbox.env[sandbox.service_id]
    token = env["MODEL_API_KEY_0"]
    assert env["MODEL_ENDPOINT_0"] == "https://chat.example.org/api/sandbox/v1"
    assert env["MODEL_ID_0"] == "qwen" and token.startswith("ocsb-")
    # The run is over, so its token is too (revoked on the way out).
    assert sandbox.token_sha256 is None
    assert sandbox.model == "qwen"
    (start,), polls = unsandbox.delegate_commands()
    assert polls  # it was polled until it wrote its exit code
    for command in [start] + polls:
        assert token not in command
    assert token not in json.dumps(result)
    assert ". /run/unsandbox/vault.sh" in start and "setsid nohup" in start
    assert "unclose -p --no-markdown --model qwen" in start
    assert "make tests pass" in start
    # Our own model keys never reach the sandbox.
    assert not any("MODEL_API_KEY_1" in k or "sk-" in v for k, v in env.items())


def test_a_delegate_line_names_its_task_in_a_glance():
    long_task = "In this sandbox, clone https://git.unturf.com/python/ago.git " * 3
    label = tools.label_for("delegate", {"task": long_task + "\nmore lines"})
    assert len(label) == 80 and label.endswith("…")
    assert tools.label_for("delegate", {"task": "make tests pass"}) == "make tests pass"


def test_the_progress_peek_is_unbuffered(unsandbox, room):
    # timeout kills the following peek; a block-buffered pipe loses every
    # row it printed, so progress never showed (2026-10-31).
    delegate(room, unsandbox)
    _, polls = unsandbox.delegate_commands()
    assert polls and all(
        "PYTHONUNBUFFERED=1 timeout 6 unclose --peek" in p for p in polls
    )


def test_every_delegate_arms_a_fresh_token_and_native_tools(unsandbox, room):
    from models import Sandbox

    delegate(room, unsandbox)
    first = dict(unsandbox.env[Sandbox.query.one().service_id])
    delegate(room, unsandbox)
    second = unsandbox.env[Sandbox.query.one().service_id]
    assert len([c for c in unsandbox.calls if c[0] == "env"]) == 2
    assert first["MODEL_API_KEY_0"] != second["MODEL_API_KEY_0"]
    # vLLM strips text-format <tool_call> on qwen: uncloseai-cli goes native.
    assert second["UNCLOSEAI_NATIVE_TOOLS"] == "on"


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
    # A token as a delegate run holds it while it runs (a finished run's
    # token is revoked: test_a_finished_runs_token_is_refused).
    sandbox = tool_sandbox.ready(fox_in(room))
    tool_sandbox.arm_proxy(sandbox, "qwen")
    token = unsandbox.env[sandbox.service_id]["MODEL_API_KEY_0"]
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


def test_a_finished_runs_token_is_refused(unsandbox, room, test_app):
    delegate(room, unsandbox)
    from models import Sandbox

    token = unsandbox.env[Sandbox.query.one().service_id]["MODEL_API_KEY_0"]
    got = test_app.test_client().post(
        "/api/sandbox/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert got.status_code == 401


def test_a_run_that_raises_still_revokes_its_token(unsandbox, room, monkeypatch):
    from models import Sandbox

    def boom(*args, **kwargs):
        raise RuntimeError("unsandbox fell over")

    monkeypatch.setattr(tool_sandbox, "fresh_note", boom)
    result = delegate(room, unsandbox)
    assert "unsandbox fell over" in result["error"]
    assert Sandbox.query.one().token_sha256 is None


def test_an_older_run_ending_leaves_a_newer_runs_token(unsandbox, room):
    from models import Sandbox, db

    sandbox = tool_sandbox.ready(fox_in(room))
    tool_sandbox.arm_proxy(sandbox, "qwen")
    older = sandbox.token_sha256
    tool_sandbox.arm_proxy(sandbox, "qwen")  # a newer run armed its own
    newer = sandbox.token_sha256
    tool_sandbox.revoke_proxy(sandbox.id, older)
    assert db.session.get(Sandbox, sandbox.id).token_sha256 == newer
    tool_sandbox.revoke_proxy(sandbox.id, newer)
    assert db.session.get(Sandbox, sandbox.id).token_sha256 is None


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
    assert "api_key" not in ask.get("extra_body", {})
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


def test_a_run_without_files_tells_the_model_where_files_must_go(unsandbox, room):
    result = tools.run(fox_in(room), "run_code", {"language": "python", "code": "1"})
    assert result["artifacts"] == []
    assert "/tmp/artifacts/" in result["note"]
    assert "/tmp/artifacts/" in tools.RUN_CODE["function"]["description"]


def test_a_code_tools_line_keeps_its_code_and_output(unsandbox, room):
    unsandbox.next_result = {
        "status": "completed",
        "exit_code": 1,
        "stdout": "42\n",
        "stderr": "warning\n",
    }
    events = []
    calls = [
        {
            "id": "c1",
            "name": "run_code",
            "arguments": json.dumps({"language": "python", "code": "print(42)"}),
        },
        {"id": "c2", "name": "web_fetch", "arguments": "{}"},
    ]
    tools.run_round(fox_in(room), calls, "", events.append)
    done = [e for e in events if e["status"] != "running"]
    assert done[0]["detail"] == {
        "language": "python",
        "input": "print(42)",
        "stdout": "42\n",
        "stderr": "warning\n",
        "exit_code": 1,
    }
    assert "detail" not in done[1]


def test_shell_and_write_file_lines_show_their_command_and_file(unsandbox, room):
    from tools.loop import detail_of

    shell = detail_of("sandbox_shell", {"command": "ls -la"}, {"stdout": "a\n"})
    assert shell["language"] == "bash" and shell["input"] == "ls -la"
    wrote = detail_of(
        "sandbox_write_file", {"path": "/root/x.py", "content": "print(1)"}, {}
    )
    assert wrote["language"] == "py" and wrote["input"] == "print(1)"
    long = detail_of("run_code", {"code": "x" * 9000}, {"stdout": "y" * 9000})
    assert len(long["input"]) == 6000 and len(long["stdout"]) == 4000


def test_an_egress_users_note_names_delegate_for_multi_step_work(unsandbox, room):
    note = tools.guidance(fox_in(room))["content"]
    assert (
        "run_code, sandbox_shell, sandbox_write_file, sandbox_share_file, delegate"
        in note
    )
    # Which sandbox tool for what: delegate the multi-step work, the shell
    # for a quick look, both in one sandbox.
    assert "multi-step" in tools.DELEGATE["function"]["description"]
    assert "shared with delegate" in tools.SANDBOX_SHELL["function"]["description"]
    assert tools.guidance(None) is None


# -- destroying sandboxes & the next one ------------------------------------


def make_sandbox(unsandbox, room):
    from models import Sandbox

    tools.run(fox_in(room), "sandbox_shell", {"command": "true"})
    return Sandbox.query.one()


def test_destroy_sends_a_plain_delete_and_keeps_the_row(unsandbox, room):
    sandbox = make_sandbox(unsandbox, room)
    sandbox.token_sha256 = "abc"
    assert tool_sandbox.destroy(sandbox, "test") is True
    assert (
        "request",
        "DELETE",
        f"/services/{sandbox.service_id}",
        None,
    ) in unsandbox.calls
    assert sandbox.state == "destroyed" and sandbox.token_sha256 is None


def test_a_sudo_gated_destroy_keeps_the_sandbox_and_says_why(
    unsandbox, room, monkeypatch, capsys
):
    import requests
    import un

    sandbox = make_sandbox(unsandbox, room)
    response = requests.Response()
    response.status_code = 428

    def refuse(*a):
        raise requests.HTTPError("428 Client Error", response=response)

    monkeypatch.setattr(un, "_make_request", refuse)
    assert tool_sandbox.destroy(sandbox, "test") is False
    assert sandbox.state == "running"
    assert "turn sudo off for this key" in capsys.readouterr().out


def test_the_sweep_destroys_only_long_unused_sandboxes(
    unsandbox, test_app, monkeypatch
):
    from models import Room, Sandbox, db

    rooms = [Room(name=f"r{i}") for i in range(3)]
    db.session.add_all(rooms)
    db.session.commit()
    now = time.time()
    rows = [
        Sandbox(
            room_id=rooms[0].id,
            service_id="s-old",
            name="a",
            state="frozen",
            last_used_at=now - 15 * 86400,
        ),
        Sandbox(
            room_id=rooms[1].id,
            service_id="s-new",
            name="b",
            state="frozen",
            last_used_at=now - 86400,
        ),
        Sandbox(
            room_id=rooms[2].id,
            service_id="s-run",
            name="c",
            state="running",
            last_used_at=now - 30 * 86400,
        ),
    ]
    db.session.add_all(rows)
    db.session.commit()
    # A long-unused row that still says running (its idle freeze failed,
    # e.g. mid-upgrade) is destroyed too (2026-10-31).
    assert tool_sandbox.sweep(app=test_app) == 2
    db.session.expire_all()
    assert [s.state for s in Sandbox.query.order_by(Sandbox.id)] == [
        "destroyed",
        "frozen",
        "destroyed",
    ]
    monkeypatch.setenv("OPENCOMPLETION_SANDBOX_RETENTION_DAYS", "0")
    assert tool_sandbox.sweep(app=test_app) == 0


def test_deleting_a_chat_destroys_its_sandbox(unsandbox, test_app):
    from models import Room, Sandbox, User, db

    owner = User(email=FOX, display_name="fox")
    db.session.add(owner)
    db.session.commit()
    room = Room(name="mine", owner_id=owner.id)
    db.session.add(room)
    db.session.commit()
    sandbox = make_sandbox(unsandbox, room)
    service_id = sandbox.service_id
    http = test_app.test_client()
    with http.session_transaction() as session:
        session["user_id"] = owner.id
    assert http.delete(f"/api/rooms/{room.id}/delete").status_code == 200
    assert ("request", "DELETE", f"/services/{service_id}", None) in unsandbox.calls
    assert Sandbox.query.count() == 0


def _refuse_deletes(monkeypatch, status):
    import requests
    import un

    response = requests.Response()
    response.status_code = status

    def refuse(method, path, *a, **k):
        raise requests.HTTPError(f"{status} Client Error", response=response)

    monkeypatch.setattr(un, "_make_request", refuse)


def test_a_chats_sandbox_that_wont_destroy_is_kept_for_the_sweep(
    unsandbox, test_app, monkeypatch
):
    from models import Room, Sandbox, db

    room = Room(name="gone")
    db.session.add(room)
    db.session.commit()
    sandbox = make_sandbox(unsandbox, room)
    sandbox.token_sha256 = "live"
    db.session.commit()
    _refuse_deletes(monkeypatch, 409)  # busy: mid-upgrade
    tool_sandbox.forget_room(room.id)
    kept = Sandbox.query.one()
    assert kept.state == "destroy_pending" and kept.room_id is None
    assert kept.token_sha256 is None
    service_id = kept.service_id
    db.session.delete(room)  # the chat itself goes
    db.session.commit()

    monkeypatch.undo()
    unsandbox.install(monkeypatch)  # deletes work again
    monkeypatch.setenv("OPENCOMPLETION_SANDBOX_RETENTION_DAYS", "0")
    assert tool_sandbox.sweep(app=test_app) == 1  # retried even with retention off
    assert Sandbox.query.count() == 0
    assert (
        "request",
        "DELETE",
        f"/services/{service_id}",
        None,
    ) in unsandbox.calls


def test_an_already_gone_sandbox_counts_as_destroyed(unsandbox, room, monkeypatch):
    sandbox = make_sandbox(unsandbox, room)
    _refuse_deletes(monkeypatch, 404)
    assert tool_sandbox.destroy(sandbox, "test") is True
    assert sandbox.state == "destroyed"


def test_a_busy_destroy_waits_for_the_sweep(unsandbox, room, monkeypatch, capsys):
    sandbox = make_sandbox(unsandbox, room)
    _refuse_deletes(monkeypatch, 409)
    assert tool_sandbox.destroy(sandbox, "test") is False
    assert sandbox.state == "running"
    assert "retried by the hourly sweep" in capsys.readouterr().out


def test_a_new_sandbox_for_an_old_chat_brings_its_setup_steps(unsandbox, room):
    from models import Message, Sandbox, db

    trail = [
        {"tool": "offered", "names": ["sandbox_shell"], "status": "done"},
        {
            "tool": "sandbox_shell",
            "status": "done",
            "label": "clone",
            "detail": {
                "input": "git clone https://git.unturf.com/python/ago.git",
                "exit_code": 0,
            },
        },
        {
            "tool": "sandbox_shell",
            "status": "done",
            "label": "bad",
            "detail": {"input": "make nope", "exit_code": 2},
        },
        {
            "tool": "sandbox_write_file",
            "status": "done",
            "label": "/root/ago/t.py",
            "detail": {"input": "print(1)"},
        },
        {
            "tool": "sandbox_shell",
            "status": "done",
            "label": "deps",
            "detail": {"input": "cd ago && make env", "exit_code": 0},
        },
    ]
    reply = Message(username="qwen", content="Set up.", room_id=room.id)
    reply.tool_trail = json.dumps(trail)
    db.session.add(reply)
    db.session.commit()
    sandbox = make_sandbox(unsandbox, room)
    sandbox.state = "destroyed"
    db.session.commit()

    first = tools.run(fox_in(room), "sandbox_shell", {"command": "ls"})
    note = first["sandbox_note"]
    assert "NEW & EMPTY" in note
    assert note.index("git clone https://git.unturf.com/python/ago.git") < note.index(
        "cd ago && make env"
    )
    assert "(wrote /root/ago/t.py)" in note and "make nope" not in note
    assert len([c for c in unsandbox.calls if c[0] == "create"]) == 2
    assert Sandbox.query.one().state == "running"  # the same row, remade
    second = tools.run(fox_in(room), "sandbox_shell", {"command": "ls"})
    assert "sandbox_note" not in second  # said once


def test_delegate_in_a_remade_sandbox_hands_uncloseai_cli_the_history(unsandbox, room):
    from models import Sandbox, db

    sandbox = make_sandbox(unsandbox, room)
    sandbox.state = "destroyed"
    db.session.commit()
    result = delegate(room, unsandbox, task="run the tests")
    (command,), _ = unsandbox.delegate_commands()
    assert "NEW & EMPTY" in command and "Task: run the tests" in command
    assert "sandbox_note" in result


def test_the_proxy_passes_engine_fields_so_thinking_can_be_turned_off(proxy):
    """uncloseai-cli sends chat_template_kwargs to turn Qwen's thinking off;
    dropping it left the model thinking through its budget, answering
    empty."""
    body = {
        "messages": [{"role": "user", "content": "hi"}],
        "chat_template_kwargs": {"enable_thinking": False},
        "top_k": 20,
        "n": 5,
        "user": "someone",
        "base_url": "http://evil.example/",
    }
    proxy.http.post(
        "/api/sandbox/v1/chat/completions", json=body, headers=proxy.headers
    )
    (ask,) = proxy.completions.asks
    assert ask["extra_body"] == {
        "chat_template_kwargs": {"enable_thinking": False},
        "top_k": 20,
    }
    assert "n" not in ask and "user" not in ask and "base_url" not in ask


def test_a_long_job_rides_out_unsandbox_blips(unsandbox, room, monkeypatch):
    """A 503 while polling a delegate's job once failed the whole delegate."""
    import requests
    import un

    blips = []

    def get_job(job_id):
        if len(blips) < 2:
            response = requests.Response()
            response.status_code = 503
            blips.append(job_id)
            raise requests.HTTPError("503 Service Unavailable", response=response)
        return unsandbox.get_job(job_id)

    monkeypatch.setattr(un, "get_job", get_job)
    result = delegate(room, unsandbox)
    assert len(blips) == 2 and result["answer"] == "Done."


def test_a_real_error_while_polling_still_fails(unsandbox, room, monkeypatch):
    import requests
    import un

    def get_job(job_id):
        response = requests.Response()
        response.status_code = 404
        raise requests.HTTPError("404 Not Found", response=response)

    monkeypatch.setattr(un, "get_job", get_job)
    error = tools.run(fox_in(room), "sandbox_shell", {"command": "ls"})["error"]
    assert "404" in error


def test_a_job_still_running_keeps_being_polled(unsandbox, room, monkeypatch):
    import un

    states = iter(["pending", "running", "running"])

    def get_job(job_id):
        state = next(states, None)
        if state:
            return {"status": state}
        return {"status": "completed", "exit_code": 0, "stdout": "ok"}

    monkeypatch.setattr(un, "get_job", get_job)
    assert tools.run(fox_in(room), "sandbox_shell", {"command": "ls"})["stdout"] == "ok"


def test_delegate_shows_its_progress_while_it_runs(unsandbox, room):
    unsandbox.polls = [
        "● bash git clone https://git.unturf.com/python/ago.git",
        "● bash git clone https://git.unturf.com/python/ago.git",  # unchanged: not repeated
        "● bash make test\n42 passed",
    ]
    unsandbox.answer = "Added 3 tests; 45 passed."
    seen = []
    context = tools.ToolContext(
        sid="s", email=FOX, room_id=room.id, model="qwen", progress=seen.append
    )
    result = delegate(room, unsandbox, context=context)
    assert seen == [
        "● bash git clone https://git.unturf.com/python/ago.git",
        "● bash make test\n42 passed",
    ]
    assert result["answer"] == "Added 3 tests; 45 passed." and result["exit_code"] == 0
    _, polls = unsandbox.delegate_commands()
    # unclose lives in /root/.local/bin, which a bare poll's PATH lacks.
    assert (
        "PATH=/root/.local/bin:$PATH PYTHONUNBUFFERED=1 timeout 6 unclose --peek"
        in polls[0]
    )


def test_a_failing_uncloseai_cli_says_so(unsandbox, room):
    unsandbox.delegate_exit = 3
    result = delegate(room, unsandbox)
    assert result["error"] == "uncloseai-cli exited 3" and result["exit_code"] == 3


def assert_tree_killed(unsandbox):
    stops = [c for c in unsandbox.commands() if c.rstrip().endswith("echo stopped")]
    assert stops and "pgrep -P" in stops[0] and "kill -KILL" in stops[0]


def test_stop_kills_a_running_delegate(unsandbox, room):
    unsandbox.polls = ["working..."] * 5
    pressed = iter([False, True])
    context = tools.ToolContext(
        sid="s",
        email=FOX,
        room_id=room.id,
        model="qwen",
        stopped=lambda: next(pressed, True),
    )
    result = delegate(room, unsandbox, context=context)
    assert result["status"] == "stopped" and "Stop was pressed" in result["error"]
    assert_tree_killed(unsandbox)


def test_stop_between_polls_ends_the_run_without_waiting_for_one(unsandbox, room):
    # Pressed during the wait between polls: no further poll runs.
    unsandbox.polls = ["working..."] * 5
    pressed = iter([False, False, True])
    context = tools.ToolContext(
        sid="s",
        email=FOX,
        room_id=room.id,
        model="qwen",
        stopped=lambda: next(pressed, True),
    )
    result = delegate(room, unsandbox, context=context)
    assert result["status"] == "stopped"
    _, polls = unsandbox.delegate_commands()
    assert polls == []


def test_the_stop_command_kills_the_whole_tree(tmp_path):
    """Run for real: a parent, its child in a new session (as uncloseai-cli
    starts commands), & a grandchild all die."""
    import subprocess

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    script = (
        "setsid bash -c 'sleep 300 & echo $! > "
        + str(run_dir)
        + "/grandchild; wait' & echo $! > "
        + str(run_dir)
        + "/child; wait"
    )
    parent = subprocess.Popen(["bash", "-c", script])
    (run_dir / "pid").write_text(str(parent.pid))
    for _ in range(50):
        if (run_dir / "grandchild").exists() and (run_dir / "child").exists():
            break
        time.sleep(0.1)
    pids = [
        parent.pid,
        int((run_dir / "child").read_text()),
        int((run_dir / "grandchild").read_text()),
    ]
    out = subprocess.run(
        ["bash", "-c", tool_sandbox.stop_command(str(run_dir))],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert out.stdout.strip() == "stopped"
    parent.wait(timeout=10)

    def alive(pid):
        try:
            state = open(f"/proc/{pid}/stat").read().split()[2]
        except FileNotFoundError:
            return False
        return state != "Z"

    assert not any(alive(p) for p in pids)


def test_a_delegate_past_its_time_limit_is_killed(unsandbox, room, monkeypatch):
    monkeypatch.setenv("OPENCOMPLETION_DELEGATE_TIMEOUT_S", "1")
    unsandbox.polls = ["working..."] * 1000
    clock = iter(range(0, 10_000, 10))
    monkeypatch.setattr(tool_sandbox.time, "time", lambda: next(clock))
    result = delegate(room, unsandbox)
    assert (
        result["status"] == "timeout"
        and "OPENCOMPLETION_DELEGATE_TIMEOUT_S" in result["error"]
    )
    assert_tree_killed(unsandbox)


def test_progress_reaches_the_room_as_the_running_line(unsandbox, room):
    """run_round turns a call's progress into its running line, kept with
    the reply as it goes."""
    unsandbox.polls = ["● bash make test"]
    events = []
    calls = [{"id": "c1", "name": "delegate", "arguments": json.dumps({"task": "t"})}]
    tools.run_round(fox_in(room), calls, "", events.append)
    running = [e for e in events if e["status"] == "running"]
    assert running[-1]["progress"] == "● bash make test"
    assert running[-1]["tool"] == "delegate" and running[-1]["where"] == "sandbox"


def test_a_delegates_progress_is_a_log_that_grows(unsandbox, room):
    """Each peek's new rows are pushed under the old ones; overlap is
    dropped, peek's banner never shows, and the finished line keeps the
    log in its fold (fox, 2026-10-31: the box was overwritten)."""
    banner = "  🔒 peek  viewing pid 4242 — read-only, no key reaches it"
    unsandbox.polls = [
        f"{banner}\n[bash] git clone\n↳ bash ok",
        f"{banner}\n[bash] git clone\n↳ bash ok\n[read] Makefile",
        f"{banner}\n[bash] make test\n↳ bash ok · 45 passed",
    ]
    events = []
    calls = [{"id": "c1", "name": "delegate", "arguments": json.dumps({"task": "t"})}]
    tools.run_round(fox_in(room), calls, "", events.append)
    logs = [e["progress"] for e in events if e.get("progress")]
    assert logs[0] == "[bash] git clone\n↳ bash ok"
    assert logs[1] == "[bash] git clone\n↳ bash ok\n[read] Makefile"
    # No overlap with what came before: rows were missed, marked.
    assert logs[2].endswith(
        "[read] Makefile\n⋯\n[bash] make test\n↳ bash ok · 45 passed"
    )
    assert not any("🔒" in log for log in logs)
    done = events[-1]
    assert done["status"] == "done" and done["detail"]["log"] == logs[-1]


VITALS = {
    "schema": "unclose-vitals/1",
    "pid": 4242,
    "status": "working",
    "turn": 7,
    "max_turns": 100,
    "context": {"used": 18234, "peak": 21000, "window": 82000},
    "tools": {"total": 23, "by_name": {"bash": 15, "read": 6, "edit": 2}},
    "activity": {"label": "in tool: bash", "elapsed_s": 8.24},
    "exit_code": None,
    "plan": {"done": 2, "total": 5, "current": "run tests"},
    "files": {"changed": ["ago/tests/test_new.py"], "count": 1},
    "llm": {"calls": 9, "retries": 2, "empty": 1, "last_error": "empty_delivery"},
    "tokens": {"input": 50000, "output": 3000},
    "time": {"elapsed_s": 241.37, "budget_s": None},
    "secret": "never shown",
}


def test_a_delegates_vitals_reach_the_room_shaped(unsandbox, room):
    """uncloseai-cli's unclose-vitals/1 (turn, context, tools, activity)
    rides the running line, cut to known fields; the finished line keeps
    the last of them (fox, 2026-10-31)."""
    final = dict(VITALS, status="done", turn=9, exit_code=0)
    unsandbox.polls = [
        f"__VITALS__ {json.dumps(VITALS)}\n[bash] make test",
        "__VITALS__ {not json\n[bash] still going",
        f"__VITALS__ {json.dumps(final)}\n__EXIT__0\nDone.\n__STDERR__\n",
    ]
    events = []
    calls = [{"id": "c1", "name": "delegate", "arguments": json.dumps({"task": "t"})}]
    tools.run_round(fox_in(room), calls, "", events.append)
    vitals = [
        e["vitals"] for e in events if e["status"] == "running" and e.get("vitals")
    ]
    first = vitals[0]
    assert first["turn"] == 7 and first["max_turns"] == 100
    assert first["context"] == {"used": 18234, "peak": 21000, "window": 82000}
    assert first["tools"] == {
        "total": 23,
        "by_name": {"bash": 15, "read": 6, "edit": 2},
    }
    assert first["activity"] == {"label": "in tool: bash", "elapsed_s": 8.2}
    assert "secret" not in json.dumps(first) and "pid" not in first
    assert first["plan"] == {"done": 2, "total": 5, "current": "run tests"}
    assert first["files"] == {"changed": ["ago/tests/test_new.py"], "count": 1}
    assert first["llm"]["empty"] == 1 and first["tokens"]["input"] == 50000
    assert first["time"] == {"elapsed_s": 241.4, "budget_s": None}
    done = events[-1]
    assert done["status"] == "done" and done["vitals"]["turn"] == 9
    assert done["vitals"]["exit_code"] == 0


def test_the_poll_asks_unclose_for_its_vitals_like_peek(unsandbox, room):
    delegate(room, unsandbox)
    _, polls = unsandbox.delegate_commands()
    assert polls and all("timeout 4 unclose --vitals" in p for p in polls)
    assert all("__VITALS__" in p for p in polls)


def test_our_poll_markers_never_reach_the_reply(unsandbox, room):
    unsandbox.polls = [
        "__EXIT__0\nDone.\n__EXIT__0\n__STDERR__\n(slow call: 45s)\n__EXIT__0\n"
    ]
    result = delegate(room, unsandbox)
    assert result["answer"] == "Done."
    assert result["stderr"] == "(slow call: 45s)"


def test_vitals_of_another_schema_are_ignored():
    from tools.loop import shape_vitals

    assert shape_vitals({"turn": 3}) is None
    assert shape_vitals("x") is None
    shaped = shape_vitals(
        {"schema": "unclose-vitals/1", "turn": -1, "tools": {"total": True}}
    )
    assert shaped["turn"] is None and shaped["tools"]["total"] is None


def test_a_delegate_is_summarized_as_it_goes(unsandbox, room, monkeypatch):
    """A sentence or two on how it goes, asked of the chat's model when the
    log grew (fox, 2026-10-31); a late one never reopens a finished line."""
    from tools import loop

    monkeypatch.setattr(loop, "SUMMARY_EVERY_S", 0)
    monkeypatch.setattr(loop.gevent, "spawn", lambda fn, *a: fn(*a))
    unsandbox.polls = [
        "[bash] git clone",
        "[bash] git clone",  # nothing new: no second summary
        "[bash] git clone\n[bash] make test · 42 passed",
    ]
    asked = []

    def summarize(previous, fresh):
        asked.append((previous, fresh))
        return f"summary {len(asked)}"

    context = dataclasses.replace(fox_in(room), summarize=summarize)
    events = []
    calls = [{"id": "c1", "name": "delegate", "arguments": json.dumps({"task": "t"})}]
    tools.run_round(context, calls, "", events.append)
    assert asked == [
        ("", "[bash] git clone"),
        ("summary 1", "[bash] make test · 42 passed"),
    ]
    said = [e.get("summary") for e in events if e["status"] == "running"]
    assert "summary 1" in said and "summary 2" in said
    assert "summary" not in events[-1]  # the finished line carries none


def test_summarize_delegate_is_best_effort():
    import app as app_module

    class Client:
        def __init__(self, reply=None, fail=False):
            self.reply, self.fail = reply, fail
            self.chat = SimpleNamespace(completions=self)

        def create(self, **kwargs):
            if self.fail:
                raise RuntimeError("down")
            assert kwargs["max_tokens"] == 120 and kwargs["timeout"] == 20
            message = SimpleNamespace(content=self.reply)
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    said = app_module.summarize_delegate(
        Client("  Cloned ago; 42 pass.  "), "m", "", "x"
    )
    assert said == "Cloned ago; 42 pass."
    assert app_module.summarize_delegate(Client(fail=True), "m", "", "x") is None


def test_merge_log_keeps_order_and_drops_peek_noise():
    from tools.loop import merge_log

    log = merge_log([], "a\nb\n  … quiet 30s · 1 session(s) attached\nc")
    assert log == ["a", "b", "c"]
    assert merge_log(log, "b\nc\nd") == ["a", "b", "c", "d"]
    assert merge_log(log, "") == log
    assert merge_log(log, "  (peek stopped: session ended)") == log


def test_a_sandbox_stopped_under_a_command_is_woken_and_retried(
    unsandbox, room, monkeypatch
):
    import requests
    import un
    from models import Sandbox

    make_sandbox(unsandbox, room)
    real = unsandbox.execute_in_service
    refused = []

    def execute(service_id, command, timeout=30000):
        if not refused:
            unsandbox.services[service_id] = "frozen"  # unsandbox stopped it
            response = requests.Response()
            response.status_code = 409
            refused.append(command)
            raise requests.HTTPError(
                "409 Service is not running. Wake it first.", response=response
            )
        return real(service_id, command, timeout)

    monkeypatch.setattr(un, "execute_in_service", execute)
    result = tools.run(fox_in(room), "sandbox_shell", {"command": "ls"})
    assert result["exit_code"] == 0 and refused == ["ls"]
    assert ("unfreeze", Sandbox.query.one().service_id) in unsandbox.calls


def test_a_run_lost_to_a_stopped_sandbox_is_started_again(unsandbox, room):
    unsandbox.polls = ["__LOST__\nKilled"]  # then it finishes: Done.
    seen = []
    context = tools.ToolContext(
        sid="s", email=FOX, room_id=room.id, model="qwen", progress=seen.append
    )
    result = delegate(room, unsandbox, context=context)
    assert result["answer"] == "Done." and result["exit_code"] == 0
    starts, polls = unsandbox.delegate_commands()
    assert len(starts) == 2 and "kill -0" in polls[0]
    assert any("starting uncloseai-cli again" in s for s in seen)


def test_a_run_lost_twice_says_so(unsandbox, room):
    unsandbox.polls = ["__LOST__", "__LOST__"]
    result = delegate(room, unsandbox)
    assert result["status"] == "lost" and "lost twice" in result["error"]


def test_a_sandbox_unsandbox_froze_is_woken_though_our_row_says_running(
    unsandbox, room
):
    from models import Sandbox

    make_sandbox(unsandbox, room)
    sandbox = Sandbox.query.one()
    assert sandbox.state == "running"
    unsandbox.services[sandbox.service_id] = "frozen"  # unsandbox's doing
    result = tools.run(fox_in(room), "sandbox_shell", {"command": "ls"})
    assert result["exit_code"] == 0
    assert ("unfreeze", sandbox.service_id) in unsandbox.calls
