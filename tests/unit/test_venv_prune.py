"""venv_prune: what our lock files no longer name is removed from a venv."""

import venv_prune


def test_reads_pinned_names_from_a_lock(tmp_path):
    lock = tmp_path / "a.lock"
    lock.write_text(
        "# comment\n"
        "Flask_SocketIO==5.5 \\\n"
        "    --hash=sha256:abc\n"
        "openai[datalib]==1.0 ; python_version >= '3.11'\n"
        "    # via -r requirements.txt\n"
    )
    assert venv_prune.locked([lock]) == {"flask-socketio", "openai"}


def test_strays_are_what_no_lock_names_save_pips_own():
    locked = {"flask", "openai"}
    installed = {"flask", "openai", "together", "pip", "setuptools", "pandas"}
    assert venv_prune.strays(locked, installed) == ["pandas", "together"]


def test_nothing_is_removed_when_no_lock_was_read(tmp_path, monkeypatch, capsys):
    empty = tmp_path / "empty.lock"
    empty.write_text("# nothing pinned\n")
    calls = []
    monkeypatch.setattr(venv_prune.subprocess, "call", lambda *a: calls.append(a))
    assert venv_prune.main([empty]) == 0
    assert calls == [] and "nothing removed" in capsys.readouterr().out
