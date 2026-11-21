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


# A door only its own code opens: dice never guess it, so before
# offered_answers no walk left "door" & "inside" looked unreachable.
LOCKED = {
    "default_max_attempts_per_step": 1000,
    "sections": [
        {
            "section_id": "s",
            "title": "S",
            "steps": [
                {
                    "step_id": "start",
                    "title": "Start",
                    "question": "Ready?",
                    "buckets": ["go"],
                    "transitions": {
                        "go": {
                            "metadata_add": {"secret": "4917"},
                            "next_section_and_step": "s:door",
                        }
                    },
                },
                {
                    "step_id": "door",
                    "title": "Door",
                    "question": "What is our code?",
                    "buckets": ["guess"],
                    "processing_script": (
                        "said = str(metadata.get('user_response', '')).strip()\n"
                        "script_result = {}\n"
                        "if said == metadata['secret']:\n"
                        "    script_result['next_section_and_step'] = 's:inside'\n"
                    ),
                    "transitions": {
                        "guess": {
                            "run_processing_script": True,
                            "next_section_and_step": "s:door",
                        }
                    },
                },
                {"step_id": "inside", "title": "Inside", "content_blocks": ["In"]},
            ],
        }
    ],
}


def test_a_step_wanting_a_value_from_metadata_is_passed(tmp_path, monkeypatch):
    found = run(tmp_path, LOCKED)
    assert not found["raised"]
    assert not found["unreached"]
    assert found["ended"] == 6
    # Our dice alone never open it: the blind spot this closes.
    monkeypatch.setattr(activity_walk, "offered_answers", lambda *a: [])
    assert ("s", "inside") in run(tmp_path, LOCKED)["unreached"]


def test_offered_answers_are_what_a_steps_own_data_holds():
    step = {
        "processing_script": (
            "if metadata.get('user_response') == 'open sesame': pass\n"
            "if word in ('red', \"blue\"): pass\n"
            "x = metadata['secret'] + [metadata.get('pin')]"
        ),
        "transitions": {
            "a": {"metadata_conditions": {"colour": "green"}},
            "b": {"content_blocks": ["Was it {{metadata.shown}}?"]},
        },
    }
    metadata = {
        "secret": [4, 9, 1, 7],
        "pin": 42,
        "shown": "teal",
        "user_response": "never me",
        "flag": True,
        "unread": "never me either",
    }
    offered = activity_walk.offered_answers(step, metadata)
    assert sorted(offered) == sorted(
        ["4 9 1 7", "42", "teal", "open sesame", "red", "blue", "green"]
    )
    assert activity_walk.offered_answers({"question": "Pick?"}, metadata) == []
    assert sorted(activity_walk.offered_answers(step, None)) == sorted(
        ["open sesame", "red", "blue", "green"]
    )
