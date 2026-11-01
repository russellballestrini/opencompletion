import json
import yaml
import os
import threading
import random
import gevent

from flask import request
from sqlalchemy.exc import InvalidRequestError

# Import app, socketio, and db from the main module
# These will be set via import when this module is imported by app.py
app = None
socketio = None
db = None

# Import models
from models import Room, Message, ActivityState

# Import helper functions from app.py
# These will be imported when this module is loaded
get_room = None
get_s3_client = None
get_openai_client_and_model = None

# Import SYSTEM_USERS from app.py
SYSTEM_USERS = None

# Import activity utilities for v2.0 features
from activity_utils import (
    notice_id,
    render_template,
    evaluate_condition,
    check_conditions,
    filter_content_blocks,
    resolve_conditional_navigation,
    select_weighted_random,
    get_progressive_hint,
    create_template_context,
    create_completion_skip_thinking,
    strip_reasoning,
    resolve_bucket,
    fold_first_token_logprobs,
    first_token_top_logprobs,
)

# A second kind of model: a decision endpoint that answers a categorization
# AS a decision (probability per bucket). Asked first when configured,
# fails open onto the chat path below.
import classifier as _classifier


def handle_get_activity_status(data):
    """Get the current activity status for a room."""
    room_name = data["room_name"]
    room = get_room(room_name)

    if room:
        activity_state = ActivityState.query.filter_by(room_id=room.id).first()

        if activity_state:
            socketio.emit(
                "activity_status",
                {
                    "active": True,
                    "activity_name": activity_state.s3_file_path,
                    "section_id": activity_state.section_id,
                    "step_id": activity_state.step_id,
                },
                room=request.sid,
            )
        else:
            socketio.emit("activity_status", {"active": False}, room=request.sid)


def get_activity_content(file_path):
    """
    Load the activity content from either S3 or the local filesystem based on the configuration.
    """
    if app.config["LOCAL_ACTIVITIES"]:
        # Load the activity YAML from a local file with path traversal protection
        import os.path

        # Normalize the path and ensure it's within the research directory
        normalized_path = os.path.normpath(file_path)

        # Ensure path doesn't contain dangerous patterns
        if ".." in normalized_path or normalized_path.startswith("/"):
            raise ValueError(f"Invalid file path: {file_path}")

        # Ensure file is within research directory and has .yaml extension
        if not normalized_path.startswith("research/") or not normalized_path.endswith(
            ".yaml"
        ):
            raise ValueError(
                f"File must be in research/ directory and end with .yaml: {file_path}"
            )

        # Additional safety check - ensure resolved path is still in research dir
        full_path = os.path.abspath(normalized_path)
        research_dir = os.path.abspath("research/")
        if not full_path.startswith(research_dir):
            raise ValueError(f"Path traversal attempt detected: {file_path}")

        with open(normalized_path, "r") as file:
            activity_yaml = file.read()
    else:
        # Load the activity YAML from S3
        s3_client = get_s3_client()
        bucket_name = os.environ.get("S3_BUCKET_NAME")
        response = s3_client.get_object(Bucket=bucket_name, Key=file_path)
        activity_yaml = response["Body"].read().decode("utf-8")

    return yaml.safe_load(activity_yaml)


def loop_through_steps_until_question(
    activity_content,
    activity_state,
    room_name,
    username,
    classifier_model="MODEL_0",
    feedback_model="MODEL_0",
):
    room = get_room(room_name)

    current_section_id = activity_state.section_id
    current_step_id = activity_state.step_id

    # Get the user's language preference from metadata
    user_language = activity_state.dict_metadata.get("language", "English")

    while True:
        section = next(
            (
                s
                for s in activity_content["sections"]
                if s["section_id"] == current_section_id
            ),
            None,
        )
        if not section:
            break

        step = next(
            (s for s in section["steps"] if s["step_id"] == current_step_id), None
        )
        if not step:
            break

        # Emit the current step content blocks
        if "content_blocks" in step:
            # Create template context
            context = create_template_context(
                metadata=activity_state.dict_metadata,
                current_attempt=activity_state.attempts,
                max_attempts=activity_state.max_attempts,
                current_section=current_section_id,
                current_step=current_step_id,
                username=username,
            )

            # Filter and render content blocks (supports conditional blocks and templates)
            filtered_blocks = filter_content_blocks(
                step["content_blocks"], activity_state.dict_metadata, context
            )

            if filtered_blocks:
                content = "\n\n".join(filtered_blocks)
                translated_content = translate_text(
                    content, user_language, feedback_model
                )
                new_message = Message(
                    username="System", content=translated_content, room_id=room.id
                )
                db.session.add(new_message)
                db.session.commit()

                socketio.emit(
                    "chat_message",
                    {
                        "id": new_message.id,
                        "username": "System",
                        "content": translated_content,
                    },
                    room=room_name,
                )
                socketio.sleep(0.1)

        # Check if the current step has a question
        if "question" in step:
            # Create template context
            context = create_template_context(
                metadata=activity_state.dict_metadata,
                current_attempt=activity_state.attempts,
                max_attempts=activity_state.max_attempts,
                current_section=current_section_id,
                current_step=current_step_id,
                username=username,
            )

            # Render template variables in question
            question_content = render_template(step["question"], context)
            translated_question_content = translate_text(
                question_content, user_language, feedback_model
            )
            new_message = Message(
                username="System (Question)",
                content=translated_question_content,
                room_id=room.id,
            )
            db.session.add(new_message)
            db.session.commit()

            socketio.emit(
                "chat_message",
                {
                    "id": new_message.id,
                    "username": "System",
                    "content": translated_question_content,
                },
                room=room_name,
            )
            socketio.sleep(0.1)
            break

        # Move to the next step
        next_section, next_step = get_next_step(
            activity_content, current_section_id, current_step_id
        )

        if next_step:
            activity_state.attempts = 0
            activity_state.section_id = next_section["section_id"]
            activity_state.step_id = next_step["step_id"]

            db.session.add(activity_state)
            db.session.commit()

            current_section_id = next_section["section_id"]
            current_step_id = next_step["step_id"]
        else:
            # Activity completed

            # Display activity info before completing
            display_activity_info(room_name, username, feedback_model)

            db.session.delete(activity_state)
            db.session.commit()
            socketio.emit(
                "chat_message",
                {
                    "id": notice_id(),
                    "username": "System",
                    "content": "Activity completed!",
                },
                room=room_name,
            )
            # Return to activity chooser
            socketio.emit("activity_status", {"active": False}, room=room_name)
            break


def start_activity(room_name, s3_file_path, username):
    activity_content = get_activity_content(s3_file_path)

    with app.app_context():
        # Save the initial state to the database
        room = get_room(room_name)
        initial_section = activity_content["sections"][0]
        initial_step = initial_section["steps"][0]

        activity_state = ActivityState(
            room_id=room.id,
            section_id=initial_section["section_id"],
            step_id=initial_step["step_id"],
            max_attempts=activity_content.get("default_max_attempts_per_step", 3),
            s3_file_path=s3_file_path,  # Save the S3 file path
        )
        db.session.add(activity_state)
        db.session.commit()

        # Get model configuration from activity content if specified
        # Default to MODEL_0 (Hermes) for both - fast, accurate, and always available
        classifier_model = activity_content.get("classifier_model", "MODEL_0")
        feedback_model = activity_content.get("feedback_model", "MODEL_0")

        # Loop through steps until a question is found or the end is reached
        loop_through_steps_until_question(
            activity_content,
            activity_state,
            room_name,
            username,
            classifier_model=classifier_model,
            feedback_model=feedback_model,
        )

        # Emit activity status update
        socketio.emit(
            "activity_status",
            {
                "active": True,
                "activity_name": s3_file_path,
                "section_id": initial_section["section_id"],
                "step_id": initial_step["step_id"],
            },
            room=room_name,
        )


def cancel_activity(room_name, username):
    with app.app_context():
        room = get_room(room_name)
        activity_state = ActivityState.query.filter_by(room_id=room.id).first()

        if not activity_state:
            socketio.emit(
                "chat_message",
                {
                    "id": notice_id(),
                    "username": "System",
                    "content": "No active activity found to cancel.",
                },
                room=room_name,
            )
            return

        # Delete the activity state
        db.session.delete(activity_state)
        db.session.commit()

        # Emit a message indicating the activity has been canceled
        socketio.emit(
            "chat_message",
            {
                "id": notice_id(),
                "username": "System",
                "content": "Activity has been canceled.",
            },
            room=room_name,
        )

        # Emit activity status update
        socketio.emit("activity_status", {"active": False}, room=room_name)


def display_activity_metadata(room_name, username):
    with app.app_context():
        room = get_room(room_name)
        activity_state = ActivityState.query.filter_by(room_id=room.id).first()

        if not activity_state:
            socketio.emit(
                "chat_message",
                {
                    "id": notice_id(),
                    "username": "System",
                    "content": "No active activity found.",
                },
                room=room_name,
            )
            return

        # Pretty print the metadata
        metadata_pretty = json.dumps(activity_state.dict_metadata, indent=2)

        # Store and emit the metadata
        metadata_message = f"```\n{metadata_pretty}\n```"
        new_message = Message(
            username="System", content=metadata_message, room_id=room.id
        )
        db.session.add(new_message)
        db.session.commit()

        socketio.emit(
            "chat_message",
            {
                "id": new_message.id,
                "username": "System",
                "content": metadata_message,
            },
            room=room_name,
        )


def execute_processing_script(metadata, script):
    # Prepare the environment for the script
    # Use the same dict for both globals and locals to support comprehensions
    script_env = {
        "__builtins__": __builtins__,
        "metadata": metadata,
        "script_result": None,
    }

    # Execute the script
    exec(script, script_env, script_env)

    # Return the result from the script
    return script_env["script_result"]


def handle_activity_response(room_name, user_response, username, model="MODEL_0"):
    with app.app_context():
        room = get_room(room_name)
        activity_state = ActivityState.query.filter_by(room_id=room.id).first()

        if not activity_state:
            return

        # Load the activity content
        activity_content = get_activity_content(activity_state.s3_file_path)

        # Get activity-level model defaults
        # Default to MODEL_0 (Hermes) for both - fast, accurate, and always available
        default_models = (
            activity_content.get("classifier_model", "MODEL_0"),
            activity_content.get("feedback_model", "MODEL_0"),
        )

        try:
            turn = _Turn(
                room,
                room_name,
                activity_state,
                activity_content,
                user_response,
                username,
                default_models,
            )

            # Check if the step has a question
            if "question" in turn.step:
                _answer_question(turn)
            else:
                # Handle steps without a question
                loop_through_steps_until_question(
                    activity_content,
                    activity_state,
                    room_name,
                    username,
                    classifier_model=turn.classifier_model,
                    feedback_model=turn.feedback_model,
                )

        except Exception as e:
            import traceback

            msg = traceback.format_exc()
            socketio.emit(
                "chat_message",
                {
                    "id": notice_id(),
                    "username": "System",
                    "content": f"Error processing activity response: {e}\n\n{msg}",
                },
                room=room_name,
            )


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

    def __init__(
        self, room, room_name, state, content, user_response, username, default_models
    ):
        self.room = room
        self.room_name = room_name
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


def _answer_question(turn):
    """Categorize a response to a question step, run the transition of every
    active bucket, then advance or ask again."""
    step = turn.step
    state = turn.state

    # Runs before categorization, with user_response available
    _run_pre_script(turn)

    # Roll for random buckets BEFORE categorization
    triggered_random_buckets = _roll_random_buckets(turn)

    # Categorize the user's response
    category = categorize_response(
        step["question"],
        turn.user_response,
        step["buckets"],
        step.get("tokens_for_ai", ""),
        turn.classifier_model,
    )
    _announce_category(turn, category)

    # Combine user's category with triggered random buckets
    # User's response is processed FIRST, then random events
    all_active_buckets = [category] + triggered_random_buckets
    active_transitions = _active_transitions(step["transitions"], all_active_buckets)

    # Error only if NO transitions found at all
    if not active_transitions:
        _notice(
            turn,
            f"Error: Unrecognized category '{category}'. Please try again.",
        )
        return

    # Process ALL active transitions in order. user_language is bound by the
    # first transition that runs; with every one skipped it stays unbound &
    # asking again raises (pinned by our characterisation tests).
    for bucket_name, transition in active_transitions:
        # Emit separator between buckets (but not for the first one)
        if bucket_name != all_active_buckets[0]:
            _notice(
                turn,
                f"\n{'='*60}\nProcessing transition for bucket: '{bucket_name}'\n{'='*60}",
                pause=0.05,
            )

        if not _transition_conditions_met(turn, bucket_name, transition):
            continue

        user_language = _run_transition(turn, bucket_name, transition)

    # End of multi-bucket processing loop

    # Check for progressive hints (v2.0)
    hint = _progressive_hint(turn)
    if hint:
        _post_hint(turn, hint, user_language)

    if (
        category not in _STAY_ON_STEP_BUCKETS
        or state.attempts >= state.max_attempts
        or turn.next_target  # Use final navigation from last transition
    ):
        _advance(turn)
    else:
        # the user response is any bucket other than correct.
        _count_attempt(turn)
        _ask_question_again(turn, user_language)

    _drop_tmp_metadata(turn)


# ── categorize ───────────────────────────────────────────────────────


def _run_pre_script(turn):
    """Run the step's pre_script on a copy of metadata holding user_response;
    its result's metadata is kept."""
    if "pre_script" not in turn.step:
        return
    print("DEBUG: Executing pre-script")
    # Add user_response to a temporary copy of metadata for pre_script
    temp_metadata = turn.state.dict_metadata.copy()
    temp_metadata["user_response"] = turn.user_response
    pre_result = execute_processing_script(temp_metadata, turn.step["pre_script"]) or {}
    # Update metadata with pre-script results
    for key, value in pre_result.get("metadata", {}).items():
        turn.state.add_metadata(key, value)
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
    _ro = last_readout()
    _conf = f" (confidence {_ro['p']:.0%})" if _ro and _ro.get("p") is not None else ""
    _notice(turn, f"Category: {category}{_conf}", pause=0.1)


def _active_transitions(transitions, buckets):
    """(bucket, transition) for every bucket that has a transition, in order."""
    active = []
    for bucket in buckets:
        bucket, transition = _find_transition(transitions, bucket)
        if transition:
            active.append((bucket, transition))
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

    _run_post_script(turn, transition)

    if "metadata_clear" in transition and transition["metadata_clear"] == True:
        state.clear_metadata()

    print(state.dict_metadata)

    # Commit the changes after processing this transition
    db.session.add(state)
    db.session.commit()

    user_language = state.dict_metadata.get("language", "English")

    _post_transition_content(turn, transition, user_language)

    _post_feedback(turn, bucket_name, transition, user_language, new_metadata)

    # Track navigation (LAST transition's next_section_and_step wins)
    if "next_section_and_step" in transition:
        turn.next_target = transition["next_section_and_step"]

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
        for key in transition["metadata_remove"]:
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
            return current.get(key, 0) + random.randint(x, y)
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


def _numeric_operation(current, key, value):
    """Numeric operation: extract the numeric part c and apply the operation +/-"""
    try:
        c = int(value[2:])
        if value.startswith("n+"):
            return current.get(key, 0) + c
        return current.get(key, 0) - c
    except ValueError:
        print(f"Warning: Invalid numeric operation '{value}' for key '{key}'")
        return value


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
    at random, set to its value."""
    random_key = random.choice(list(mapping.keys()))
    _set_metadata(turn, random_key, mapping[random_key], new_metadata, tmp)


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
    result = execute_processing_script(state.dict_metadata, post_script) or {}

    plot_image_base64 = result.pop("plot_image", None)

    # Add the result to the temporary metadata for use in machine learning feedback
    turn.tmp_keys.append("processing_script_result")
    state.add_metadata("processing_script_result", result)

    # Update metadata with results from the processing script
    for key, value in result.get("metadata", {}).items():
        state.add_metadata(key, value)

    # Check if processing script wants to override the transition
    if "next_section_and_step" in result:
        turn.next_target = result["next_section_and_step"]
        print(f"DEBUG: Processing script overriding transition to: {turn.next_target}")

    # Check if the result contains a plot image
    if plot_image_base64:
        _post_plot_image(turn, plot_image_base64, result.get("set_background", False))


def _post_plot_image(turn, plot_image_base64, set_background):
    if set_background:
        socketio.emit(
            "set_background",
            {"image_data": plot_image_base64},
            room=turn.room_name,
        )
        socketio.sleep(0.1)
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
            translate_text(transition_content, user_language, turn.feedback_model),
        )


def _post_feedback(turn, bucket_name, transition, user_language, new_metadata):
    """Model feedback for a transition: one message per feedback prompt, or
    one from the legacy feedback_tokens_for_ai. the-llms-response metadata
    takes the legacy feedback once it exists."""
    step = turn.step
    state = turn.state
    feedback_messages = []

    if "feedback_prompts" in step:
        # New multi-prompt system - pass full metadata, let each prompt filter
        multi_feedback_messages = provide_feedback_prompts(
            transition,
            bucket_name,  # Use bucket_name instead of category
            step["question"],
            step["feedback_prompts"],
            turn.user_response,
            user_language,
            turn.username,
            json.dumps(state.dict_metadata),  # Pass full metadata
            json.dumps(new_metadata),
            turn.feedback_tokens_for_ai,  # Pass legacy tokens to be combined
            turn.feedback_model,
        )
        feedback_messages.extend(multi_feedback_messages)
    elif turn.feedback_tokens_for_ai:
        # Legacy single feedback system - use transition-level filtering
        feedback_metadata = state.dict_metadata
        if "metadata_feedback_filter" in transition:
            filter_keys = transition["metadata_feedback_filter"]
            feedback_metadata = {
                k: v for k, v in state.dict_metadata.items() if k in filter_keys
            }

        feedback = provide_feedback(
            transition,
            bucket_name,  # Use bucket_name instead of category
            step["question"],
            turn.feedback_tokens_for_ai,
            turn.user_response,
            user_language,
            turn.username,
            json.dumps(feedback_metadata),
            json.dumps(new_metadata),
            turn.feedback_model,
        )
        if feedback and feedback.strip():
            feedback_messages.append({"name": "Feedback", "content": feedback})

    # Store and emit all feedback messages. `feedback` is only bound on the
    # legacy path: the-llms-response beside feedback_prompts raises
    # (pinned by our characterisation tests).
    for feedback_msg in feedback_messages:
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
    context = _template_context(turn, attempt_offset=1)  # Next attempt
    return get_progressive_hint(turn.step["hints"], turn.state.attempts + 1, context)


def _post_hint(turn, hint, user_language):
    translated_hint = translate_text(hint["text"], user_language, turn.feedback_model)
    _post_message(turn, translated_hint, username="System (Hint)")

    # If hint doesn't count as attempt, don't increment
    if not hint["counts_as_attempt"]:
        turn.counts_as_attempt = False


# ── advance, ask again & persist ─────────────────────────────────────


def _advance(turn):
    """Move to the navigation target (or the next step in order) & run
    steps until the next question; with no next step, stay put."""
    next_section, next_step = _next_section_and_step(turn)
    if not next_step:
        return
    state = turn.state
    state.attempts = 0
    state.section_id = next_section["section_id"]
    state.step_id = next_step["step_id"]

    db.session.add(state)
    db.session.commit()

    # Loop through steps until a question is found or the end is reached
    loop_through_steps_until_question(
        turn.content,
        state,
        turn.room_name,
        turn.username,
        classifier_model=turn.classifier_model,
        feedback_model=turn.feedback_model,
    )


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
        db.session.add(turn.state)
        db.session.commit()


def _ask_question_again(turn, user_language):
    """Emit the question again (v2.0 with templates)"""
    question_content = render_template(turn.step["question"], _template_context(turn))
    _post_message(
        turn,
        translate_text(question_content, user_language, turn.feedback_model),
        username="System (Question)",
        shown_as="System",
    )


def _drop_tmp_metadata(turn):
    # Check if the activity state still exists before removing temporary metadata
    try:
        # Remove temporary metadata at the end of the turn
        for key in turn.tmp_keys:
            turn.state.remove_metadata(key)

        # Commit the changes after removing temporary metadata
        db.session.add(turn.state)
        db.session.commit()

    except InvalidRequestError:
        # Handle the case where the activity state was deleted
        db.session.rollback()


def _template_context(turn, attempt_offset=0):
    state = turn.state
    return create_template_context(
        metadata=state.dict_metadata,
        current_attempt=state.attempts + attempt_offset,
        max_attempts=state.max_attempts,
        current_section=state.section_id,
        current_step=state.step_id,
        username=turn.username,
    )


def _post_message(turn, content, username="System", shown_as=None):
    """Store a message in the room & emit it (as `shown_as` when the stored
    name differs from the one shown)."""
    new_message = Message(username=username, content=content, room_id=turn.room.id)
    db.session.add(new_message)
    db.session.commit()

    socketio.emit(
        "chat_message",
        {
            "id": new_message.id,
            "username": shown_as or username,
            "content": content,
        },
        room=turn.room_name,
    )
    socketio.sleep(0.1)


def _notice(turn, content, pause=None):
    """Emit a System notice that is never stored."""
    socketio.emit(
        "chat_message",
        {
            "id": notice_id(),
            "username": "System",
            "content": content,
        },
        room=turn.room_name,
    )
    if pause:
        socketio.sleep(pause)


def display_activity_info(room_name, username, model="MODEL_0"):
    with app.app_context():
        room = get_room(room_name)
        activity_state = ActivityState.query.filter_by(room_id=room.id).first()

        if not activity_state:
            socketio.emit(
                "chat_message",
                {
                    "id": notice_id(),
                    "username": "System",
                    "content": "No active activity found.",
                },
                room=room_name,
            )
            return

        # Load the activity content
        activity_content = get_activity_content(activity_state.s3_file_path)

        try:
            # Fetch the entire room history
            all_messages = Message.visible(room.id).order_by(Message.id.asc()).all()
            chat_history = [
                {
                    "role": "system" if msg.username in SYSTEM_USERS else "user",
                    "username": msg.username,
                    "content": msg.content,
                }
                for msg in all_messages
                if not msg.is_base64_image()
            ]

            # Prepare the rubric for grading
            rubric = activity_content.get(
                "tokens_for_ai_rubric",
                """
                Grade the responses of all users based on the following criteria:
                - Accuracy: How correct is the response?
                - Completeness: Does the response fully address the question?
                - Clarity: Is the response clear and easy to understand?
                - Engagement: Is the response engaging and interesting?
                Provide a score out of 10 for each criterion and an overall grade for each user.
                Finally order each user by who is winning. Number of correct answers and accuracy & include an enumeration of the feats!
                Take into account how many attempts the user took to get a passing answer when ranking.
                Don't just try to give the user a "B" or 35/40, really figure out a good placement considering some people don't know how to type.
            """,
            )

            # Generate the grading using the AI
            grading_message = generate_grading(chat_history, rubric, model)

            # Store and emit the activity info
            info_message = f"Activity Info:\nCurrent Section: {activity_state.section_id}\nCurrent Step: {activity_state.step_id}\nAttempts: {activity_state.attempts}\n\n{grading_message}"
            new_message = Message(
                username="System", content=info_message, room_id=room.id
            )
            db.session.add(new_message)
            db.session.commit()

            socketio.emit(
                "chat_message",
                {
                    "id": new_message.id,
                    "username": "System",
                    "content": info_message,
                },
                room=room_name,
            )

        except Exception as e:
            socketio.emit(
                "chat_message",
                {
                    "id": notice_id(),
                    "username": "System",
                    "content": f"Error displaying activity info: {e}",
                },
                room=room_name,
            )
            # Debugging: Log exception
            print(f"Exception: {e}")


def generate_grading(chat_history, rubric, model="MODEL_0"):
    # Use provided model or fall back to default
    if model and model != "None":
        openai_client, model_name = get_openai_client_and_model(model)
    else:
        openai_client, model_name = get_openai_client_and_model()
    messages = [
        {
            "role": "system",
            "content": f"Using the following rubric, grade the responses in the chat history:\n\n{rubric}",
        },
        {
            "role": "user",
            "content": f"Chat History:\n\n{json.dumps(chat_history, indent=2)}",
        },
    ]

    try:
        completion = create_completion_skip_thinking(
            openai_client,
            model=model_name,
            messages=messages,
            max_tokens=1000,
            temperature=0.7,
            n=1,
        )
        grading = strip_reasoning(completion.choices[0].message.content.strip())
        return grading
    except Exception as e:
        return f"Error generating grading: {e}"


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


# Categorize the user's response.
def categorize_response(question, response, buckets, tokens_for_ai, model="MODEL_0"):
    # Use provided model or fall back to default
    if model and model != "None":
        openai_client, model_name = get_openai_client_and_model(model)
    else:
        openai_client, model_name = get_openai_client_and_model()
    messages = _categorize_messages(question, response, buckets, tokens_for_ai)

    simple_format = "BUCKET:" not in messages[0]["content"]

    # A classifier model answers first when one is configured: the step's
    # tokens_for_ai are its instructions, the bucket list its choice set,
    # question + response its state. It fails OPEN onto the chat path: an
    # error leaves a breadcrumb on the readout so a vendor failing over on
    # every call never looks like one that is off.
    _readout.last = None
    _readout.cls_error = None
    if _classifier.available():
        got = _categorize_via_classifier(question, response, buckets, tokens_for_ai)
        if got is not None:
            return got

    try:
        create_kwargs = dict(
            model=model_name,
            messages=messages,
            n=1,
            max_tokens=150,  # Increased for ANALYSIS + BUCKET format
            temperature=0,
        )
        completion = _categorize_completion(openai_client, create_kwargs, simple_format)
        full_response = strip_reasoning(completion.choices[0].message.content.strip())
        print(f"DEBUG BUCKET CATEGORIZATION: Full Hermes response: {full_response}")
        _record_readout(
            first_token_top_logprobs(completion) if simple_format else None, buckets
        )

        category = _category_from_reply(full_response)

        # A verdict is one of the step's buckets or nothing: "Partial
        # Understanding." used to reach the transition lookup as
        # "partial_understanding." & tell the student "Unrecognized category".
        resolved = resolve_bucket(category, buckets)
        if resolved is not None:
            category = (
                resolved["bucket_name"] if isinstance(resolved, dict) else resolved
            )
        readout = last_readout()
        print(
            f"DEBUG BUCKET CATEGORIZATION: Extracted category: {category}"
            + (
                f" (p={readout['p']:.2f} mass={readout['mass']:.2f})"
                if readout and readout.get("p") is not None
                else ""
            )
        )
        return category
    except Exception as e:
        return f"Error: {e}"


def _categorize_messages(question, response, buckets, tokens_for_ai):
    bucket_list = ", ".join([str(bucket) for bucket in buckets])
    # Check if tokens_for_ai already includes format instructions (ANALYSIS/BUCKET format)
    if "ANALYSIS:" in tokens_for_ai and "BUCKET:" in tokens_for_ai:
        # YAML already specifies output format, don't override
        system_content = f"{tokens_for_ai}"
        user_content = f"Question: {question}\nResponse: {response}"
    else:
        # Use old simple format for backwards compatibility
        system_content = f"{tokens_for_ai} Categorize the following response into one of the following buckets: {bucket_list}. Return ONLY a bucket label."
        user_content = f"Question: {question}\nResponse: {response}\n\nCategory:"

    return [
        {
            "role": "system",
            "content": system_content,
        },
        {
            "role": "user",
            "content": user_content,
        },
    ]


def _categorize_completion(openai_client, create_kwargs, simple_format):
    if not simple_format:
        return create_completion_skip_thinking(openai_client, **create_kwargs)
    # The simple format answers with the bucket name first, so the
    # first token's top_logprobs are a distribution over the buckets:
    # a confidence readout beside the verdict (see last_readout).
    # Retried without when a hosted provider rejects the fields.
    try:
        return create_completion_skip_thinking(
            openai_client, logprobs=True, top_logprobs=20, **create_kwargs
        )
    except Exception as e:
        if "logprob" not in str(e).lower():
            raise
        return create_completion_skip_thinking(openai_client, **create_kwargs)


def _category_from_reply(full_response):
    """Handle both ANALYSIS/BUCKET format and simple bucket response"""
    if "BUCKET:" in full_response:
        # New ANALYSIS/BUCKET format
        bucket_lines = [line for line in full_response.split("\n") if "BUCKET:" in line]
        if bucket_lines:
            return bucket_lines[0].split("BUCKET:")[1].strip().lower().replace(" ", "_")
    elif "ANALYSIS:" in full_response:
        # Has analysis but no explicit BUCKET: line, try to extract from end
        lines = [line.strip() for line in full_response.split("\n") if line.strip()]
        if lines:
            return lines[-1].lower().replace(" ", "_")
    # Simple bucket response (old format)
    return full_response.lower().replace(" ", "_")


# Per-thread confidence readout of the last categorize_response call. Socket
# handlers run concurrently, so a module global would interleave rooms.
_readout = threading.local()


def _categorize_via_classifier(question, response, buckets, tokens_for_ai):
    """Route one categorization to a classifier model (`classifier.py`).
    Returns the bucket name, or None so the chat path runs exactly as
    before (fail-open) with the failure parked on the readout."""
    try:
        d = _classifier.categorize(question, response, buckets, tokens_for_ai)
    except _classifier.ClassifierError as exc:
        _readout.cls_error = str(exc)[:200]
        return None
    except Exception as exc:  # noqa: BLE001  a defect in the bridge, not the vendor
        _readout.cls_error = f"{type(exc).__name__}: {exc}"[:200]
        return None
    _readout.last = {
        "p": d["p"],
        "argmax": d["value"],
        "mass": 1.0,
        "dist": {b: round(v, 3) for b, v in d["dist"].items() if v},
        "source": _classifier.SOURCE,
        "confidence": d["confidence"],
        "instance": d["instance"],
        "model": d["model"],
    }
    print(
        f"DEBUG BUCKET CATEGORIZATION: classifier {d['instance']} ({d['model']})"
        f" -> {d['value']} (p={d['p']:.2f} confidence={d['confidence']})"
    )
    return d["value"]


def _record_readout(top, buckets):
    cls_error = getattr(_readout, "cls_error", None)
    if not top:
        _readout.last = (
            {"p": None, "source": "chat", "classifier_error": cls_error}
            if cls_error
            else None
        )
        return
    dist, mass = fold_first_token_logprobs(top, buckets)
    best = max(dist, key=dist.get) if dist else None
    _readout.last = {
        "p": round(dist[best], 4) if best and mass else None,
        "argmax": best if mass else None,
        "mass": round(mass, 4),
        "dist": {k: round(v, 3) for k, v in dist.items() if v},
        "source": "chat",
    }
    if cls_error:
        _readout.last["classifier_error"] = cls_error


def last_readout():
    """{p, argmax, mass, dist, source} of this thread's last
    categorize_response, or None when the provider returned no logprobs
    (ANALYSIS/BUCKET format, hosted providers that reject the field).
    `source` is `classifier` when a decision model answered (then
    `confidence`, `instance` & `model` ride along) or `chat`; a
    `classifier_error` breadcrumb names why a configured classifier did not
    decide. p is the probability the model put on its verdict. Measured
    2026-09-16 on Qwen3.6-27B in unhomeschool (same lesson shape, 239 labeled
    evals): chat verdicts correct 25% / 31% / 50% / 77% across p bands <.5 /
    .5-.7 / .7-.9 / >=.9. A readout an activity author reads, never a
    grade."""
    return getattr(_readout, "last", None)


# Generate AI feedback
def generate_ai_feedback(
    category,
    question,
    user_response,
    tokens_for_ai,
    username,
    json_metadata,
    json_new_metadata,
    model="MODEL_0",
):
    # Use provided model or fall back to default
    if model and model != "None":
        openai_client, model_name = get_openai_client_and_model(model)
    else:
        openai_client, model_name = get_openai_client_and_model()
    messages = [
        {
            "role": "system",
            "content": f"{tokens_for_ai} Generate a human-readable feedback message based on the following:",
        },
        {
            "role": "user",
            "content": f"Username: {username}\nQuestion: {question}\nResponse: {user_response}\nCategory: {category}\nMetadata: {json_metadata}\n New Metadata: {json_new_metadata}",
        },
    ]

    try:
        completion = create_completion_skip_thinking(
            openai_client,
            model=model_name,
            messages=messages,
            max_tokens=1000,
            temperature=0.7,
            n=1,
        )
        feedback = strip_reasoning(completion.choices[0].message.content.strip())
        return feedback
    except Exception as e:
        return f"Error: {e}"


def provide_feedback(
    transition,
    category,
    question,
    tokens_for_ai,
    user_response,
    user_language,
    username,
    json_metadata,
    json_new_metadata,
    model="MODEL_0",
):
    feedback = ""
    if "ai_feedback" in transition:
        tokens_for_ai += f" You must provide the feedback in the user's language: {user_language}. {transition['ai_feedback'].get('tokens_for_ai', '')}."
        ai_feedback = generate_ai_feedback(
            category,
            question,
            user_response,
            tokens_for_ai,
            username,
            json_metadata,
            json_new_metadata,
            model,
        )
        feedback += f"\n\n{ai_feedback}"

    return feedback


def provide_feedback_prompts(
    transition,
    category,
    question,
    feedback_prompts,
    user_response,
    user_language,
    username,
    json_metadata,
    json_new_metadata,
    legacy_tokens_for_ai="",
    model="MODEL_0",
):
    """Generate feedback from multiple prompts"""
    feedback_messages = []

    # Parse full metadata once for filtering
    full_metadata = json.loads(json_metadata)

    # Add user_response to metadata for filtering purposes
    full_metadata["user_response"] = user_response

    for prompt in feedback_prompts:
        prompt_name = prompt.get("name", "unnamed")

        # Apply per-prompt metadata filtering if specified
        prompt_metadata = full_metadata
        if "metadata_filter" in prompt:
            filter_keys = prompt["metadata_filter"]
            prompt_metadata = {
                k: v for k, v in full_metadata.items() if k in filter_keys
            }

            # Check skip condition if specified
            skip_condition = prompt.get("skip_condition")
            if skip_condition and _skip_condition_met(
                skip_condition, list(prompt_metadata.values())
            ):
                print(
                    f"DEBUG: Skipping prompt '{prompt_name}' - skip_condition '{skip_condition}' met"
                )
                continue

        _debug_battleship_prompt(prompt, prompt_name, prompt_metadata)

        tokens_for_ai = _feedback_prompt_tokens(
            prompt, transition, user_language, legacy_tokens_for_ai
        )

        # Determine user_response for this prompt based on metadata filtering
        filtered_user_response = user_response
        if (
            "metadata_filter" in prompt
            and "user_response" not in prompt["metadata_filter"]
        ):
            filtered_user_response = ""  # Remove user response if not in filter

        ai_feedback = generate_ai_feedback(
            category,
            question,
            filtered_user_response,
            tokens_for_ai,
            username,
            json.dumps(prompt_metadata),  # Use filtered metadata for this prompt
            json_new_metadata,
            model,
        )

        # Only add feedback if it has content
        if ai_feedback and ai_feedback.strip():
            feedback_messages.append(
                {"name": prompt_name, "content": ai_feedback.strip()}
            )

    return feedback_messages


def _skip_condition_met(skip_condition, values):
    """all_null / all_false / all_true over a prompt's filtered values; any
    other condition never skips."""
    if skip_condition == "all_null":
        return all(value is None or value == "" or value == "None" for value in values)
    if skip_condition == "all_false":
        return all(value is False or value == "False" for value in values)
    if skip_condition == "all_true":
        return all(value is True or value == "True" for value in values)
    return False


def _feedback_prompt_tokens(prompt, transition, user_language, legacy_tokens_for_ai):
    tokens_for_ai = prompt.get("tokens_for_ai", "")

    # Combine legacy tokens with prompt-specific tokens
    if legacy_tokens_for_ai:
        tokens_for_ai = legacy_tokens_for_ai + " " + tokens_for_ai

    # Add language instruction
    tokens_for_ai += (
        f" You must provide the feedback in the user's language: {user_language}."
    )

    # Add transition-specific machine learning feedback if present
    if "ai_feedback" in transition:
        tokens_for_ai += f" {transition['ai_feedback'].get('tokens_for_ai', '')}"

    return tokens_for_ai


def _debug_battleship_prompt(prompt, prompt_name, prompt_metadata):
    """Special debug for Ship Status and Game Over"""
    if "metadata_filter" not in prompt:
        if prompt_name == "Ship Status":
            print(
                f"DEBUG SHIP STATUS - NO metadata_filter, full metadata: {prompt_metadata}"
            )
        elif prompt_name == "Game Over":
            print(
                f"DEBUG GAME OVER - NO metadata_filter, full metadata: {prompt_metadata}"
            )
        return

    filter_keys = prompt["metadata_filter"]
    if prompt_name == "Ship Status":
        print(f"DEBUG SHIP STATUS - filter_keys: {filter_keys}")
        print(f"DEBUG SHIP STATUS - filtered metadata: {prompt_metadata}")
        print(
            f"DEBUG SHIP STATUS - user_sunk_ship_this_round = '{prompt_metadata.get('user_sunk_ship_this_round')}'"
        )
        print(
            f"DEBUG SHIP STATUS - ai_sunk_ship_this_round = '{prompt_metadata.get('ai_sunk_ship_this_round')}'"
        )
    elif prompt_name == "Game Over":
        print(f"DEBUG GAME OVER - filter_keys: {filter_keys}")
        print(f"DEBUG GAME OVER - filtered metadata: {prompt_metadata}")
        print(f"DEBUG GAME OVER - game_over = '{prompt_metadata.get('game_over')}'")
        print(f"DEBUG GAME OVER - user_wins = '{prompt_metadata.get('user_wins')}'")
        print(f"DEBUG GAME OVER - ai_wins = '{prompt_metadata.get('ai_wins')}'")


def translate_text(text, target_language, model="MODEL_0"):
    # Guard clause for default language
    target_language = target_language.lower().split()

    if "english" in target_language:
        return text

    # Use provided model or fall back to default
    if model and model != "None":
        openai_client, model_name = get_openai_client_and_model(model)
    else:
        openai_client, model_name = get_openai_client_and_model()
    messages = [
        {
            "role": "system",
            "content": f"Translate the following text to {target_language}. DO NOT add anything else extra to your translation. It should be as close to word for word the dame but translated. Don't start with 'Set_language:' DO NOT try to solve math questions, translate the text around it and use mathmatical notation like normal.",
        },
        {
            "role": "user",
            "content": text,
        },
    ]

    try:
        completion = create_completion_skip_thinking(
            openai_client,
            model=model_name,
            messages=messages,
            max_tokens=2000,
            temperature=0.7,
            n=1,
        )
        translation = strip_reasoning(completion.choices[0].message.content.strip())
        return translation
    except Exception as e:
        return f"Error: {e}"


def init_activity_module(
    app_instance, socketio_instance, db_instance, helper_functions
):
    """Initialize the activity module with dependencies from app.py"""
    global app, socketio, db
    global get_room, get_s3_client, get_openai_client_and_model
    global SYSTEM_USERS

    app = app_instance
    socketio = socketio_instance
    db = db_instance

    get_room = helper_functions["get_room"]
    get_s3_client = helper_functions["get_s3_client"]
    get_openai_client_and_model = helper_functions["get_openai_client_and_model"]
    SYSTEM_USERS = helper_functions["SYSTEM_USERS"]
