"""activity_gallery: what our Activities gallery says about an activity is
its own title, description & kind, else derived from its YAML: no model, no
script run."""

import os
from pathlib import Path

import pytest
import yaml

import activity_gallery as gallery

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.unit


def activity(first_step, **top):
    """An activity whose first step is `first_step`, then a goodbye."""
    return {
        **top,
        "sections": [
            {
                "section_id": "s",
                "title": "Section Title",
                "steps": [
                    {"step_id": "a", "title": "A", **first_step},
                    {"step_id": "z", "title": "Z", "content_blocks": ["Bye"]},
                ],
            }
        ],
    }


@pytest.mark.parametrize(
    "path, title",
    [
        ("research/activity29-battleship.yaml", "Battleship"),
        ("research/activity26-magic-8-ball.yaml", "Magic 8 Ball"),
        ("research/activity-submarine-simulation.yaml", "Submarine Simulation"),
        ("research/activity7.yaml", ""),
        ("research/activity.yaml", ""),
    ],
)
def test_a_files_name_as_a_title(path, title):
    assert gallery.slug_title(path) == title


def test_a_heading_that_shares_a_word_with_the_name_improves_it():
    content = activity(
        {"content_blocks": ["# 🧪 Welcome to Scientific Method Explorer!", "Hi."]}
    )
    made = gallery.card("research/activity31-scientific-method.yaml", content)
    assert made["title"] == "Scientific Method Explorer"


def test_a_heading_naming_a_scene_leaves_the_files_name():
    content = activity({"content_blocks": ["# ✨ THE DIRECTOR AWAKENS ✨", "Hi."]})
    made = gallery.card("research/activity40-fashion-empire.yaml", content)
    assert made["title"] == "Fashion Empire"


def test_a_numbered_file_is_named_by_its_welcome_then_its_section():
    welcomed = activity(
        {"content_blocks": ["Welcome to the **GNU Manifesto** course! 👋", "More."]}
    )
    assert (
        gallery.card("research/activity0.yaml", welcomed)["title"]
        == "GNU Manifesto course"
    )
    plain = activity({"content_blocks": ["You wake up in a dim room."]})
    assert gallery.card("research/activity0.yaml", plain)["title"] == "Section Title"


def test_description_is_opening_prose_cut_at_a_sentence():
    content = activity(
        {
            "content_blocks": [
                "# A heading",
                "",
                "```",
                "ASCII art that is not prose.",
                "```",
                "Welcome to **the game**! 🎮",
                "- a list item",
                "Hello {{metadata.name}}, templated.",
                "You'll learn to:",
                "Guess our code. " + "Every guess costs a try. " * 12,
            ]
        }
    )
    description = gallery.card("research/activity1-x.yaml", content)["description"]
    assert description.startswith("Guess our code. Every guess costs a try.")
    assert description.endswith("try.")
    assert len(description) <= gallery.DESCRIPTION_CHARS
    for junk in ("heading", "ASCII", "Welcome", "list item", "templated", "learn to"):
        assert junk not in description


def test_one_long_sentence_is_cut_at_a_word_and_says_so():
    words = "word " * 80
    cut = gallery.cut(words.strip() + ".")
    assert cut.endswith("word…") and len(cut) <= gallery.DESCRIPTION_CHARS + 1


def test_a_first_step_with_only_a_question_gives_it():
    content = activity({"question": "Who is **Mario**?", "buckets": []})
    assert gallery.card("research/activity4.yaml", content)["description"] == (
        "Who is Mario?"
    )


def test_kind_is_game_for_scripts_or_chance_else_lesson():
    lesson = activity({"content_blocks": ["Read this."]})
    assert gallery.derive_kind(lesson) == "lesson"
    scripted = activity({"content_blocks": ["Hi"], "processing_script": "x = 1"})
    assert gallery.derive_kind(scripted) == "game"
    pre = activity({"content_blocks": ["Hi"], "pre_script": "x = 1"})
    assert gallery.derive_kind(pre) == "game"
    chance = activity(
        {
            "question": "Roll?",
            "buckets": ["roll"],
            "transitions": {"roll": {"metadata_tmp_random": {"die": [1, 2]}}},
        }
    )
    assert gallery.derive_kind(chance) == "game"


def test_an_activitys_own_words_win_and_a_wrong_kind_is_ignored():
    content = activity(
        {"content_blocks": ["# Other", "Derived words."]},
        title="Our **Own** Title",
        description="Our own line.",
        kind="Tool",
    )
    made = gallery.card("research/activity9-x.yaml", content)
    assert (made["title"], made["description"], made["kind"]) == (
        "Our Own Title",
        "Our own line.",
        "tool",
    )
    content["kind"] = "puzzle"
    assert gallery.card("research/activity9-x.yaml", content)["kind"] == "lesson"


def test_a_script_is_never_run(tmp_path):
    marker = tmp_path / "ran"
    path = tmp_path / "activity1-trap.yaml"
    script = f"open({str(marker)!r}, 'w').write('x')"
    path.write_text(
        yaml.safe_dump(
            activity(
                {"content_blocks": ["Hi."], "pre_script": script},
                # Not a string: ignored, never evaluated.
                title={"not": "a string"},
            )
        )
    )
    made = gallery.local_card(str(path))
    assert made["kind"] == "game" and made["title"] == "Trap"
    assert not marker.exists()


def test_a_file_we_cannot_read_gets_its_name_only(tmp_path):
    broken = tmp_path / "activity3-broken-file.yaml"
    broken.write_text("sections: [unclosed\n  - : :")
    made = gallery.local_card(str(broken))
    assert made == {
        "path": str(broken),
        "name": "activity3-broken-file",
        "title": "Broken File",
        "description": "",
        "kind": None,
        "readable": False,
    }
    not_an_activity = tmp_path / "activity4-list.yaml"
    not_an_activity.write_text("- just\n- a list\n")
    assert gallery.local_card(str(not_an_activity))["readable"] is False
    assert gallery.local_card(str(tmp_path / "gone.yaml"))["readable"] is False


def test_a_card_is_cached_until_its_file_changes(tmp_path, monkeypatch):
    path = tmp_path / "activity5-cache.yaml"
    path.write_text(yaml.safe_dump(activity({"content_blocks": ["One."]})))
    assert gallery.local_card(str(path))["description"] == "One."

    loads = []
    real_load = yaml.load
    monkeypatch.setattr(
        gallery.yaml, "load", lambda *a, **k: loads.append(1) or real_load(*a, **k)
    )
    assert gallery.local_card(str(path))["description"] == "One."
    assert loads == []  # unchanged: not parsed again

    path.write_text(yaml.safe_dump(activity({"content_blocks": ["Two, longer."]})))
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    assert gallery.local_card(str(path))["description"] == "Two, longer."
    assert loads == [1]


def test_every_activity_we_ship_gets_a_card_worth_showing():
    paths = gallery.local_paths(str(ROOT / "research"))
    assert len(paths) > 50
    for path in paths:
        made = gallery.local_card(path)
        assert made["readable"], path
        assert made["title"] and made["kind"] in gallery.KINDS, path
        assert made["description"], path
        assert len(made["description"]) <= gallery.DESCRIPTION_CHARS + 1, path
        assert "{{" not in made["title"] + made["description"], path


def test_showpieces_say_their_own_words():
    made = gallery.local_card(str(ROOT / "research" / "activity52-vault-heist.yaml"))
    assert made["title"] == "Vault Heist" and made["kind"] == "game"
    assert "shared tries" in made["description"]
    plotter = gallery.local_card(str(ROOT / "research" / "activity24-math-plot.yaml"))
    assert plotter["kind"] == "tool"  # never derived: its YAML says so
