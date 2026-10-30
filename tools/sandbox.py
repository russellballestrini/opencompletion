"""unsandbox for a chat model: one-off code runs & a sandbox per chat.

    run_code        one job, no network (`zerotrust`), Linux or FreeBSD.
                    Files it writes come back as attachments in the chat.
    sandbox_shell   a command in this chat's own sandbox: an unsandbox
    sandbox_write_file  service made on first use from our golden image
                    (OPENCOMPLETION_SANDBOX_IMAGE), `semitrusted` network
                    (unsandbox's egress, never ours), kept across turns,
                    frozen when idle & unfrozen on the next call.
    delegate        a task for uncloseai-cli (`unclose -p`) in that sandbox.

Every call spends this server's unsandbox account (un.py reads
UNSANDBOX_PUBLIC_KEY & UNSANDBOX_SECRET_KEY). Idle sandboxes freeze (no
RAM, disk kept). A sandbox is destroyed only when its chat is deleted, or
once frozen & unused for OPENCOMPLETION_SANDBOX_RETENTION_DAYS (an hourly
sweep); its row stays, state "destroyed", & the chat's next sandbox call
makes it a new one. unsandbox gates destroy behind sudo: turn sudo off for
this server's key, or destroys answer 428 & the sandbox is kept & logged.

uncloseai-cli needs a model. Its sandbox never holds our model keys:
anything model-written runs there with network, so whatever it can read
it can leak. It gets MODEL_ENDPOINT_0 = our /api/sandbox/v1 & a token made
for that one sandbox, in its env vault (never a command line, a log or a
model's context). routes/sandbox_proxy.py takes the token & answers with
our own model & keys; a leaked token reaches only that, & only while the
sandbox is running.
"""

import base64
import hashlib
import json
import os
import secrets
import shlex
import time

import gevent

import un
from blob_store import max_bytes, save_attachment
from models import Message, Sandbox, db
from tools import policy

OUTPUT_CHARS = 8000
LANGUAGES_FREEBSD = ("bash", "sh", "python", "perl", "lua", "c", "cpp")
READY_TIMEOUT_S = 180
# A service unsandbox is upgrading (onto a new golden image) answers
# "Cannot unfreeze service in state: upgrading" & comes back by itself;
# we wait for it, longer than for a start (2026-10-30, demo: a delegate
# failed on that 400 right after the golden rollout).
UPGRADE_TIMEOUT_S = 600
ASLEEP = ("frozen", "stopped", "deep_frozen")


def _keys():
    return un._resolve_credentials(None, None)


def _clip(text, limit=OUTPUT_CHARS):
    text = text or ""
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n[… {len(text) - limit} characters cut …]\n{text[-half:]}"


# unsandbox answers these for a moment now & then (a pool restarting, a
# node busy); waiting on a job or a sandbox rides them out instead of
# failing a long delegate on one blip.
TRANSIENT_STATUSES = (502, 503, 504)


def _transient(error):
    import requests

    if isinstance(error, (requests.ConnectionError, requests.Timeout)):
        return True
    status = getattr(getattr(error, "response", None), "status_code", None)
    return status in TRANSIENT_STATUSES


def _patiently(call, deadline, *args):
    """call(*args), retrying transient failures with backoff until deadline."""
    delay = 2
    while True:
        try:
            return call(*args)
        except Exception as e:  # noqa: BLE001 — only transient ones are retried
            if not _transient(e) or time.time() + delay > deadline:
                raise
            print(f"[WARN] unsandbox blip ({e}); retrying in {delay}s")
            gevent.sleep(delay)
            delay = min(delay * 2, 30)


def _wait_job(job_id, timeout_s):
    """A job's result once it ends: polls get_job (riding out blips) until
    it stops being pending or running, or timeout_s passes."""
    deadline = time.time() + timeout_s
    delay = 0.5
    while True:
        job = _patiently(un.get_job, deadline, job_id)
        if job.get("status") not in ("pending", "running", "queued", None):
            return job
        if time.time() > deadline:
            return {"status": "timeout", "error": f"no result within {timeout_s}s"}
        gevent.sleep(delay)
        delay = min(delay * 1.5, 5)


def _finished(result):
    """What a model reads from a finished job."""
    shaped = {
        "status": result.get("status"),
        "exit_code": result.get("exit_code"),
        "stdout": _clip(result.get("stdout")),
        "stderr": _clip(result.get("stderr")),
    }
    if result.get("error"):
        shaped["error"] = str(result["error"])[:500]
    elif result.get("status") not in ("completed", None):
        shaped["error"] = f"the run ended {result.get('status')}"
    return shaped


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


# -- one-off runs ------------------------------------------------------------


def run_code(context, language, code, os_name="linux"):
    language = str(language or "").strip().lower()
    if not language or not str(code or "").strip():
        return {"error": "language & code required"}
    if os_name not in ("linux", "freebsd"):
        return {"error": "os is linux or freebsd"}
    if os_name == "freebsd" and language not in LANGUAGES_FREEBSD:
        return {
            "error": "FreeBSD runs " + ", ".join(LANGUAGES_FREEBSD) + " for now",
        }
    body = {
        "language": language,
        "code": str(code),
        "network_mode": "zerotrust",
        "ttl": policy.run_code_timeout(),
        "return_artifact": True,
    }
    if os_name == "freebsd":
        body["os"] = "freebsd"
    job = un._make_request("POST", "/execute", *_keys(), body)
    result = _wait_job(job["job_id"], policy.run_code_timeout() + 30)
    shaped = _finished(result)
    shaped["artifacts"] = keep_artifacts(context, result.get("artifacts") or [])
    if not shaped["artifacts"]:
        shaped["note"] = "no files came back: only files saved under /tmp/artifacts/ do"
    return shaped


def keep_artifacts(context, artifacts):
    """A run's files, kept as attachments in its chat: [{name, url,
    mime_type, size}] (a model embeds an image as ![name](url))."""
    kept = []
    for artifact in artifacts[:10]:
        name = artifact.get("filename") or "artifact"
        try:
            data = base64.b64decode(artifact.get("content_base64") or "")
        except ValueError:
            continue
        if not data or len(data) > max_bytes() or context.room_id is None:
            kept.append({"name": name, "skipped": "too large or empty"})
            continue
        attachment = save_attachment(
            context.room_id, data, name, artifact.get("mime_type"), "unsandbox"
        )
        kept.append(
            {
                "name": attachment.filename,
                "url": attachment.url,
                "mime_type": attachment.mime_type,
                "size": attachment.size,
            }
        )
    return kept


# -- a sandbox per chat ------------------------------------------------------


def _state(service):
    return str(service.get("state") or service.get("status") or "").lower()


def _unfreeze(service_id):
    """Ask unsandbox to wake a service. One it says it cannot wake from the
    state it is in (upgrading, starting: it lost a race with our look) is
    left to come up by itself; _wait_running waits for it."""
    try:
        un.unfreeze_service(service_id)
    except Exception as e:  # noqa: BLE001 — only "not from this state" is ours
        response = getattr(e, "response", None)
        said = getattr(response, "text", "") or ""
        if getattr(response, "status_code", None) != 400 or (
            "Cannot unfreeze" not in said
        ):
            raise
        print(f"[WARN] sandbox {service_id}: {said.strip()[:200]}; waiting")


def _wait_running(service_id):
    """Until the service is running. One asleep (frozen or stopped by
    unsandbox, whatever our row says) is woken, once; any other state
    (upgrading, starting, unfreezing) is waited out, an upgrade longer."""
    start = time.time()
    deadline = start + READY_TIMEOUT_S
    woken = False
    while time.time() < deadline:
        state = _state(_patiently(un.get_service, deadline, service_id))
        if state == "running":
            return
        if state in ("error", "failed", "destroyed"):
            raise RuntimeError(f"sandbox {state}")
        if state in ASLEEP and not woken:
            print(f"[WARN] sandbox {service_id} was {state}; waking it")
            _unfreeze(service_id)
            woken = True
        if state == "upgrading":
            deadline = max(deadline, start + UPGRADE_TIMEOUT_S)
        gevent.sleep(2)
    raise RuntimeError(f"sandbox did not start in time (last state: {state})")


def _new_service(context):
    name = f"oc-{context.room_id}-{secrets.token_hex(3)}"
    service = un.create_service(
        name=name, network_mode="semitrusted", golden_image=policy.sandbox_image()
    )
    service_id = service.get("service_id") or service.get("id")
    if not service_id:
        raise RuntimeError("unsandbox made no sandbox")
    return service_id, name


def _create(context):
    service_id, name = _new_service(context)
    sandbox = Sandbox(
        room_id=context.room_id,
        service_id=service_id,
        name=name,
        state="running",
        created_by=context.email,
    )
    db.session.add(sandbox)
    db.session.commit()
    return sandbox


def _recreate(sandbox, context):
    """A chat whose sandbox was destroyed gets a fresh one, in its row, &
    the first result from it says so (see `fresh_note`)."""
    fresh = _new_service(context)
    sandbox.service_id, sandbox.name = fresh
    sandbox.state = "running"
    sandbox.token_sha256 = None
    sandbox.model = None
    db.session.commit()
    _fresh[sandbox.id] = True
    return sandbox


# Sandboxes remade this process, until their first result has said so.
_fresh = {}
SETUP_COMMANDS = 20


def setup_history(room_id):
    """What set up this chat's last sandbox, oldest first: each shell
    command that succeeded & each file written, from its replies' saved
    tool lines (Message.tool_trail), at most SETUP_COMMANDS."""
    steps = []
    replies = (
        Message.visible(room_id)
        .filter(Message.tool_trail.isnot(None))
        .order_by(Message.id)
        .all()
    )
    for reply in replies:
        try:
            trail = json.loads(reply.tool_trail)
        except ValueError:
            continue
        for entry in trail:
            detail = entry.get("detail") or {}
            if entry.get("status") != "done" or not detail.get("input"):
                continue
            if entry.get("tool") == "sandbox_shell" and detail.get("exit_code") in (
                0,
                None,
            ):
                step = detail["input"].strip()
            elif entry.get("tool") == "sandbox_write_file":
                step = f"(wrote {entry.get('label')})"
            else:
                continue
            if step not in steps:
                steps.append(step)
    return steps[-SETUP_COMMANDS:]


def fresh_note(sandbox):
    """For a sandbox remade after its chat's old one was destroyed: a note
    for the model, once, so it rebuilds from the chat's own history instead
    of finding the old files gone mid-task. None otherwise."""
    if not _fresh.pop(sandbox.id, None):
        return None
    steps = setup_history(sandbox.room_id)
    note = (
        "This chat's sandbox is NEW & EMPTY: its old one was destroyed after "
        "going unused, so earlier files, clones & installs are gone."
    )
    if steps:
        note += (
            " These steps set the old one up, oldest first; re-run what this "
            "task needs (one command can chain several):\n" + "\n".join(steps)
        )
    return note


def _with_fresh_note(sandbox, result):
    note = fresh_note(sandbox)
    if note:
        result["sandbox_note"] = note
    return result


def ready(context):
    """This chat's sandbox, made or unfrozen as needed, running."""
    if context.room_id is None:
        raise RuntimeError("no chat to keep a sandbox for")
    sandbox = Sandbox.query.filter_by(room_id=context.room_id).first()
    if sandbox is None:
        sandbox = _create(context)
    elif sandbox.state == "destroyed":
        sandbox = _recreate(sandbox, context)
    elif sandbox.state == "frozen":
        # Our row says frozen; _wait_running asks unsandbox what it really
        # is & wakes it only if it is asleep (not mid-upgrade).
        sandbox.state = "running"
    _wait_running(sandbox.service_id)
    sandbox.last_used_at = time.time()
    db.session.commit()
    return sandbox


def freeze_if_idle(sandbox_id, app=None):
    """Freeze a sandbox nobody used for OPENCOMPLETION_SANDBOX_IDLE_S.
    Freezing frees its RAM & ends its token's use; nothing is deleted."""
    app = app or _app
    if app is None:
        return
    with app.app_context():
        sandbox = db.session.get(Sandbox, sandbox_id)
        if sandbox is None or sandbox.state != "running":
            return
        if time.time() - (sandbox.last_used_at or 0) < policy.sandbox_idle():
            return
        try:
            un.freeze_service(sandbox.service_id)
        except Exception as e:  # noqa: BLE001 — try again on its next use
            print(f"[WARN] could not freeze sandbox {sandbox.name}: {e}")
            return
        idle = time.time() - (sandbox.last_used_at or 0)
        print(f"[INFO] froze sandbox {sandbox.name} after {idle:.0f}s idle")
        sandbox.state = "frozen"
        db.session.commit()


def destroy(sandbox, why):
    """Destroy a sandbox's unsandbox service & mark its row destroyed. A
    plain signed DELETE: un.delete_service would ask for a sudo code on a
    428 with input(), which a server can't answer. True when it went."""
    try:
        un._make_request("DELETE", f"/services/{sandbox.service_id}", *_keys())
    except Exception as e:  # noqa: BLE001 — kept, & said so
        status = getattr(getattr(e, "response", None), "status_code", None)
        hint = " (turn sudo off for this key)" if status == 428 else ""
        print(f"[WARN] could not destroy sandbox {sandbox.name} ({why}): {e}{hint}")
        return False
    print(f"[INFO] destroyed sandbox {sandbox.name} {sandbox.service_id} ({why})")
    sandbox.state = "destroyed"
    sandbox.token_sha256 = None
    db.session.commit()
    return True


def forget_room(room_id):
    """A chat is being deleted: destroy its sandbox & drop the row (it
    points at the room). One that won't destroy is logged with its id, so
    it can be destroyed by hand."""
    sandbox = Sandbox.query.filter_by(room_id=room_id).first()
    if sandbox is None:
        return
    if sandbox.state != "destroyed" and not destroy(sandbox, "its chat was deleted"):
        print(
            f"[WARN] sandbox {sandbox.service_id} outlives its chat: destroy it by hand"
        )
    db.session.delete(sandbox)
    db.session.commit()


def sweep(app=None):
    """Destroy sandboxes frozen & unused past the retention window."""
    days = policy.sandbox_retention_days()
    app = app or _app
    if not days or app is None:
        return 0
    cutoff = time.time() - days * 86400
    destroyed = 0
    with app.app_context():
        stale = Sandbox.query.filter(
            Sandbox.state == "frozen", Sandbox.last_used_at < cutoff
        ).all()
        for sandbox in stale:
            destroyed += destroy(sandbox, f"unused for {days:g} days")
    return destroyed


SWEEP_EVERY_S = 3600


def _sweep_forever():
    while True:
        gevent.sleep(SWEEP_EVERY_S)
        try:
            sweep()
        except Exception as e:  # noqa: BLE001 — try again next hour
            print(f"[WARN] sandbox sweep failed: {e}")


_app = None


def init_app(app):
    """The Flask app our idle timers & hourly sweep open a context on."""
    global _app
    _app = app
    if policy.sandbox_enabled() and not os.environ.get("TESTING"):
        gevent.spawn(_sweep_forever)


def _execute(sandbox, command, timeout_s):
    """Run `command` in a sandbox. It counts as in use until the command
    could end, so a long delegate is never frozen under itself; its idle
    clock starts when the command does end."""
    sandbox.last_used_at = time.time() + timeout_s
    db.session.commit()
    try:
        try:
            job = un.execute_in_service(
                sandbox.service_id, command, timeout=timeout_s * 1000
            )
        except Exception as e:  # noqa: BLE001 — one kind is ours to fix
            if getattr(getattr(e, "response", None), "status_code", None) != 409:
                raise
            # "Service is not running. Wake it first.": it stopped under
            # us (frozen, or its host restarted). Wake it & try once more.
            print(f"[WARN] sandbox {sandbox.name} was not running; waking it")
            sandbox.state = "running"
            db.session.commit()
            _wait_running(sandbox.service_id)
            job = un.execute_in_service(
                sandbox.service_id, command, timeout=timeout_s * 1000
            )
        if job.get("job_id"):
            return _wait_job(job["job_id"], timeout_s + 30)
        return job
    finally:
        sandbox.last_used_at = time.time()
        db.session.commit()
        gevent.spawn_later(policy.sandbox_idle() + 1, freeze_if_idle, sandbox.id)


def shell(context, command):
    command = str(command or "").strip()
    if not command:
        return {"error": "command required"}
    sandbox = ready(context)
    return _with_fresh_note(
        sandbox, _finished(_execute(sandbox, command, policy.sandbox_timeout()))
    )


def write_file(context, path, content):
    path = str(path or "").strip()
    if not path:
        return {"error": "path required"}
    encoded = base64.b64encode(str(content or "").encode()).decode()
    quoted = shlex.quote(path)
    command = (
        f'mkdir -p "$(dirname {quoted})" && '
        f"printf %s {shlex.quote(encoded)} | base64 -d > {quoted} && wc -c < {quoted}"
    )
    sandbox = ready(context)
    result = _finished(_execute(sandbox, command, policy.sandbox_timeout()))
    if result.get("exit_code") == 0:
        result = {"path": path, "bytes": int((result["stdout"] or "0").strip() or 0)}
    return _with_fresh_note(sandbox, result)


# -- uncloseai-cli in the sandbox --------------------------------------------


def arm_proxy(sandbox, model):
    """A fresh token for this sandbox, in its env vault, & our proxy set to
    answer it with `model`. The token is never returned or logged."""
    token = "ocsb-" + secrets.token_urlsafe(32)
    un.set_service_env(
        sandbox.service_id,
        {
            "MODEL_ENDPOINT_0": policy.public_url() + "/api/sandbox/v1",
            "MODEL_API_KEY_0": token,
            "MODEL_ID_0": model,
            "MODEL_LABEL_0": "opencompletion",
            # vLLM's qwen tool parser strips text-format <tool_call> from
            # content (a request without tools comes back empty), so
            # uncloseai-cli declares native tools from its first turn, as
            # fox's own development.env does for qwen.
            "UNCLOSEAI_NATIVE_TOOLS": "on",
        },
    )
    sandbox.token_sha256 = token_hash(token)
    sandbox.model = model
    db.session.commit()


DELEGATE_POLL_S = 15
DELEGATE_RESTARTS = 1  # a run lost to a stopped sandbox is started again
DELEGATE_DIR = "/root/.opencompletion"
ANSI = r"s/\x1b\[[0-9;?]*[A-Za-z]//g"


def delegate(context, task):
    """uncloseai-cli (`unclose -p`) on a task in this chat's sandbox, run in
    the background: we poll it every DELEGATE_POLL_S with a short command,
    showing `unclose --peek`'s view of its session as progress, until it
    writes its exit code. Each poll is its own quick job, so a moment of
    unsandbox unavailability costs one poll, not the run; Stop or the time
    limit kills it."""
    task = str(task or "").strip()
    if not task:
        return {"error": "task required"}
    sandbox = ready(context)
    model = context.model or sandbox.model
    # Armed afresh for every run: a new token, & the vault's settings as
    # this server has them now (an older sandbox catches up).
    arm_proxy(sandbox, model)
    note = fresh_note(sandbox)
    if note:
        # uncloseai-cli starts in the new sandbox: it needs the history too.
        task = f"{note}\n\nTask: {task}"
    run_dir = shlex.quote(f"{DELEGATE_DIR}/delegate-{secrets.token_hex(4)}")
    inner = (
        ". /run/unsandbox/vault.sh 2>/dev/null; export PATH=/root/.local/bin:$PATH; "
        f"unclose -p --no-markdown --model {shlex.quote(model)} {shlex.quote(task)} "
        f"> {run_dir}/answer 2> {run_dir}/stderr & echo $! > {run_dir}/pid; "
        f"wait $!; echo $? > {run_dir}/exit"
    )
    start = (
        f"mkdir -p {run_dir} && cd /root && "
        f"setsid nohup bash -c {shlex.quote(inner)} > /dev/null 2>&1 < /dev/null & "
        f"sleep 1; cat {run_dir}/pid"
    )

    def launch():
        started = _execute(sandbox, start, 60)
        if (
            started.get("status") != "completed"
            or not (started.get("stdout") or "").strip()
        ):
            result = _finished(started)
            result.setdefault("error", "uncloseai-cli did not start")
            return result
        return None

    failed = launch()
    if failed:
        return failed
    restarts = 0
    poll = (
        f"if [ -f {run_dir}/exit ]; then echo __EXIT__$(cat {run_dir}/exit); "
        f"cat {run_dir}/answer; echo __STDERR__; tail -c 3000 {run_dir}/stderr; "
        f"elif ! kill -0 $(cat {run_dir}/pid 2>/dev/null) 2>/dev/null; then "
        f"echo __LOST__; tail -c 2000 {run_dir}/stderr 2>/dev/null; "
        f"else PATH=/root/.local/bin:$PATH timeout 6 unclose --peek "
        f"$(cat {run_dir}/pid) 2>&1 | sed -E '{ANSI}' "
        f"| tail -n 12; fi"
    )
    stop = f"kill $(cat {run_dir}/pid) 2>/dev/null; echo stopped"
    deadline = time.time() + policy.delegate_timeout()
    shown = ""
    while True:
        if context.stopped and context.stopped():
            _execute(sandbox, stop, 30)
            return {"status": "stopped", "error": "stopped: Stop was pressed"}
        if time.time() > deadline:
            _execute(sandbox, stop, 30)
            return {
                "status": "timeout",
                "error": f"uncloseai-cli did not finish within "
                f"{policy.delegate_timeout()}s (OPENCOMPLETION_DELEGATE_TIMEOUT_S)",
            }
        gevent.sleep(DELEGATE_POLL_S)
        try:
            polled = _execute(sandbox, poll, 30)
        except Exception as e:  # noqa: BLE001 — a blip costs one poll
            if _transient(e):
                continue
            raise
        out = polled.get("stdout") or ""
        if out.startswith("__LOST__") and restarts < DELEGATE_RESTARTS:
            # The sandbox stopped under it (a freeze or a host restart ends
            # every process in it): wake it & start the run again, once.
            restarts += 1
            if context.progress:
                context.progress(
                    "The sandbox stopped mid-run; starting uncloseai-cli again."
                )
            _wait_running(sandbox.service_id)
            failed = launch()
            if failed:
                return failed
            continue
        if out.startswith("__LOST__"):
            # Lost again: say so rather than loop.
            return {
                "status": "lost",
                "error": "uncloseai-cli's run was lost twice: the sandbox stopped "
                "mid-run & again after a restart",
                "stderr": _clip(out[len("__LOST__") :].strip()),
            }
        if out.startswith("__EXIT__"):
            head, _, rest = out.partition("\n")
            answer, _, stderr = rest.partition("__STDERR__\n")
            try:
                code = int(head[len("__EXIT__") :].strip())
            except ValueError:
                code = None
            result = {
                "status": "completed",
                "exit_code": code,
                "answer": _clip(answer.rstrip()),
                "stderr": _clip(stderr.rstrip()),
            }
            if code != 0:
                result["error"] = f"uncloseai-cli exited {code}"
            if note:
                result["sandbox_note"] = note
            return result
        progress = out.strip()
        if progress and progress != shown and context.progress:
            context.progress(progress)
            shown = progress
