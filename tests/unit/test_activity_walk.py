"""activity_walk.py plays an activity with dice & reports what breaks."""

import activity_walk

GOOD = {
    "sections": [
        {
            "section_id": "s",
            "title": "S",
            "steps": [
                {
                    "step_id": "ask",
                    "title": "Ask",
                    "question": "Pick?",
                    "buckets": ["a", "b"],
                    "transitions": {
                        "a": {"metadata_add": {"score": "n+1"}},
                        "b": {"content_blocks": ["Score {{metadata.score}}"]},
                    },
                },
                {"step_id": "end", "title": "End", "content_blocks": ["Bye"]},
            ],
        }
    ]
}


def run(tmp_path, content, walks=6):
    import yaml

    path = tmp_path / "activity.yaml"
    path.write_text(yaml.safe_dump(content))
    return activity_walk.walk_file(str(path), walks, 40, "test")


def test_a_sound_activity_ends_and_raises_nothing(tmp_path):
    found = run(tmp_path, GOOD)
    assert not found["raised"]
    assert found["ended"] == 6
    assert not found["unreached"]
    assert not found["raw_names"]


def test_a_script_that_raises_is_reported_with_its_step(tmp_path):
    bad = yaml_copy(GOOD)
    bad["sections"][0]["steps"][0]["processing_script"] = "x = 1 / 0"
    bad["sections"][0]["steps"][0]["transitions"]["a"]["run_processing_script"] = True
    found = run(tmp_path, bad, walks=8)
    assert any(
        where == "s:ask" and "ZeroDivisionError" in error
        for where, _, _, error in found["raised"]
    )


def test_a_raw_metadata_name_shown_is_reported(tmp_path):
    bad = yaml_copy(GOOD)
    bad["sections"][0]["steps"][1]["content_blocks"] = ["Your score: metadata.score"]
    found = run(tmp_path, bad)
    assert ("s:ask", "metadata.score") in found["raw_names"] or any(
        name == "metadata.score" for _, name in found["raw_names"]
    )


def yaml_copy(content):
    import copy

    return copy.deepcopy(content)
