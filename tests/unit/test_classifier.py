"""classifier.py: a second kind of model for categorize_response.

Discovery from MODEL_CLASSIFIER_*_N, the OPENCOMPLETION_CLASSIFIER knob
(auto / on / off), the wire shape (one POST /v1/systemone with a bearer that
never travels into a result or an error), the typed questions, cooldown
failover across instances, one retry on an overloaded vendor, & the
fail-open bridge inside activity.categorize_response. No network: urlopen is
replaced everywhere.
"""

import io
import json
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import classifier  # noqa: E402

BUCKETS = ["correct", "close", "incorrect"]
ENV = {
    "MODEL_CLASSIFIER_ENDPOINT_0": "https://api.typesafe.ai/",
    "MODEL_CLASSIFIER_API_KEY_0": "apikey_test_never_printed",
    "OPENCOMPLETION_CLASSIFIER": "auto",
}


def _choice(choice="correct", probs=None, confidence=0.9, model="jev-1.13.0"):
    probs = probs or {"correct": 0.8, "close": 0.15, "incorrect": 0.05}
    return {
        "model": model,
        "answers": {
            "answer": {
                "type": "choice",
                "choice": choice,
                "probabilities": probs,
                "confidence": confidence,
            }
        },
        "usage": {"input_tokens": 120, "output_tokens": 12},
    }


class _Resp(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _urlopen(bodies):
    """A urlopen stand-in answering from `bodies` (dicts, or an exception to
    raise), remembering every Request."""
    seen = []
    queue = list(bodies)

    def fake(req, timeout=None):
        seen.append(req)
        b = queue.pop(0)
        if isinstance(b, Exception):
            raise b
        return _Resp(json.dumps(b).encode())

    fake.seen = seen
    return fake


class _Base(unittest.TestCase):
    def setUp(self):
        classifier._reset_cooling()
        classifier._warned.clear()
        classifier.reset_stats()
        self._env = mock.patch.dict("os.environ", ENV, clear=False)
        self._env.start()
        self.addCleanup(self._env.stop)


# ── Discovery & the knob ───────────────────────────────────────────


class TestDiscovery(unittest.TestCase):
    def test_instances_from_env(self):
        env = {
            "MODEL_CLASSIFIER_ENDPOINT_0": "https://api.typesafe.ai/",
            "MODEL_CLASSIFIER_API_KEY_0": "k",
            "MODEL_CLASSIFIER_ENDPOINT_3": "https://proxy.example.com/typesafe",
            "MODEL_CLASSIFIER_ID_3": "jev-preview",
            "MODEL_CLASSIFIER_LABEL_3": "proxy",
        }
        inst = classifier.instances(env)
        self.assertEqual(list(inst), ["typesafe", "proxy"])
        self.assertEqual(inst["typesafe"]["base"], "https://api.typesafe.ai")
        self.assertEqual(inst["typesafe"]["key_env"], "MODEL_CLASSIFIER_API_KEY_0")
        self.assertEqual(inst["typesafe"]["model"], classifier.DEFAULT_MODEL)
        self.assertIsNone(inst["proxy"]["key_env"])  # optional: a proxy adds its own
        self.assertEqual(inst["proxy"]["model"], "jev-preview")
        # Key VALUES never appear in the catalog.
        self.assertNotIn("k", json.dumps(inst).replace('"key_env"', ""))

    def test_nothing_configured(self):
        self.assertEqual(classifier.instances({}), {})
        self.assertFalse(classifier.available({"OPENCOMPLETION_CLASSIFIER": "auto"}))

    def test_mode_words(self):
        for word, want in (
            ("", "auto"),
            ("auto", "auto"),
            ("weird", "auto"),
            ("0", "off"),
            ("off", "off"),
            ("False", "off"),
            ("1", "on"),
            ("on", "on"),
            ("yes", "on"),
        ):
            self.assertEqual(
                classifier.mode({"OPENCOMPLETION_CLASSIFIER": word}), want, word
            )

    def test_available_follows_knob(self):
        env = dict(ENV)
        self.assertTrue(classifier.available(env))
        env["OPENCOMPLETION_CLASSIFIER"] = "off"
        self.assertFalse(classifier.available(env))

    def test_on_without_endpoint_is_unavailable_and_warns_once(self):
        classifier._warned.clear()
        env = {"OPENCOMPLETION_CLASSIFIER": "on"}
        with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertFalse(classifier.available(env))
            self.assertFalse(classifier.available(env))
        self.assertEqual(err.getvalue().count("OPENCOMPLETION_CLASSIFIER=on"), 1)

    def test_describe(self):
        self.assertIn("typesafe jev-latest", classifier.describe(ENV))
        self.assertEqual(classifier.describe({}), "")
        self.assertIn(
            "none configured", classifier.describe({"OPENCOMPLETION_CLASSIFIER": "on"})
        )

    def test_knob_floats(self):
        env = {
            "OPENCOMPLETION_CLASSIFIER_TIMEOUT_S": "3",
            "OPENCOMPLETION_CLASSIFIER_COOLDOWN_S": "nope",
            "OPENCOMPLETION_CLASSIFIER_MIN_P": "0.6",
        }
        self.assertEqual(classifier.default_timeout(env), 3.0)
        self.assertEqual(classifier.default_cooldown(env), classifier.COOLDOWN_S)
        self.assertEqual(classifier.min_p(env), 0.6)
        self.assertEqual(classifier.min_p({}), 0.0)


# ── Wire ───────────────────────────────────────────────────────────


class TestWire(_Base):
    def test_choose_posts_systemone_with_bearer_and_returns_decide_shape(self):
        fake = _urlopen([_choice()])
        with mock.patch("urllib.request.urlopen", fake):
            d = classifier.choose("5 + 7 is 12", BUCKETS, instructions="grade it")
        req = fake.seen[0]
        self.assertEqual(req.full_url, "https://api.typesafe.ai/v1/systemone")
        self.assertEqual(
            req.get_header("Authorization"), "Bearer apikey_test_never_printed"
        )
        body = json.loads(req.data)
        self.assertEqual(body["state"], "5 + 7 is 12")
        self.assertEqual(body["model"], "jev-latest")
        q = body["questions"]["answer"]
        self.assertEqual(q["type"], "choice")
        self.assertEqual(q["instructions"], "grade it")
        self.assertEqual(list(q["criteria"]), BUCKETS)
        self.assertEqual(d["value"], "correct")
        self.assertEqual(d["p"], 0.8)
        self.assertEqual(d["mass"], 1.0)
        self.assertEqual(d["source"], "classifier")
        self.assertEqual(d["confidence"], 0.9)
        self.assertEqual(d["model"], "jev-1.13.0")
        self.assertEqual(d["instance"], "typesafe")
        s = classifier.stats()
        self.assertEqual((s["calls"], s["ok"], s["input_tokens"]), (1, 1, 120))

    def test_choice_outside_labels_falls_to_argmax(self):
        fake = _urlopen(
            [_choice(choice="CORRECT", probs={"correct": 0.2, "incorrect": 0.7})]
        )
        with mock.patch("urllib.request.urlopen", fake):
            d = classifier.choose("x", BUCKETS)
        self.assertEqual(d["value"], "incorrect")
        self.assertAlmostEqual(sum(d["dist"].values()), 1.0, places=3)

    def test_extra_questions_ride_in_the_same_call(self):
        body = _choice()
        body["answers"]["flag"] = {"type": "noul", "noul": 0.9}
        fake = _urlopen([body])
        with mock.patch("urllib.request.urlopen", fake):
            d = classifier.choose(
                "x", BUCKETS, extra={"flag": {"type": "noul", "instructions": "?"}}
            )
        self.assertEqual(len(fake.seen), 1)
        self.assertEqual(d["extra"]["flag"]["noul"], 0.9)
        self.assertIn("flag", json.loads(fake.seen[0].data)["questions"])

    def test_http_error_never_carries_the_key(self):
        err = urllib.error.HTTPError("u", 401, "nope", {}, io.BytesIO(b"denied"))
        fake = _urlopen([err])
        with mock.patch("urllib.request.urlopen", fake):
            with self.assertRaises(classifier.ClassifierError) as cm:
                classifier.choose("x", BUCKETS)
        self.assertIn("HTTP 401", str(cm.exception))
        self.assertNotIn("apikey_test", str(cm.exception))
        self.assertEqual(classifier.stats()["errors"], 1)

    def test_unanswered_question_is_an_error(self):
        fake = _urlopen([{"model": "m", "answers": {}}])
        with mock.patch("urllib.request.urlopen", fake):
            with self.assertRaises(classifier.ClassifierError) as cm:
                classifier.choose("x", BUCKETS)
        self.assertIn("unanswered", str(cm.exception))

    def test_yes_no_and_score(self):
        bodies = [
            {"model": "m", "answers": {"answer": {"type": "noul", "noul": 0.83}}},
            {
                "model": "m",
                "answers": {
                    "answer": {
                        "type": "score",
                        "score": 3.2,
                        "confidence": 0.7,
                        "probabilities": {"3": 0.8, "4": 0.2},
                    }
                },
            },
        ]
        fake = _urlopen(bodies)
        with mock.patch("urllib.request.urlopen", fake):
            p = classifier.yes_no(
                "is it a question?",
                "Judge the form.",
                true="a question",
                false="an attempt",
            )
            s = classifier.score(
                "answer",
                ["none", "some", "most", "all", "mastery"],
                instructions="rubric",
            )
        self.assertEqual(p, 0.83)
        q = json.loads(fake.seen[0].data)["questions"]["answer"]
        self.assertEqual(q["criteria"], {"true": "a question", "false": "an attempt"})
        self.assertEqual(s["score"], 3.2)
        self.assertEqual(s["dist"], {3: 0.8, 4: 0.2})
        q = json.loads(fake.seen[1].data)["questions"]["answer"]
        self.assertEqual(q["type"], "score")
        self.assertEqual(len(q["criteria"]), 5)

    def test_failed_instance_cools_and_next_instance_answers(self):
        env = {
            "MODEL_CLASSIFIER_ENDPOINT_1": "https://proxy.example.com",
            "MODEL_CLASSIFIER_LABEL_1": "proxy",
        }
        fake = _urlopen([TimeoutError("slow"), _choice(choice="incorrect")])
        with mock.patch.dict("os.environ", env), mock.patch(
            "urllib.request.urlopen", fake
        ):
            d = classifier.choose("x", BUCKETS)
            self.assertEqual(d["instance"], "proxy")
            self.assertTrue(classifier._is_cooling("typesafe"))
            # The cooling instance is skipped; the proxy takes the next call too.
            fake2 = _urlopen([_choice()])
            with mock.patch("urllib.request.urlopen", fake2):
                classifier.choose("y", BUCKETS)
            self.assertIn("proxy.example.com", fake2.seen[0].full_url)

    def test_overloaded_vendor_gets_one_retry_before_cooling(self):
        over = urllib.error.HTTPError("u", 529, "busy", {}, io.BytesIO(b"overloaded"))
        fake = _urlopen([over, _choice()])
        with mock.patch("urllib.request.urlopen", fake), mock.patch(
            "classifier.time.sleep"
        ) as slept:
            d = classifier.choose("x", BUCKETS)
        self.assertEqual(d["value"], "correct")
        self.assertEqual(len(fake.seen), 2)
        slept.assert_called_once_with(classifier.RETRY_DELAY_S)
        self.assertFalse(classifier._is_cooling("typesafe"))
        fake = _urlopen([over, over])
        with mock.patch("urllib.request.urlopen", fake), mock.patch(
            "classifier.time.sleep"
        ):
            with self.assertRaises(classifier.ClassifierError):
                classifier.choose("x", BUCKETS)
        self.assertTrue(classifier._is_cooling("typesafe"))

    def test_list_models(self):
        fake = _urlopen([{"models": [{"name": "jev-latest"}, {"name": "jev-preview"}]}])
        with mock.patch("urllib.request.urlopen", fake):
            got = classifier.list_models()
        self.assertEqual(got, {"typesafe": ["jev-latest", "jev-preview"]})
        self.assertTrue(fake.seen[0].full_url.endswith("/v1/models"))
        self.assertIsNone(fake.seen[0].data)


# ── The categorize bridge ──────────────────────────────────────────


class TestCategorize(_Base):
    def test_state_instructions_and_bucket_names(self):
        fake = _urlopen([_choice()])
        buckets = ["correct", {"bucket_name": "close"}, "incorrect"]
        with mock.patch("urllib.request.urlopen", fake):
            d = classifier.categorize("What is 5 + 7?", "12", buckets, "Grade it.")
        body = json.loads(fake.seen[0].data)
        self.assertEqual(body["state"], "Question: What is 5 + 7?\nResponse: 12")
        q = body["questions"]["answer"]
        self.assertEqual(q["instructions"], "Grade it.")
        self.assertEqual(list(q["criteria"]), BUCKETS)
        self.assertEqual(d["value"], "correct")

    def test_low_p_hands_the_decision_to_chat(self):
        fake = _urlopen(
            [_choice(probs={"correct": 0.4, "close": 0.35, "incorrect": 0.25})]
        )
        with mock.patch("urllib.request.urlopen", fake), mock.patch.dict(
            "os.environ", {"OPENCOMPLETION_CLASSIFIER_MIN_P": "0.5"}
        ):
            with self.assertRaises(classifier.ClassifierError) as cm:
                classifier.categorize("q", "r", BUCKETS, "t")
        self.assertIn("low p", str(cm.exception))

    def test_no_buckets_is_an_error(self):
        with self.assertRaises(classifier.ClassifierError):
            classifier.categorize("q", "r", [], "t")


class TestActivityBridge(_Base):
    """activity.categorize_response asks the classifier first & fails open."""

    def setUp(self):
        super().setUp()
        import activity

        self.activity = activity
        self.completion = mock.MagicMock()
        self.completion.choices = [mock.MagicMock()]
        self.completion.choices[0].message.content = "close"
        self.completion.choices[0].logprobs = None
        self.create = mock.patch.object(
            activity, "create_completion_skip_thinking", return_value=self.completion
        ).start()
        mock.patch.object(
            activity,
            "get_openai_client_and_model",
            return_value=(mock.MagicMock(), "m"),
        ).start()
        self.addCleanup(mock.patch.stopall)

    def test_classifier_decides_and_chat_is_never_called(self):
        fake = _urlopen([_choice()])
        with mock.patch("urllib.request.urlopen", fake):
            got = self.activity.categorize_response("q", "12", BUCKETS, "Grade it.")
        self.assertEqual(got, "correct")
        self.create.assert_not_called()
        ro = self.activity.last_readout()
        self.assertEqual(ro["source"], "classifier")
        self.assertEqual(ro["p"], 0.8)
        self.assertEqual(ro["confidence"], 0.9)
        self.assertEqual(ro["instance"], "typesafe")

    def test_vendor_error_fails_open_with_a_breadcrumb(self):
        fake = _urlopen([TimeoutError("slow")])
        with mock.patch("urllib.request.urlopen", fake):
            got = self.activity.categorize_response("q", "13", BUCKETS, "Grade it.")
        self.assertEqual(got, "close")
        self.create.assert_called_once()
        ro = self.activity.last_readout()
        self.assertEqual(ro["source"], "chat")
        self.assertIn("TimeoutError", ro["classifier_error"])

    def test_knob_off_means_no_network(self):
        fake = _urlopen([_choice()])
        with mock.patch.dict(
            "os.environ", {"OPENCOMPLETION_CLASSIFIER": "off"}
        ), mock.patch("urllib.request.urlopen", fake):
            got = self.activity.categorize_response("q", "13", BUCKETS, "Grade it.")
        self.assertEqual(got, "close")
        self.assertEqual(fake.seen, [])
        self.assertIsNone(self.activity.last_readout())


if __name__ == "__main__":
    unittest.main()
