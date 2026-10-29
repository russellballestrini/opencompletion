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
UNSANDBOX_PUBLIC_KEY & UNSANDBOX_SECRET_KEY). We never delete a sandbox:
idle ones freeze, & a frozen one costs no RAM.

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
import secrets
import shlex
import time

import gevent

import un
from blob_store import max_bytes, save_attachment
from models import Sandbox, db
from tools import policy

OUTPUT_CHARS = 8000
LANGUAGES_FREEBSD = ("bash", "sh", "python", "perl", "lua", "c", "cpp")
READY_TIMEOUT_S = 180


def _keys():
    return un._resolve_credentials(None, None)


def _clip(text, limit=OUTPUT_CHARS):
    text = text or ""
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n[… {len(text) - limit} characters cut …]\n{text[-half:]}"


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
    result = un.wait_for_job(job["job_id"], timeout=policy.run_code_timeout() + 30)
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


def _wait_running(service_id):
    deadline = time.time() + READY_TIMEOUT_S
    while time.time() < deadline:
        state = _state(un.get_service(service_id))
        if state == "running":
            return
        if state in ("error", "failed", "destroyed"):
            raise RuntimeError(f"sandbox {state}")
        gevent.sleep(2)
    raise RuntimeError("sandbox did not start in time")


def _create(context):
    name = f"oc-{context.room_id}-{secrets.token_hex(3)}"
    service = un.create_service(
        name=name, network_mode="semitrusted", golden_image=policy.sandbox_image()
    )
    service_id = service.get("service_id") or service.get("id")
    if not service_id:
        raise RuntimeError("unsandbox made no sandbox")
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


def ready(context):
    """This chat's sandbox, made or unfrozen as needed, running."""
    if context.room_id is None:
        raise RuntimeError("no chat to keep a sandbox for")
    sandbox = Sandbox.query.filter_by(room_id=context.room_id).first()
    if sandbox is None:
        sandbox = _create(context)
    elif sandbox.state == "frozen":
        un.unfreeze_service(sandbox.service_id)
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
        sandbox.state = "frozen"
        db.session.commit()


_app = None


def init_app(app):
    """The Flask app our idle timers open a context on."""
    global _app
    _app = app


def _execute(sandbox, command, timeout_s):
    """Run `command` in a sandbox. It counts as in use until the command
    could end, so a long delegate is never frozen under itself; its idle
    clock starts when the command does end."""
    sandbox.last_used_at = time.time() + timeout_s
    db.session.commit()
    try:
        job = un.execute_in_service(
            sandbox.service_id, command, timeout=timeout_s * 1000
        )
        if job.get("job_id"):
            return un.wait_for_job(job["job_id"], timeout=timeout_s + 30)
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
    return _finished(_execute(sandbox, command, policy.sandbox_timeout()))


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
    result = _finished(_execute(ready(context), command, policy.sandbox_timeout()))
    if result.get("exit_code") == 0:
        return {"path": path, "bytes": int((result["stdout"] or "0").strip() or 0)}
    return result


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
        },
    )
    sandbox.token_sha256 = token_hash(token)
    sandbox.model = model
    db.session.commit()


def delegate(context, task):
    task = str(task or "").strip()
    if not task:
        return {"error": "task required"}
    sandbox = ready(context)
    model = context.model or sandbox.model
    if not sandbox.token_sha256 or sandbox.model != model:
        arm_proxy(sandbox, model)
    command = (
        ". /run/unsandbox/vault.sh 2>/dev/null; "
        "PATH=/root/.local/bin:$PATH unclose -p --no-markdown "
        f"--model {shlex.quote(model)} {shlex.quote(task)}"
    )
    result = _finished(_execute(sandbox, command, policy.delegate_timeout()))
    result["answer"] = result.pop("stdout")
    return result
