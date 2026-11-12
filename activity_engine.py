"""Our activity engine: a learner's answer to the current step, & the walk
through content steps to the next question.

One engine for our server (activity.py) & a terminal (research/
guarded_ai.py), so a defect fixed here is fixed in both (fox, 2026-11-02:
two copies drifted). No Flask, database or socket here; two things are
passed in:

- state: where the activity is (section_id, step_id,
  attempts, max_attempts) & its metadata (dict_metadata,
  add_metadata, remove_metadata, clear_metadata):
  models.ActivityState on our server, MemoryState below in a terminal.
- io: everything said, stored or asked (ActivityIO below): activity.py's
  RoomIO posts to a room & its database, guarded_ai's TerminalIO prints.
"""

import copy
import json
import random

import activity_grading

from activity_utils import (
    check_conditions,
    create_template_context,
    filter_content_blocks,
    get_progressive_hint,
    metadata_keys,
    pick_metadata_random,
    render_template,
    resolve_conditional_navigation,
    select_weighted_random,
)


class ActivityIO:
    """What the engine needs from where it runs. Every method is called by
    the engine; a subclass fills in the ones its place can do."""

    def say(self, content, username="System", shown_as=None):
        """A message to the learner (kept, where messages are kept)."""
        raise NotImplementedError

    def notice(self, content, pause=None):
        """A passing System line (a category, a skipped transition)."""
        raise NotImplementedError

    def save(self, state, gone_ok=False):
        """Keep the state; gone_ok: it may have been deleted (completed)."""

    def categorize(self, question, response, buckets, tokens_for_ai, model):
        raise NotImplementedError

    def readout(self):
        """The last categorization's readout (its probability p), or None."""
        return None

    def translate(self, text, language, model):
        return text

    def feedback(
        self,
        transition,
        bucket,
        question,
        tokens_for_ai,
        response,
        language,
        username,
        json_metadata,
        json_new_metadata,
        model,
    ):
        """One feedback message for a transition's ai_feedback, or ""."""
        raise NotImplementedError

    def feedback_prompts(
        self,
        transition,
        bucket,
        question,
        prompts,
        response,
        language,
        username,
        json_metadata,
        json_new_metadata,
        legacy_tokens,
        model,
    ):
        """[{"name", "content"}] for a step's feedback_prompts."""
        raise NotImplementedError

    def run_script(self, metadata, script):
        """A pre/post script's script_result dict (or None)."""
        raise NotImplementedError

    def set_background(self, image_base64):
        """A post-script's plot shown as the background, where one can be."""

    def offer_choices(self, offer):
        """A multiple-choice step's {choices: [{id, text}], multiple} for a
        page to show as buttons, where one can (never its answer). Its
        question already lists them, so typing an id works anywhere."""

    def complete(self, state, username, feedback_model):
        """The activity is over: report & forget it."""
        raise NotImplementedError


class MemoryState:
    """An activity's state kept in memory (a terminal run)."""

    def __init__(self, section_id, step_id, max_attempts=3, metadata=None):
        self.section_id = section_id
        self.step_id = step_id
        self.attempts = 0
        self.max_attempts = max_attempts
        self._metadata = dict(metadata or {})
        self.completed = False

    # A copy each read, as models.ActivityState's (JSON) is: what the
    # engine reads never changes under it.
    @property
    def dict_metadata(self):
        return json.loads(json.dumps(self._metadata))

    @dict_metadata.setter
    def dict_metadata(self, value):
        self._metadata = json.loads(json.dumps(value))

    def add_metadata(self, key, value):
        self._metadata[key] = json.loads(json.dumps(value))

    def remove_metadata(self, key):
        self._metadata.pop(key, None)

    def clear_metadata(self):
        self._metadata = {}


def models_for(content, default_model):
    """The activity's (classifier_model, feedback_model) defaults."""
    return (
        content.get("classifier_model", default_model),
        content.get("feedback_model", default_model),
    )


def get_next_step(activity_content, current_section_id, current_step_id):
    for section in activity_content["sections"]:
        if section["section_id"] == current_section_id:
            for i, step in enumerate(section["steps"]):
                if step["step_id"] == current_step_id:
                    if i + 1 < len(section["steps"]):
                        return section, section["steps"][i + 1]
                    else:
                        # Move to the next section
                        next_section_index = (
                            activity_content["sections"].index(section) + 1
                        )
                        if next_section_index < len(activity_content["sections"]):
                            next_section = activity_content["sections"][
                                next_section_index
                            ]
                            return next_section, next_section["steps"][0]
    return None, None


def current_step(content, state):
    """The state's current step, or None when it names none."""
    section = next(
        (s for s in content["sections"] if s["section_id"] == state.section_id),
        None,
    )
    if not section:
        return None
    return next((s for s in section["steps"] if s["step_id"] == state.step_id), None)


def show_until_question(content, state, io, username, feedback_model="MODEL_0"):
    """Say the current step's content & question; past a step with no
    question, go on to the next; with no next step, complete."""
    # Get the user's language preference from metadata
    user_language = state.dict_metadata.get("language", "English")

    while True:
        step = current_step(content, state)
        if not step:
            break

        context = create_template_context(
            metadata=state.dict_metadata,
            current_attempt=state.attempts + 1,  # the try they are on, from 1
            max_attempts=state.max_attempts,
            current_section=state.section_id,
            current_step=state.step_id,
            username=username,
        )

        # Say the current step content blocks (conditional blocks & templates)
        if "content_blocks" in step:
            filtered_blocks = filter_content_blocks(
                step["content_blocks"], state.dict_metadata, context
            )
            if filtered_blocks:
                io.say(
                    io.translate(
                        "\n\n".join(filtered_blocks), user_language, feedback_model
                    )
                )

        # A question: ask it & wait for an answer
        if "question" in step:
            question_content = io.translate(
                render_template(step["question"], context),
                user_language,
                feedback_model,
            )
            if activity_grading.is_multiple_choice(step):
                # Choice texts are content (translated); their ids never are.
                question_content += "\n\n" + io.translate(
                    activity_grading.choices_markdown(step),
                    user_language,
                    feedback_model,
                )
            io.say(
                question_content,
                username="System (Question)",
                shown_as="System",
            )
            if activity_grading.is_multiple_choice(step):
                io.offer_choices(activity_grading.choices_for_page(step))
            break

        # Move on: where this content step says (its own
        # next_section_and_step, a target or conditional list), else the
        # next step in order.
        next_section, next_step = _step_target(content, step, state)
        if next_step is None:
            next_section, next_step = get_next_step(
                content, state.section_id, state.step_id
            )
        if not next_step:
            io.complete(state, username, feedback_model)
            break
        state.attempts = 0
        state.section_id = next_section["section_id"]
        state.step_id = next_step["step_id"]
        io.save(state)


def _step_target(content, step, state):
    """A content step's own next_section_and_step, resolved against our
    metadata: (section, step), or (None, None) without one."""
    target = step.get("next_section_and_step")
    if not target:
        return None, None
    resolved = resolve_conditional_navigation(target, state.dict_metadata)
    if not resolved or ":" not in str(resolved):
        return None, None
    section_id, step_id = str(resolved).split(":", 1)
    for section in content["sections"]:
        if section["section_id"] == section_id:
            for candidate in section["steps"]:
                if candidate["step_id"] == step_id:
                    return section, candidate
    return None, None


def answer(content, state, user_response, username, io, default_model="MODEL_0"):
    """One answer to the current step: a question's is categorized & its
    transitions run; a step with no question just goes on."""
    turn = _Turn(
        io,
        state,
        content,
        user_response,
        username,
        models_for(content, default_model),
    )
    if "question" in turn.step:
        _answer_question(turn)
    else:
        show_until_question(content, state, io, username, turn.feedback_model)


# Buckets that keep a learner on the current step (attempts counted) unless
# a transition names a target or max attempts are used up.
_STAY_ON_STEP_BUCKETS = (
    "partial_understanding",
    "limited_effort",
    "asking_clarifying_questions",
    "set_language",
    "off_topic",
    "incorrect",  # Incorrect answers should stay on step and increment attempts
)

# Placeholder metadata value filled in with the model's feedback once it exists.
_LLMS_RESPONSE = "the-llms-response"


class _Turn:
    """One response to the current step: what every phase of
    handle_activity_response reads, plus what its transitions decide."""

    def __init__(self, io, state, content, user_response, username, default_models):
        self.io = io
        self.state = state
        self.content = content
        self.user_response = user_response
        self.username = username

        # Find the current section and step
        self.section = next(
            s for s in content["sections"] if s["section_id"] == state.section_id
        )
        self.step = next(
            s for s in self.section["steps"] if s["step_id"] == state.step_id
        )

        # Step-level model overrides (if specified), otherwise activity defaults
        default_classifier_model, default_feedback_model = default_models
        self.classifier_model = self.step.get(
            "classifier_model", default_classifier_model
        )
        self.feedback_model = self.step.get("feedback_model", default_feedback_model)
        self.feedback_tokens_for_ai = self.step.get("feedback_tokens_for_ai", "")

        # Temporary metadata keys across all transitions
        self.tmp_keys = []
        # The final navigation target (LAST transition's next_section_and_step wins)
        self.next_target = None
        # If ANY transition counts as an attempt, it counts
        self.counts_as_attempt = False
        # A multiple-choice step's transitions carry static feedback; a
        # model writes some only when the step asks (mc_ai_feedback).
        self.model_feedback = not (
            activity_grading.is_multiple_choice(self.step)
            and not self.step.get("mc_ai_feedback", False)
        )


def _answer_question(turn):
    """Categorize a response to a question step, run the transition of every
    active bucket, then advance or ask again."""
    step = turn.step
    state = turn.state

    # Runs before categorization, with user_response available
    _run_pre_script(turn)

    # Roll for random buckets BEFORE categorization
    triggered_random_buckets = _roll_random_buckets(turn)

    # Categorize the user's response: a multiple-choice answer by its key,
    # no model asked; anything else by the step's grader, which reads its
    # criteria, required elements & the clarifying bucket too.
    if activity_grading.is_multiple_choice(step):
        category = activity_grading.grade_choice(step, turn.user_response)
    else:
        # The grader reads what the learner read: templates filled in.
        context = _template_context(turn)
        category = turn.io.categorize(
            render_template(step["question"], context),
            turn.user_response,
            activity_grading.buckets_for(turn.content, step),
            render_template(
                activity_grading.grading_instructions(turn.content, step), context
            ),
            turn.classifier_model,
        )
    _announce_category(turn, category)

    # Combine user's category with triggered random buckets
    # User's response is processed FIRST, then random events
    all_active_buckets = [category] + triggered_random_buckets
    active_transitions = _active_transitions(step, all_active_buckets)

    # Error only if NO transitions found at all
    if not active_transitions:
        _notice(
            turn,
            f"Error: Unrecognized category '{category}'. Please try again.",
        )
        return

    # Process ALL active transitions in order
    user_language = state.dict_metadata.get("language", "English")
    ran = 0
    for index, (bucket_name, transition) in enumerate(active_transitions):
        # Emit separator between buckets (but not for the first one)
        if index:
            _notice(
                turn,
                f"\n{'='*60}\nProcessing transition for bucket: '{bucket_name}'\n{'='*60}",
                pause=0.05,
            )

        if not _transition_conditions_met(turn, bucket_name, transition):
            continue

        user_language = _run_transition(turn, bucket_name, transition)
        ran += 1

    # End of multi-bucket processing loop

    # Every transition skipped by its metadata_conditions: nothing applied,
    # so the question is asked again, no attempt counted.
    if not ran:
        _ask_question_again(turn, user_language)
        _drop_tmp_metadata(turn)
        return

    # This answer counts toward max attempts before we compare, so a max
    # of 3 means 3 answers, not 4.
    attempts = state.attempts + (1 if turn.counts_as_attempt else 0)
    # A wrong answer pointed back at its own step is staying, not leaving &
    # coming back: its attempt counts & max attempts moves it on (as
    # written, the jump reset attempts to 0, so a learner could never
    # leave). A loop on purpose (an 8 ball's "ask another") still re-runs.
    if category in _STAY_ON_STEP_BUCKETS and _targets_here(turn):
        turn.next_target = None
    if (
        category not in _STAY_ON_STEP_BUCKETS
        or attempts >= state.max_attempts
        or turn.next_target  # Use final navigation from last transition
    ):
        _advance(turn)
    else:
        # the user response is any bucket other than correct.
        _count_attempt(turn)
        # A step's hint for the miss just counted (hints: attempt N shows
        # after the Nth): never on an answer that moved on or cost nothing.
        hint = _progressive_hint(turn) if turn.counts_as_attempt else None
        if hint:
            _post_hint(turn, hint, user_language)
        _ask_question_again(turn, user_language)

    _drop_tmp_metadata(turn)


# ── categorize ───────────────────────────────────────────────────────


def _run_script(turn, script):
    """A step script, as SPEC.yaml promises it: `metadata` holds the answer
    as user_response & is read/write. The script's own writes (but
    user_response) are kept, then its script_result's metadata. Returns
    script_result."""
    before = turn.state.dict_metadata
    # A deep copy: a script changing a list or dict in place (an append)
    # must differ from `before`, or its write is never kept.
    metadata = copy.deepcopy(before)
    metadata["user_response"] = turn.user_response
    result = turn.io.run_script(metadata, script) or {}
    metadata.pop("user_response", None)
    for key in before:
        if key not in metadata:
            turn.state.remove_metadata(key)
    for key, value in metadata.items():
        if key not in before or before[key] != value:
            turn.state.add_metadata(key, value)
    for key, value in result.get("metadata", {}).items():
        turn.state.add_metadata(key, value)
    return result


def _run_pre_script(turn):
    """Run the step's pre_script before categorizing."""
    if "pre_script" not in turn.step:
        return
    print("DEBUG: Executing pre-script")
    _run_script(turn, turn.step["pre_script"])
    print("DEBUG: Pre-script completed, updated metadata")


def _roll_random_buckets(turn):
    """The step's random_buckets that fire this turn, each announced."""
    triggered = []
    if "random_buckets" not in turn.step:
        return triggered
    for bucket_name, config in turn.step["random_buckets"].items():
        probability = config.get("probability", 0)
        roll = random.random()
        if roll < probability:
            triggered.append(bucket_name)
            _notice(turn, f"🎲 [RANDOM EVENT] '{bucket_name}' triggered!", pause=0.05)
    return triggered


def _announce_category(turn, category):
    """Emit the category to the frontend, with the classifier's confidence
    when the provider gave one."""
    _ro = turn.io.readout()
    _conf = f" (confidence {_ro['p']:.0%})" if _ro and _ro.get("p") is not None else ""
    _notice(turn, f"Category: {category}{_conf}", pause=0.1)


def _active_transitions(step, buckets):
    """(bucket, transition) for every bucket that has a transition, in order:
    the step's own, else the built-in one for the clarifying bucket."""
    transitions = step.get("transitions") or {}
    active = []
    for bucket in buckets:
        found, transition = _find_transition(transitions, bucket)
        if transition is None:
            transition = activity_grading.transition_for(step, bucket)
            found = bucket
        if transition is not None:  # `a: {}` is a transition that does nothing
            active.append((found, transition))
    return active


def _find_transition(transitions, bucket):
    """A bucket's transition as YAML keyed it: as written, as an integer, or
    as the boolean YAML reads yes/no/true/false into."""
    if bucket in transitions:
        return bucket, transitions[bucket]
    if str(bucket).isdigit() and int(bucket) in transitions:
        return bucket, transitions[int(bucket)]
    # Try boolean conversion
    if str(bucket).lower() in ["yes", "true"]:
        bucket = True
    elif str(bucket).lower() in ["no", "false"]:
        bucket = False
    if bucket in transitions:
        return bucket, transitions[bucket]
    return bucket, None


def _transition_conditions_met(turn, bucket_name, transition):
    """Check metadata conditions for the transition (v2.0 advanced
    conditions); a transition whose conditions fail is skipped & said so."""
    if "metadata_conditions" not in transition:
        return True
    if check_conditions(turn.state.dict_metadata, transition["metadata_conditions"]):
        return True
    _notice(
        turn,
        f"Skipping '{bucket_name}' - metadata conditions not met",
        pause=0.05,
    )
    return False


# ── one transition ───────────────────────────────────────────────────


def _run_transition(turn, bucket_name, transition):
    """Apply one transition: metadata, post-script, content & feedback, then
    record its navigation & attempt counting. Returns the language its
    content was shown in."""
    state = turn.state

    # this gives the llm context on what changed.
    new_metadata = _apply_metadata_operations(turn, transition)

    # Track navigation (LAST transition's next_section_and_step wins); set
    # before the post-script so a script's own target overrides it.
    if "next_section_and_step" in transition:
        turn.next_target = transition["next_section_and_step"]

    _run_post_script(turn, transition)

    if "metadata_clear" in transition and transition["metadata_clear"] == True:
        state.clear_metadata()

    print(f"DEBUG: metadata {json.dumps(state.dict_metadata)}")

    # Save the changes after processing this transition
    turn.io.save(state)

    user_language = state.dict_metadata.get("language", "English")

    _post_transition_content(turn, transition, user_language)

    _post_feedback(turn, bucket_name, transition, user_language, new_metadata)

    # Track counts_as_attempt (if ANY transition counts, it counts)
    if transition.get("counts_as_attempt", True):
        turn.counts_as_attempt = True

    return user_language


def _apply_metadata_operations(turn, transition):
    """Every metadata_* operation of a transition, in our fixed order.
    Returns the keys & values it set (the llm's view of what changed)."""
    new_metadata = {}

    # Update metadata based on user actions
    if "metadata_add" in transition:
        _add_metadata(turn, transition["metadata_add"], new_metadata, tmp=False)
    if "metadata_tmp_add" in transition:
        _add_metadata(turn, transition["metadata_tmp_add"], new_metadata, tmp=True)

    # Update metadata by appending values to lists
    if "metadata_append" in transition:
        _append_metadata(turn, transition["metadata_append"], tmp=False)
    if "metadata_tmp_append" in transition:
        _append_metadata(turn, transition["metadata_tmp_append"], tmp=True)

    if "metadata_remove" in transition:
        for key in metadata_keys(transition["metadata_remove"]):
            turn.state.remove_metadata(key)

    if "metadata_random" in transition:
        _add_random_key(turn, transition["metadata_random"], new_metadata, tmp=False)
    if "metadata_tmp_random" in transition:
        _add_random_key(turn, transition["metadata_tmp_random"], new_metadata, tmp=True)

    # Handle metadata_weighted_random (v2.0)
    if "metadata_weighted_random" in transition:
        _add_weighted_random(
            turn, transition["metadata_weighted_random"], new_metadata, tmp=False
        )
    if "metadata_tmp_weighted_random" in transition:
        _add_weighted_random(
            turn, transition["metadata_tmp_weighted_random"], new_metadata, tmp=True
        )

    return new_metadata


def _set_metadata(turn, key, value, new_metadata, tmp):
    new_metadata[key] = value
    if tmp:
        turn.tmp_keys.append(key)
    turn.state.add_metadata(key, value)


def _add_metadata(turn, items, new_metadata, tmp):
    """metadata_add / metadata_tmp_add: each value resolved against the
    response & the key's current value."""
    for key, value in items.items():
        if value == _LLMS_RESPONSE:
            continue  # Handled after feedback
        value = _resolve_metadata_value(turn, key, value)
        _set_metadata(turn, key, value, new_metadata, tmp)


def _resolve_metadata_value(turn, key, value):
    """the-users-response, n+random(x,y), n+c / n-c (numeric), n+,s / n-,s
    (comma-separated list) or a literal."""
    if value == "the-users-response":
        return turn.user_response
    if not isinstance(value, str):
        return value
    current = turn.state.dict_metadata
    if value.startswith("n+random(") and value.endswith(")"):
        # Extract the range and apply the random increment
        range_values = value[9:-1].split(",")
        if len(range_values) == 2:
            x, y = map(int, range_values)
            return _number(current.get(key), key) + random.randint(x, y)
        return value
    # Check if this is string concatenation (n+,value) or numeric operation (n+5)
    if value.startswith("n+,") or value.startswith("n-,"):
        return _comma_list_operation(current.get(key, ""), value)
    if value.startswith("n+") or value.startswith("n-"):
        return _numeric_operation(current, key, value)
    return value


def _comma_list_operation(existing_value, value):
    """String concatenation: append/remove from existing value"""
    operation = value[:2]  # "n+" or "n-"
    suffix = value[3:]  # Everything after "n+," or "n-,"
    if operation == "n+":
        # Append with comma separator if existing value is non-empty
        if existing_value:
            return f"{existing_value},{suffix}"
        return suffix
    # Remove suffix from existing value
    if existing_value:
        parts = existing_value.split(",")
        parts = [p for p in parts if p != suffix]
        return ",".join(parts)
    return existing_value


def _number(existing, key):
    """A stored value to count from: missing, None or blank is 0, & a number
    written as text ('0', "2.5", what a YAML author quoted or a script
    stored) is that number. Anything else counts from 0, said so, rather
    than raise mid-turn (an activity used to die on its first `n+1`)."""
    if existing is None or existing == "":
        return 0
    if isinstance(existing, bool):
        return int(existing)
    if isinstance(existing, (int, float)):
        return existing
    try:
        return int(str(existing).strip())
    except ValueError:
        pass
    try:
        return float(str(existing).strip())
    except ValueError:
        print(f"Warning: {key}={existing!r} is not a number; counting from 0")
        return 0


def _numeric_operation(current, key, value):
    """n+c / n-c: c added to or taken from the key's number (_number)."""
    try:
        c = int(value[2:])
    except ValueError:
        print(f"Warning: Invalid numeric operation '{value}' for key '{key}'")
        return value
    base = _number(current.get(key), key)
    return base + c if value.startswith("n+") else base - c


def _append_metadata(turn, items, tmp):
    """metadata_append / metadata_tmp_append: append values to lists."""
    for key, value in items.items():
        # Determine the value to append
        if value == "the-users-response":
            value_to_append = turn.user_response
        elif value == _LLMS_RESPONSE:
            continue  # Handle this after feedback
        else:
            value_to_append = value

        _append_to_list(turn.state, key, value_to_append)

        # Track temporary metadata keys
        if tmp:
            turn.tmp_keys.append(key)


def _append_to_list(state, key, value):
    # Ensure the key exists and is a list
    current_value = state.dict_metadata.get(key, [])
    if not isinstance(current_value, list):
        current_value = [current_value]

    # Append the value to the list
    if isinstance(value, list):
        current_value.extend(value)
    else:
        current_value.append(value)

    # Update the metadata
    state.add_metadata(key, current_value)


def _add_random_key(turn, mapping, new_metadata, tmp):
    """metadata_random / metadata_tmp_random: one key of the mapping, picked
    at random, set to its value (one item of it, when it is a list)."""
    random_key, value = pick_metadata_random(mapping)
    _set_metadata(turn, random_key, value, new_metadata, tmp)


def _add_weighted_random(turn, items, new_metadata, tmp):
    for key, weighted_options in items.items():
        selected_value = select_weighted_random(weighted_options)
        _set_metadata(turn, key, selected_value, new_metadata, tmp)


def _run_post_script(turn, transition):
    """Execute the post-script if it exists (supports both old and new
    naming) & the transition asks for it."""
    post_script = turn.step.get("post_script") or turn.step.get("processing_script")
    if not post_script or not (
        transition.get("run_post_script", False)
        or transition.get("run_processing_script", False)
    ):
        return
    state = turn.state
    print("DEBUG: Executing post-script")
    result = _run_script(turn, post_script)

    plot_image_base64 = result.pop("plot_image", None)

    # Add the result to the temporary metadata for use in machine learning feedback
    turn.tmp_keys.append("processing_script_result")
    state.add_metadata("processing_script_result", result)

    # Check if processing script wants to override the transition
    if "next_section_and_step" in result:
        turn.next_target = result["next_section_and_step"]
        print(f"DEBUG: Processing script overriding transition to: {turn.next_target}")

    # Check if the result contains a plot image
    if plot_image_base64:
        _post_plot_image(turn, plot_image_base64, result.get("set_background", False))


def _post_plot_image(turn, plot_image_base64, set_background):
    if set_background:
        turn.io.set_background(plot_image_base64)
        return
    # Save the plot image to the database, posted as the user
    plot_image_html = (
        f'<img alt="Plot Image" src="data:image/png;base64,{plot_image_base64}">'
    )
    _post_message(turn, plot_image_html, username=turn.username)


# ── feedback ─────────────────────────────────────────────────────────


def _post_transition_content(turn, transition, user_language):
    """Emit the transition content blocks if they exist (v2.0 with templates
    & conditions)"""
    if "content_blocks" not in transition:
        return
    # Filter and render content blocks (supports conditional blocks and templates)
    filtered_blocks = filter_content_blocks(
        transition["content_blocks"],
        turn.state.dict_metadata,
        _template_context(turn),
    )
    if filtered_blocks:
        transition_content = "\n\n".join(filtered_blocks)
        _post_message(
            turn,
            turn.io.translate(transition_content, user_language, turn.feedback_model),
        )


def _post_feedback(turn, bucket_name, transition, user_language, new_metadata):
    """Model feedback for a transition: one message per feedback prompt, or
    one from the legacy feedback_tokens_for_ai. the-llms-response metadata
    takes the legacy feedback once it exists."""
    step = turn.step
    state = turn.state
    feedback_messages = []
    if not turn.model_feedback:
        return
    # The feedback model reads what the learner read: templates filled in.
    context = _template_context(turn)
    question = render_template(step["question"], context)
    legacy_tokens = render_template(turn.feedback_tokens_for_ai or "", context)
    if isinstance(transition.get("ai_feedback"), dict) and isinstance(
        transition["ai_feedback"].get("tokens_for_ai"), str
    ):
        transition = {
            **transition,
            "ai_feedback": {
                **transition["ai_feedback"],
                "tokens_for_ai": render_template(
                    transition["ai_feedback"]["tokens_for_ai"], context
                ),
            },
        }

    if "feedback_prompts" in step:
        # New multi-prompt system - pass full metadata, let each prompt filter
        multi_feedback_messages = turn.io.feedback_prompts(
            transition,
            bucket_name,  # Use bucket_name instead of category
            question,
            step["feedback_prompts"],
            turn.user_response,
            user_language,
            turn.username,
            json.dumps(state.dict_metadata),  # Pass full metadata
            json.dumps(new_metadata),
            legacy_tokens,  # Pass legacy tokens to be combined
            turn.feedback_model,
        )
        feedback_messages.extend(multi_feedback_messages)
    elif turn.feedback_tokens_for_ai or "ai_feedback" in transition:
        # Single feedback: the transition's ai_feedback, with the step's
        # feedback_tokens_for_ai (if any) before it; transition-level filtering
        feedback_metadata = state.dict_metadata
        if "metadata_feedback_filter" in transition:
            filter_keys = transition["metadata_feedback_filter"]
            feedback_metadata = {
                k: v for k, v in state.dict_metadata.items() if k in filter_keys
            }

        feedback = turn.io.feedback(
            transition,
            bucket_name,  # Use bucket_name instead of category
            question,
            legacy_tokens,
            turn.user_response,
            user_language,
            turn.username,
            json.dumps(feedback_metadata),
            json.dumps(new_metadata),
            turn.feedback_model,
        )
        if feedback and feedback.strip():
            feedback_messages.append({"name": "Feedback", "content": feedback})

    # Store and emit all feedback messages; the-llms-response takes each
    # message as it is posted (the last one, for metadata_add).
    for feedback_msg in feedback_messages:
        feedback = feedback_msg["content"]
        _post_message(
            turn,
            feedback_msg["content"],
            username=f"System ({feedback_msg['name'].title()})",
        )

        # Add or append the LLM's response to the metadata
        for key, value in transition.get("metadata_add", {}).items():
            if value == _LLMS_RESPONSE:
                state.add_metadata(key, feedback)

        for key, value in transition.get("metadata_append", {}).items():
            if value == _LLMS_RESPONSE:
                _append_to_list(state, key, feedback)


def _progressive_hint(turn):
    """The step's hint for the next attempt (v2.0), or None."""
    if "hints" not in turn.step:
        return None
    context = _template_context(turn)
    return get_progressive_hint(turn.step["hints"], turn.state.attempts, context)


def _post_hint(turn, hint, user_language):
    translated_hint = turn.io.translate(
        hint["text"], user_language, turn.feedback_model
    )
    # Showing a hint never changes counting: the miss before it counted,
    # & a hint that undid it held the learner on one hint forever.
    _post_message(turn, translated_hint, username="System (Hint)")


# ── advance, ask again & persist ─────────────────────────────────────


def _advance(turn):
    """Move to the navigation target (or the next step in order) & run
    steps until the next question; with no next step, stay put."""
    next_section, next_step = _next_section_and_step(turn)
    if not next_step:
        # The activity's last question, answered: we're done.
        turn.io.complete(turn.state, turn.username, turn.feedback_model)
        return
    state = turn.state
    state.attempts = 0
    state.section_id = next_section["section_id"]
    state.step_id = next_step["step_id"]

    turn.io.save(state)

    # Loop through steps until a question is found or the end is reached
    show_until_question(
        turn.content, state, turn.io, turn.username, turn.feedback_model
    )


def _targets_here(turn):
    if not turn.next_target:
        return False
    resolved = resolve_conditional_navigation(
        turn.next_target, turn.state.dict_metadata
    )
    return resolved == f"{turn.section['section_id']}:{turn.step['step_id']}"


def _next_section_and_step(turn):
    if turn.next_target:
        # Resolve conditional navigation (v2.0)
        resolved_navigation = resolve_conditional_navigation(
            turn.next_target, turn.state.dict_metadata
        )
        if resolved_navigation:
            section_id, step_id = resolved_navigation.split(":")
            next_section = next(
                s for s in turn.content["sections"] if s["section_id"] == section_id
            )
            next_step = next(
                s for s in next_section["steps"] if s["step_id"] == step_id
            )
            return next_section, next_step
    # No navigation resolved, move to the next step or section
    return get_next_step(turn.content, turn.section["section_id"], turn.step["step_id"])


def _count_attempt(turn):
    # Count attempt if ANY transition counted
    if turn.counts_as_attempt:
        turn.state.attempts += 1
        turn.io.save(turn.state)


def _ask_question_again(turn, user_language):
    """Emit the question again (v2.0 with templates)"""
    question_content = turn.io.translate(
        render_template(turn.step["question"], _template_context(turn)),
        user_language,
        turn.feedback_model,
    )
    choices = activity_grading.is_multiple_choice(turn.step)
    if choices:
        question_content += "\n\n" + turn.io.translate(
            activity_grading.choices_markdown(turn.step),
            user_language,
            turn.feedback_model,
        )
    _post_message(
        turn,
        question_content,
        username="System (Question)",
        shown_as="System",
    )
    if choices:
        turn.io.offer_choices(activity_grading.choices_for_page(turn.step))


def _drop_tmp_metadata(turn):
    # Remove temporary metadata at the end of the turn; the state may be
    # gone (the activity completed), which saving tolerates.
    for key in turn.tmp_keys:
        turn.state.remove_metadata(key)
    turn.io.save(turn.state, gone_ok=True)


def _template_context(turn, attempt_offset=0):
    state = turn.state
    return create_template_context(
        metadata=state.dict_metadata,
        # The try a learner is on, from 1 (state.attempts counts misses):
        # after one miss the question reads "Attempt 2 of 3".
        current_attempt=state.attempts + 1 + attempt_offset,
        max_attempts=state.max_attempts,
        current_section=state.section_id,
        current_step=state.step_id,
        username=turn.username,
    )


def _post_message(turn, content, username="System", shown_as=None):
    """A message to the room (as `shown_as` when the stored name differs
    from the one shown)."""
    turn.io.say(content, username=username, shown_as=shown_as)


def _notice(turn, content, pause=None):
    """A System notice that is never stored."""
    turn.io.notice(content, pause=pause)
