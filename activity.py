import json
import yaml
import os
import threading
import random  # tests replace activity.random.random / .choice
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
    evaluate_condition,
    create_completion_skip_thinking,
    strip_reasoning,
    resolve_bucket,
    fold_first_token_logprobs,
    first_token_top_logprobs,
)

# Our activity engine: one learner's answer & the walk to the next question,
# shared with research/guarded_ai.py; RoomIO below runs it in a chat room.
import activity_engine

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
    """Say the room's current step & go on to its next question (or the
    end): activity_engine.show_until_question, in this room."""
    activity_engine.show_until_question(
        activity_content,
        activity_state,
        RoomIO(get_room(room_name), room_name),
        username,
        feedback_model,
    )


def complete_activity(activity_state, room_name, username, feedback_model="MODEL_0"):
    """Activity completed: its info, then its state goes & the room returns
    to the activity chooser."""
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

        try:
            # Default to MODEL_0 (Hermes) - fast, accurate, & always available
            activity_engine.answer(
                activity_content,
                activity_state,
                user_response,
                username,
                RoomIO(room, room_name),
                default_model="MODEL_0",
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


class RoomIO(activity_engine.ActivityIO):
    """Our activity engine in a chat room: messages stored in our database &
    emitted to the room, state kept in ActivityState. Every call goes
    through this module's names (categorize_response, provide_feedback…)
    so tests & callers that replace them here still reach the engine."""

    def __init__(self, room, room_name):
        self.room = room
        self.room_name = room_name

    def say(self, content, username="System", shown_as=None):
        new_message = Message(username=username, content=content, room_id=self.room.id)
        db.session.add(new_message)
        db.session.commit()
        socketio.emit(
            "chat_message",
            {
                "id": new_message.id,
                "username": shown_as or username,
                "content": content,
            },
            room=self.room_name,
        )
        socketio.sleep(0.1)

    def notice(self, content, pause=None):
        socketio.emit(
            "chat_message",
            {"id": notice_id(), "username": "System", "content": content},
            room=self.room_name,
        )
        if pause:
            socketio.sleep(pause)

    def save(self, state, gone_ok=False):
        try:
            db.session.add(state)
            db.session.commit()
        except InvalidRequestError:
            if not gone_ok:
                raise
            # The activity state was deleted (it completed)
            db.session.rollback()

    def categorize(self, question, response, buckets, tokens_for_ai, model):
        return categorize_response(question, response, buckets, tokens_for_ai, model)

    def readout(self):
        return last_readout()

    def translate(self, text, language, model):
        return translate_text(text, language, model)

    def feedback(self, *args):
        return provide_feedback(*args)

    def feedback_prompts(self, *args):
        return provide_feedback_prompts(*args)

    def run_script(self, metadata, script):
        return execute_processing_script(metadata, script)

    def set_background(self, image_base64):
        socketio.emit(
            "set_background", {"image_data": image_base64}, room=self.room_name
        )
        socketio.sleep(0.1)

    def offer_choices(self, offer):
        # Ids & texts only (activity_grading.choices_for_page): the answer
        # key stays on our server, which grades.
        socketio.emit("activity_choices", offer, room=self.room_name)

    def complete(self, state, username, feedback_model):
        complete_activity(state, self.room_name, username, feedback_model)


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


get_next_step = activity_engine.get_next_step


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
    if "english" in target_language.lower().split():
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
