"""`/api/activities` & our Activities page: one list (the files
`/activity` can start), each with a title, a description & a kind that are
the activity's own or derived from its YAML, and a name only for a file we
cannot read."""

import pytest
import yaml

pytestmark = pytest.mark.integration


def activity(blocks, **top):
    return yaml.safe_dump(
        {
            **top,
            "sections": [
                {
                    "section_id": "s",
                    "title": "Section",
                    "steps": [
                        {"step_id": "a", "title": "A", "content_blocks": blocks},
                        {"step_id": "z", "title": "Z", "content_blocks": ["Bye"]},
                    ],
                }
            ],
        }
    )


@pytest.fixture
def research(test_app, tmp_path, monkeypatch):
    """A research/ of our own making, listed the way --local-activities
    lists our real one (paths are relative to where our server runs)."""
    folder = tmp_path / "research"
    folder.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setitem(test_app.config, "LOCAL_ACTIVITIES", True)
    return folder


@pytest.fixture
def http(test_app):
    return test_app.test_client()


def test_each_activity_carries_derived_title_description_and_kind(research, http):
    (research / "activity7-odd-socks.yaml").write_text(
        activity(["# 🧦 Welcome to Odd Socks!", "", "Pair our socks. Then fold."])
    )
    (research / "notes.txt").write_text("not an activity")
    data = http.get("/api/activities").json
    # Chat's chooser still gets every path `/activity` takes.
    assert data["activities"] == ["research/activity7-odd-socks.yaml"]
    assert data["details"] == [
        {
            "path": "research/activity7-odd-socks.yaml",
            "name": "activity7-odd-socks",
            "title": "Odd Socks",
            "description": "Pair our socks. Then fold.",
            "kind": "lesson",
            "readable": True,
            "listed": True,
        }
    ]


def test_an_activitys_own_keys_override_what_we_derive(research, http):
    (research / "activity8-dice.yaml").write_text(
        activity(
            ["# Dice", "Derived words."],
            title="Liar's Dice",
            description="Bluff about what we rolled.",
            kind="game",
        )
    )
    detail = http.get("/api/activities").json["details"][0]
    assert detail["title"] == "Liar's Dice"
    assert detail["description"] == "Bluff about what we rolled."
    assert detail["kind"] == "game"


def test_a_file_we_cannot_read_keeps_its_name_only(research, http):
    (research / "activity1-good.yaml").write_text(activity(["Fine words."]))
    (research / "activity2-torn-page.yaml").write_text("sections: [unclosed\n : :")
    (research / "activity3-bytes.yaml").write_bytes(b"\xff\xfe\x00 not text")
    data = http.get("/api/activities").json
    assert len(data["activities"]) == 3  # still listed, still in order
    good, torn, raw = data["details"]
    assert good["readable"] and good["kind"] == "lesson"
    for broken, title in ((torn, "Torn Page"), (raw, "Bytes")):
        assert broken["readable"] is False
        assert broken["title"] == title
        assert broken["description"] == "" and broken["kind"] is None

    page = http.get("/activities").get_data(as_text=True)
    assert "Torn Page" in page and "could not read this activity" in page
    # Only an activity we could read offers Play.
    assert page.count("data-play") == 1


def test_no_script_runs_to_build_our_gallery(research, http, tmp_path):
    marker = tmp_path / "ran"
    content = yaml.safe_load(activity(["Roll."]))
    content["sections"][0]["steps"][0][
        "pre_script"
    ] = f"open({str(marker)!r}, 'w').write('x')"
    (research / "activity9-trap.yaml").write_text(yaml.safe_dump(content))
    assert http.get("/api/activities").json["details"][0]["kind"] == "game"
    assert http.get("/activities").status_code == 200
    assert not marker.exists()


def test_without_local_activities_nothing_is_listed_and_our_page_says_so(
    test_app, http, monkeypatch
):
    monkeypatch.setitem(test_app.config, "LOCAL_ACTIVITIES", False)
    assert http.get("/api/activities").json == {"activities": [], "details": []}
    page = http.get("/activities").get_data(as_text=True)
    assert "No activities are listed on this server" in page
    assert "activities.js" not in page


def test_our_page_shows_activities_only_and_opens_on_a_kind(research, http):
    (research / "SPEC.yaml").write_text(activity(["Our format's reference."]))
    (research / "activity1-lesson.yaml").write_text(activity(["Learn."]))
    (research / "activity2-game.yaml").write_text(activity(["Play."], kind="game"))
    assert len(http.get("/api/activities").json["details"]) == 3

    page = http.get("/activities?kind=game").get_data(as_text=True)
    assert 'data-path="research/SPEC.yaml"' not in page
    assert 'data-path="research/activity1-lesson.yaml"' in page
    assert 'data-kind="game" aria-checked="true"' in page
    # No tool among them: no Tools filter to press for nothing.
    assert 'data-kind="tool"' not in page
    # A kind nobody has opens on all.
    page = http.get("/activities?kind=tool").get_data(as_text=True)
    assert 'data-kind="all" aria-checked="true"' in page


def test_a_title_cannot_break_out_of_its_card(research, http):
    (research / "activity1-x.yaml").write_text(
        activity(["Hi."], title='"><script>alert(1)</script>')
    )
    page = http.get("/activities").get_data(as_text=True)
    assert "<script>alert(1)" not in page


def test_our_gallery_leaves_out_fixtures_and_gives_every_card_its_own_title(test_app):
    from collections import Counter

    test_app.config["LOCAL_ACTIVITIES"] = True
    try:
        html = test_app.test_client().get("/activities").get_data(as_text=True)
        details = test_app.test_client().get("/api/activities").json["details"]
    finally:
        test_app.config["LOCAL_ACTIVITIES"] = False
    # Our two test fixtures say `listed: false`: on our API, off our page.
    assert "activity29-testship" not in html and "activity-test-v2-features" not in html
    assert any(d["name"] == "activity29-testship" and not d["listed"] for d in details)
    listed = [d for d in details if d["listed"] and d["name"].startswith("activity")]
    twice = [t for t, n in Counter(d["title"] for d in listed).items() if n > 1]
    assert twice == [], twice
    assert "Escape Room: Key, Book &amp; Safe" in html
