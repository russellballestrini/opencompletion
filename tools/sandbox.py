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
import math
import mimetypes
import os
import re
import secrets
import shlex
import time

import gevent

import un
from blob_store import attachment_json, max_bytes, save_attachment
from models import Message, Sandbox, db
from tools import oversight, policy

OUTPUT_CHARS = 8000
LANGUAGES_FREEBSD = ("bash", "sh", "python", "perl", "lua", "c", "cpp")
READY_TIMEOUT_S = 180
# A service unsandbox is upgrading (onto a new golden image) answers
# "Cannot unfreeze service in state: upgrading" & comes back by itself;
# we wait for it, longer than for a start (2026-10-30, demo: a delegate
# failed on that 400 right after the golden rollout).
UPGRADE_TIMEOUT_S = 600
UPGRADING = ("upgrading", "deep_upgrading")
# What unsandbox's unfreeze wakes (audit 2026-10-30): frozen, & unreachable
# (its host came back). "failed" stays an error: waking it repairs nothing.
ASLEEP = ("frozen", "unreachable")


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
    """A run's files, kept as attachments in its chat, each described by
    blob_store.attachment_json (its `markdown` is what a reply pastes)."""
    kept = []
    for artifact in artifacts[:10]:
        name = artifact.get("filename") or "artifact"
        try:
            data = base64.b64decode(artifact.get("content_base64") or "")
        except ValueError:
            continue
        if not data or len(data) > max_bytes() or context.room_id is None:
            kept.append({"filename": name, "skipped": "too large or empty"})
            continue
        attachment = save_attachment(
            context.room_id, data, name, artifact.get("mime_type"), "unsandbox"
        )
        kept.append(attachment_json(attachment))
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
        # Today a 400 "Cannot unfreeze service in state: X"; the API's error
        # contract adds code "service_busy" (& later answers 409) for it.
        busy = '"service_busy"' in said or (
            getattr(response, "status_code", None) == 400 and "Cannot unfreeze" in said
        )
        if not busy:
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
        if state in UPGRADING:
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
    428 with input(), which a server can't answer. True when it went (or
    was already gone); False leaves it for the hourly sweep to retry."""
    try:
        un._make_request("DELETE", f"/services/{sandbox.service_id}", *_keys())
    except Exception as e:  # noqa: BLE001 — kept, & said so
        response = getattr(e, "response", None)
        status = getattr(response, "status_code", None)
        if status != 404:
            if status == 428:
                hint = (
                    " (turn sudo off for this key on unsandbox: no destroy"
                    " can succeed until it is)"
                )
            elif status == 409:
                # The API refuses while the sandbox is upgrading, unfreezing
                # or migrating (service_busy, 2026-10-30): the sweep retries.
                hint = " (busy; retried by the hourly sweep)"
            else:
                hint = ""
            print(f"[WARN] could not destroy sandbox {sandbox.name} ({why}): {e}{hint}")
            return False
        print(f"[INFO] sandbox {sandbox.name} was already gone ({why})")
    print(f"[INFO] destroyed sandbox {sandbox.name} {sandbox.service_id} ({why})")
    sandbox.state = "destroyed"
    sandbox.token_sha256 = None
    db.session.commit()
    return True


def forget_room(room_id):
    """A chat is being deleted: destroy its sandbox & drop the row. One that
    won't destroy now (busy, a sudo-gated key, unsandbox down) is kept as
    destroy_pending, detached from the chat & its token dead, so the hourly
    sweep retries it: it used to be logged & forgotten, left running with
    nothing on our side that knew it existed (2026-10-31)."""
    sandbox = Sandbox.query.filter_by(room_id=room_id).first()
    if sandbox is None:
        return
    if sandbox.state == "destroyed" or destroy(sandbox, "its chat was deleted"):
        db.session.delete(sandbox)
    else:
        sandbox.room_id = None
        sandbox.state = "destroy_pending"
        sandbox.token_sha256 = None
    db.session.commit()


def sweep(app=None):
    """Retry destroying deleted chats' sandboxes (destroy_pending), then
    destroy sandboxes unused past the retention window, frozen or not: one
    whose idle freeze failed (it was mid-upgrade) still says running."""
    days = policy.sandbox_retention_days()
    app = app or _app
    if app is None:
        return 0
    destroyed = 0
    with app.app_context():
        pending = Sandbox.query.filter_by(state="destroy_pending").all()
        for sandbox in pending:
            if destroy(sandbox, "its chat was deleted (retry)"):
                db.session.delete(sandbox)
                db.session.commit()
                destroyed += 1
        if not days:
            return destroyed
        cutoff = time.time() - days * 86400
        stale = Sandbox.query.filter(
            Sandbox.state.in_(("frozen", "running")),
            Sandbox.last_used_at < cutoff,
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


def _execute(sandbox, command, timeout_s, on_job=None, job_id=None):
    """Run `command` in a sandbox. It counts as in use until the command
    could end, so a long delegate is never frozen under itself; its idle
    clock starts when the command does end. on_job(job_id) hears the job
    unsandbox started (a checkpoint); job_id instead waits on one already
    started (a command our server restarted in the middle of)."""
    sandbox.last_used_at = time.time() + timeout_s
    db.session.commit()
    try:
        if job_id:
            return _wait_job(job_id, timeout_s + 30)
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
            if on_job:
                on_job(job["job_id"])
            return _wait_job(job["job_id"], timeout_s + 30)
        return job
    finally:
        sandbox.last_used_at = time.time()
        db.session.commit()
        gevent.spawn_later(policy.sandbox_idle() + 1, freeze_if_idle, sandbox.id)


def shell(context, command):
    """One command in this chat's sandbox. It runs as an unsandbox job, which
    finishes on unsandbox even if our server restarts, so its job id is
    checkpointed & a resumed reply collects that job's result rather than
    running the command again (fox, 2026-10-31)."""
    command = str(command or "").strip()
    if not command:
        return {"error": "command required"}
    resumed = context.resume_run or {}
    if resumed.get("job_id"):
        sandbox = db.session.get(Sandbox, resumed.get("sandbox_id"))
        if sandbox is None or sandbox.state in ("destroyed", "destroy_pending"):
            return {"error": "the sandbox this command ran in is gone"}
        result = _execute(
            sandbox, command, policy.sandbox_timeout(), job_id=resumed["job_id"]
        )
        return _finished(result)
    sandbox = ready(context)
    on_job = None
    if context.checkpoint:

        def on_job(job_id):
            context.checkpoint({"sandbox_id": sandbox.id, "job_id": job_id})

    return _with_fresh_note(
        sandbox,
        _finished(_execute(sandbox, command, policy.sandbox_timeout(), on_job=on_job)),
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


# A file leaves a sandbox as base64 on stdout, this many bytes a command,
# so no one job's output grows past what unsandbox returns comfortably.
SHARE_CHUNK_BYTES = 1024 * 1024


def share_file(context, path):
    """A file in this chat's sandbox, kept as an attachment in its chat:
    what a model hands the person (a deck, a build, an image) since the
    sandbox's disk is never theirs to browse. Read in chunks & checked
    against its sha256, under the upload size limit."""
    path = str(path or "").strip()
    if not path:
        return {"error": "path required"}
    if context.room_id is None:
        return {"error": "no chat to keep a file in"}
    quoted = shlex.quote(path)
    sandbox = ready(context)
    probe = _execute(
        sandbox,
        f'p={quoted}; if [ -d "$p" ]; then echo __DIR__; '
        f'elif [ ! -f "$p" ]; then echo __MISSING__; '
        f'else stat -c %s "$p" && sha256sum "$p" | cut -d" " -f1 && '
        f'(file -b --mime-type "$p" 2>/dev/null || echo); fi',
        policy.sandbox_timeout(),
    )
    lines = str(probe.get("stdout") or "").splitlines()
    if probe.get("status") not in ("completed", None) or not lines:
        return _finished(probe) | {"error": "could not read the sandbox"}
    if lines[0] == "__DIR__":
        return {
            "error": f"{path} is a folder: zip it first (cd {quoted} && "
            "zip -qr /root/out.zip .) & share the zip"
        }
    if lines[0] == "__MISSING__" or not lines[0].isdigit():
        return {"error": f"no file at {path} in this chat's sandbox"}
    size, digest = int(lines[0]), (lines[1] if len(lines) > 1 else "")
    mime = lines[2].strip() if len(lines) > 2 else ""
    if size > max_bytes():
        return {
            "error": f"{path} is {size} bytes; files up to {max_bytes()} bytes "
            "can be shared (compress it or make it smaller)"
        }
    data = b""
    while len(data) < size:
        chunk = _execute(
            sandbox,
            f"tail -c +{len(data) + 1} {quoted} | head -c {SHARE_CHUNK_BYTES} "
            "| base64 -w0",
            policy.sandbox_timeout(),
        )
        try:
            piece = base64.b64decode(str(chunk.get("stdout") or "").strip())
        except ValueError:
            piece = b""
        if chunk.get("exit_code") not in (0, None) or not piece:
            return {"error": f"reading {path} stopped at byte {len(data)}"}
        data += piece
    if hashlib.sha256(data).hexdigest() != digest:
        return {"error": f"{path} changed while it was read; share it again"}
    name = path.rstrip("/").rsplit("/", 1)[-1] or "file"
    attachment = save_attachment(
        context.room_id,
        data,
        name,
        mimetypes.guess_type(name)[0] or mime,
        "sandbox",
    )
    return {
        "path": path,
        "artifacts": [attachment_json(attachment)],
        "note": "paste its markdown into your reply to give the person the file",
    }


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
# A review waits this long for its overseer, then lets the run go on.
OVERSEER_TIMEOUT_S = 30
DELEGATE_RESTARTS = 1  # a run lost to a stopped sandbox is started again
DELEGATE_DIR = "/root/.opencompletion"
ANSI = r"s/\x1b\[[0-9;?]*[A-Za-z]//g"


_MARKER_LINE = re.compile(r"(?m)^__(?:EXIT|VITALS|STDERR|LOST)__.*\n?")


def _unmarked(text):
    """Text from a poll without our own marker lines: they are ours, never
    the run's (one reached a reply's stderr box, 2026-10-31)."""
    return _MARKER_LINE.sub("", text or "")


def stop_command(run_dir):
    """Kill a delegate run & everything it started: uncloseai-cli runs its
    commands in their own sessions, so killing its pid alone left a long
    `make test` running on (2026-10-31). The tree is walked through
    parent pids (pgrep -P), TERM first, then KILL for what ignored it."""
    return (
        f"P=$(cat {run_dir}/pid 2>/dev/null); "
        'if [ -n "$P" ]; then T=$P; N=$P; '
        'while [ -n "$N" ]; do N=$(for p in $N; do pgrep -P "$p"; done); T="$T $N"; done; '
        "kill -TERM $T 2>/dev/null; sleep 2; kill -KILL $T 2>/dev/null; fi; "
        "echo stopped"
    )


def delegate(context, task):
    """_delegate, with its proxy token revoked however the run ends
    (finished, stopped, failed, timed out, raised): a token that outlived
    its run let anything left in the sandbox keep calling our model while
    the sandbox ran, & came back to life when it was unfrozen (2026-10-30)."""
    armed = {}
    try:
        return _delegate(context, task, armed)
    finally:
        revoke_proxy(armed.get("sandbox_id"), armed.get("token_sha256"))


def _review(context, sandbox, run_dir, task, watch, vitals, log_tail):
    """Ask the run's overseer (the chat's model) how it goes & act on it:
    continue, steer (a note the run reads at its next turn, via
    `unclose --steer`), or stop. A failed review is a continue."""
    now = time.monotonic()
    digest = {
        "task": task,
        "elapsed": oversight.minutes(now - watch.started),
        "since_progress": oversight.minutes(now - watch.last_progress),
        "vitals": vitals or {},
        "log_tail": log_tail,
    }
    try:
        with gevent.Timeout(OVERSEER_TIMEOUT_S):
            said = context.oversee(digest)
    except BaseException as e:  # noqa: BLE001 — a review is optional; Timeout too
        if isinstance(e, (KeyboardInterrupt, SystemExit)):
            raise
        print(f"[WARN] overseer review skipped: {e or 'timed out'}")
        said = None
    decision, note = oversight.parse_decision(said)
    verdict = {"decision": decision, "note": note, "at": digest["elapsed"]}
    if decision == "steer":
        steered = _execute(
            sandbox,
            "PATH=/root/.local/bin:$PATH unclose --steer "
            f"$(cat {run_dir}/pid) {shlex.quote(note)} 2>&1",
            30,
        )
        if steered.get("exit_code") not in (0, None):
            # An uncloseai-cli without a steering inbox (older image): the
            # note is shown, not delivered.
            verdict["undelivered"] = True
    return verdict


def revoke_proxy(sandbox_id, armed_hash):
    """Forget a run's proxy token, unless a newer run has armed its own
    since (that run's token stays good). Never raises: it runs on every
    way out of a delegate."""
    if not sandbox_id or not armed_hash:
        return
    try:
        db.session.rollback()  # a failed run may have left one open
        sandbox = db.session.get(Sandbox, sandbox_id)
        if sandbox is not None and sandbox.token_sha256 == armed_hash:
            sandbox.token_sha256 = None
            db.session.commit()
    except Exception as e:  # noqa: BLE001 — revocation must not mask the result
        db.session.rollback()
        print(f"[WARN] could not revoke sandbox {sandbox_id}'s proxy token: {e}")


def _delegate(context, task, armed):
    """uncloseai-cli (`unclose -p`) on a task in this chat's sandbox, run in
    the background: we poll it every DELEGATE_POLL_S with a short command,
    showing `unclose --peek`'s view of its session as progress, until it
    writes its exit code. Each poll is its own quick job, so a moment of
    unsandbox unavailability costs one poll, not the run; Stop or the time
    limit kills it."""
    task = str(task or "").strip()
    if not task:
        return {"error": "task required"}
    resumed = context.resume_run
    if resumed:
        # Our server restarted while uncloseai-cli ran: the run went on in
        # its sandbox, so reattach to it rather than start it again (fox,
        # 2026-10-31). Its token is still armed; revoked when it ends.
        sandbox = db.session.get(Sandbox, resumed.get("sandbox_id"))
        if sandbox is None or sandbox.state in ("destroyed", "destroy_pending"):
            return {"error": "the sandbox this run was in is gone"}
        _wait_running(sandbox.service_id)
        armed.update(sandbox_id=sandbox.id, token_sha256=resumed.get("token_sha256"))
        model = context.model or sandbox.model
        note = None
        run_dir = resumed["run_dir"]
    else:
        sandbox = ready(context)
        model = context.model or sandbox.model
        # Armed afresh for every run: a new token, & the vault's settings
        # as this server has them now (an older sandbox catches up).
        arm_proxy(sandbox, model)
        armed.update(sandbox_id=sandbox.id, token_sha256=sandbox.token_sha256)
        note = fresh_note(sandbox)
        if note:
            # uncloseai-cli starts in the new sandbox: it needs the history.
            task = f"{note}\n\nTask: {task}"
        run_dir = shlex.quote(f"{DELEGATE_DIR}/delegate-{secrets.token_hex(4)}")
    # A chat's delegates share one uncloseai-cli session: a new one resumes
    # it, remembering the work before (fox, 2026-10-31). A new sandbox has
    # none; one that will not resume falls back to a fresh start.
    session = {
        "id": None if (resumed or note) else sandbox.delegate_session_id,
        "resumed": False,
    }

    def start_command():
        resume = f"--resume {shlex.quote(session['id'])} " if session["id"] else ""
        inner = (
            ". /run/unsandbox/vault.sh 2>/dev/null; export PATH=/root/.local/bin:$PATH; "
            f"unclose {resume}-p --no-markdown --model {shlex.quote(model)} "
            f"{shlex.quote(task)} "
            f"> {run_dir}/answer 2> {run_dir}/stderr & echo $! > {run_dir}/pid; "
            f"wait $!; echo $? > {run_dir}/exit"
        )
        return (
            f"mkdir -p {run_dir} && rm -f {run_dir}/exit && cd /root && "
            f"setsid nohup bash -c {shlex.quote(inner)} > /dev/null 2>&1 < /dev/null & "
            f"sleep 1; cat {run_dir}/pid"
        )

    def launch():
        session["resumed"] = bool(session["id"])
        session["launched_at"] = time.monotonic()
        started = _execute(sandbox, start_command(), 60)
        if (
            started.get("status") != "completed"
            or not (started.get("stdout") or "").strip()
        ):
            result = _finished(started)
            result.setdefault("error", "uncloseai-cli did not start")
            return result
        return None

    # No clock limit unless OPENCOMPLETION_DELEGATE_TIMEOUT_S sets one:
    # progress & its overseer decide (tools/oversight.py).
    limit = policy.delegate_timeout()
    if resumed:
        deadline = float(resumed.get("deadline") or 0) or None
    else:
        failed = launch()
        if failed:
            return failed
        deadline = time.time() + limit if limit else None
        if context.checkpoint:
            # Where to find this run again if our server restarts.
            context.checkpoint(
                {
                    "sandbox_id": sandbox.id,
                    "run_dir": run_dir,
                    "deadline": deadline,
                    "token_sha256": sandbox.token_sha256,
                }
            )
    restarts = 0
    # First line: uncloseai-cli's vitals (`unclose --vitals <pid>`, the
    # unclose-vitals/1 JSON: plan, turn, context, tools, files, model
    # health, tokens, time), asked like peek; an older uncloseai-cli
    # without it prints nothing here.
    poll = (
        f"V=$(PATH=/root/.local/bin:$PATH timeout 4 unclose --vitals "
        f"$(cat {run_dir}/pid 2>/dev/null) 2>/dev/null | head -c 20000 | tr -d '\\n'); "
        f'[ -n "$V" ] && printf \'__VITALS__ %s\\n\' "$V"; '
        f"if [ -f {run_dir}/exit ]; then echo __EXIT__$(cat {run_dir}/exit); "
        f"cat {run_dir}/answer; echo __STDERR__; tail -c 3000 {run_dir}/stderr; "
        f"elif ! kill -0 $(cat {run_dir}/pid 2>/dev/null) 2>/dev/null; then "
        f"echo __LOST__; tail -c 2000 {run_dir}/stderr 2>/dev/null; "
        # PYTHONUNBUFFERED: peek follows until `timeout` kills it, & a
        # block-buffered pipe dies with it unflushed: every poll came back
        # empty & the progress line never moved (2026-10-31).
        f"else PATH=/root/.local/bin:$PATH PYTHONUNBUFFERED=1 timeout 6 unclose --peek "
        f"$(cat {run_dir}/pid) 2>&1 | sed -E '{ANSI}' "
        f"| tail -n 12; fi"
    )
    stop = stop_command(run_dir)
    next_tick = time.monotonic() + DELEGATE_POLL_S
    shown = None
    last_vitals = None
    watch = oversight.Watch(time.monotonic())
    card = {"session": {"resumed": session["resumed"]}}

    def over():
        return bool(deadline) and time.time() > deadline

    while True:
        if context.stopped and context.stopped():
            _execute(sandbox, stop, 30)
            return {"status": "stopped", "error": "stopped: Stop was pressed"}
        if over():
            _execute(sandbox, stop, 30)
            return {
                "status": "timeout",
                "error": f"uncloseai-cli did not finish within "
                f"{limit}s (OPENCOMPLETION_DELEGATE_TIMEOUT_S)",
            }
        # One tick every DELEGATE_POLL_S, start to start: the poll & the
        # summary it may wait for come out of the same 15s, so the card
        # updates on a steady beat (fox, 2026-10-31). Between ticks, look
        # for Stop every second.
        for _ in range(math.ceil(max(0.0, next_tick - time.monotonic()))):
            if (context.stopped and context.stopped()) or over():
                break
            gevent.sleep(min(1, max(0.0, next_tick - time.monotonic())))
        if (context.stopped and context.stopped()) or over():
            continue  # the top of the loop stops it & says why
        next_tick = time.monotonic() + DELEGATE_POLL_S
        try:
            polled = _execute(sandbox, poll, 30)
        except Exception as e:  # noqa: BLE001 — a blip costs one poll
            if _transient(e):
                continue
            raise
        out = polled.get("stdout") or ""
        vitals = None
        if out.startswith("__VITALS__ "):
            line, _, out = out.partition("\n")
            try:
                vitals = json.loads(line[len("__VITALS__ ") :])
            except ValueError:
                vitals = None
            if vitals is not None:
                last_vitals = vitals
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
            if (
                code != 0
                and session["resumed"]
                and time.monotonic() - session.get("launched_at", 0) < 120
            ):
                # Its session would not resume (gone, or unreadable): start
                # fresh, once, & forget it.
                print(f"[WARN] sandbox {sandbox.name}: session would not resume")
                session["id"] = None
                sandbox.delegate_session_id = None
                db.session.commit()
                failed = launch()
                if failed:
                    return failed
                card["session"] = {"resumed": False}
                continue
            result = {
                "status": "completed",
                "exit_code": code,
                "answer": _clip(_unmarked(answer).rstrip()),
                "stderr": _clip(_unmarked(stderr).rstrip()),
            }
            if code != 0:
                result["error"] = f"uncloseai-cli exited {code}"
            if note:
                result["sandbox_note"] = note
            if last_vitals is not None and context.progress:
                extra = {"card": card} if card.get("oversight") else {}
                context.progress("", vitals=last_vitals, **extra)  # final numbers
            return result
        progress = out.strip()
        now = time.monotonic()
        watch.observe(vitals, progress, now)
        if watch.session_id and watch.session_id != sandbox.delegate_session_id:
            sandbox.delegate_session_id = watch.session_id  # the next resumes it
            db.session.commit()
        if watch.stalled(now):
            _execute(sandbox, stop, 30)
            return {
                "status": "stalled",
                "error": "stopped: no progress for "
                f"{oversight.minutes(now - watch.last_progress)} (no plan step, "
                "file change or new activity; OPENCOMPLETION_DELEGATE_STALL_S)",
            }
        if watch.review_due(now) and context.oversee:
            watch.reviewed(now)
            verdict = _review(context, sandbox, run_dir, task, watch, vitals, progress)
            card["oversight"] = verdict
            if verdict["decision"] == "stop":
                _execute(sandbox, stop, 30)
                return {
                    "status": "stopped",
                    "error": f"stopped by its overseer: {verdict['note']}",
                }
        seen = (
            progress,
            json.dumps(vitals, sort_keys=True) if vitals else "",
            json.dumps(card, sort_keys=True),
        )
        if (progress or vitals) and seen != shown and context.progress:
            extra = {"vitals": vitals} if vitals is not None else {}
            if card.get("oversight") or card["session"].get("resumed"):
                extra["card"] = card
            context.progress(progress, **extra)
            shown = seen
