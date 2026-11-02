import argparse
import yaml
import json
import os
import sys
from openai import OpenAI

# Add parent directory to path to import activity_utils
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import classifier
import activity_engine

from activity_utils import create_completion_skip_thinking, strip_reasoning

# Global model-client mapping
MODEL_CLIENT_MAP = {}

# Numbers of every configured MODEL_ENDPOINT_x, in order; used as the
# fallback chain when a requested endpoint is down or unconfigured.
CONFIGURED_MODEL_NUMS = [
    str(i) for i in range(1000) if os.getenv(f"MODEL_ENDPOINT_{i}")
]

# Switch mains by setting DEFAULT_MODEL (e.g. DEFAULT_MODEL=MODEL_2).
DEFAULT_MODEL = os.getenv(
    "DEFAULT_MODEL",
    f"MODEL_{CONFIGURED_MODEL_NUMS[0]}" if CONFIGURED_MODEL_NUMS else "MODEL_1",
)


def get_client_for_endpoint(endpoint, api_key):
    """Create OpenAI client for any endpoint"""
    return OpenAI(api_key=api_key, base_url=endpoint)


# Endpoint health cache: base_url -> healthy. The simulator is a single
# short-lived process, so one probe per endpoint per run is enough.
_endpoint_health = {}


def endpoint_is_healthy(client, base_url):
    if base_url in _endpoint_health:
        return _endpoint_health[base_url]
    try:
        client.with_options(timeout=5.0).models.list()
        healthy = True
    except Exception as e:
        print(f"[WARN] Endpoint {base_url} failed health check: {e}")
        healthy = False
    _endpoint_health[base_url] = healthy
    return healthy


def initialize_model_map():
    """Initialize the model-client mapping from environment variables"""
    # Load endpoints from environment variables
    for i in range(1000):  # Support up to 1000 endpoints
        endpoint_key = f"MODEL_ENDPOINT_{i}"
        api_key_key = f"MODEL_API_KEY_{i}"

        endpoint = os.getenv(endpoint_key)
        api_key = os.getenv(api_key_key)

        if endpoint and api_key:
            try:
                client = get_client_for_endpoint(endpoint, api_key)
                # Query endpoint for available models
                try:
                    response = client.models.list()
                    model_list = response.data
                    print(
                        f"[DEBUG] {endpoint} returned models: {[m.id for m in model_list]}"
                    )
                    for m in model_list:
                        model_id = m.id
                        if model_id and model_id not in MODEL_CLIENT_MAP:
                            MODEL_CLIENT_MAP[model_id] = (client, endpoint)
                except Exception as e:
                    print(
                        f"Warning: Could not list models for endpoint '{endpoint}': {e}"
                    )
            except Exception as e:
                print(f"Warning: Failed to initialize endpoint {endpoint}: {e}")


def _resolve_model_num(model_num, check_health=True):
    """Resolve MODEL_<num> env config to (client, model_name), or None."""
    endpoint = os.getenv(f"MODEL_ENDPOINT_{model_num}")
    api_key = os.getenv(f"MODEL_API_KEY_{model_num}")
    if not endpoint or not api_key:
        return None
    client = get_client_for_endpoint(endpoint, api_key)
    if check_health and not endpoint_is_healthy(client, endpoint):
        return None

    # Explicit MODEL_NAME_X wins: endpoints like Gemini list dozens of
    # models (some retired) and "first listed" picks wrong.
    explicit_model = os.getenv(f"MODEL_NAME_{model_num}")
    if explicit_model:
        return client, explicit_model

    for model_id, (registered_client, base_url) in MODEL_CLIENT_MAP.items():
        if base_url == endpoint:
            return client, model_id

    # Fallback: query endpoint for models if not in map yet
    try:
        response = client.with_options(timeout=10.0).models.list()
        if response.data:
            actual_model = response.data[0].id
            print(f"[DEBUG] Using first model from {endpoint}: {actual_model}")
            return client, actual_model
    except Exception as e:
        print(f"Warning: Could not query models from {endpoint}: {e}")
        return None

    print(f"Warning: No models found for {endpoint}, using 'model' as fallback")
    return client, "model"


def get_openai_client_and_model(model_name=None):
    """Get OpenAI client and model name

    Supports both direct model names and MODEL_X environment variable
    references. A MODEL_X whose endpoint is down or unconfigured falls
    through to the next healthy configured endpoint. Set DEFAULT_MODEL to
    change which reference is main.
    """
    if not model_name:
        model_name = DEFAULT_MODEL

    # Handle MODEL_X references with health-aware fallback
    if model_name.startswith("MODEL_"):
        requested = model_name.split("_")[1]
        candidates = [requested] + [n for n in CONFIGURED_MODEL_NUMS if n != requested]
        for num in candidates:
            resolved = _resolve_model_num(num)
            if resolved:
                if num != requested:
                    print(
                        f"[WARN] MODEL_{requested} unavailable; "
                        f"falling back to MODEL_{num} ({resolved[1]})"
                    )
                return resolved
        # Nothing healthy: build the first configured client anyway so the
        # failure surfaces as a real completion error instead of a crash.
        for num in candidates:
            resolved = _resolve_model_num(num, check_health=False)
            if resolved:
                return resolved
        print(f"Warning: no MODEL_x endpoints configured for {model_name}")
        return None, model_name

    # Try to find client for specific model name
    for stored_model, (client, base_url) in MODEL_CLIENT_MAP.items():
        if model_name in stored_model or stored_model == model_name:
            return client, model_name

    # Fallback to first available client
    if MODEL_CLIENT_MAP:
        client, _ = next(iter(MODEL_CLIENT_MAP.values()))
        return client, model_name

    # Final fallback to environment or default OpenAI
    api_key = os.getenv("OPENAI_API_KEY", "dummy-key")
    endpoint = os.getenv("MODEL_ENDPOINT_0", "https://api.openai.com/v1")

    client = get_client_for_endpoint(endpoint, api_key)
    return client, model_name


# Initialize the model mapping on startup
initialize_model_map()


# Load the YAML activity file
def load_yaml_activity(file_path):
    with open(file_path, "r") as file:
        return yaml.safe_load(file)


# Categorize the user's response
def categorize_response(question, response, buckets, tokens_for_ai, model="MODEL_1"):
    bucket_list = ", ".join([str(bucket) for bucket in buckets])
    messages = [
        {
            "role": "system",
            "content": f"{tokens_for_ai} Categorize the following response into one of the following buckets: {bucket_list}. Return ONLY a bucket label.",
        },
        {
            "role": "user",
            "content": f"Question: {question}\nResponse: {response}\n\nCategory:",
        },
    ]

    # A classifier model (classifier.py) answers first when one is configured
    # & fails open onto the chat path below, exactly as activity.py does.
    if classifier.available():
        try:
            return classifier.categorize(question, response, buckets, tokens_for_ai)[
                "value"
            ]
        except classifier.ClassifierError as exc:
            print(f"classifier: {exc}; chat decides", file=sys.stderr)

    try:
        client, model_name = get_openai_client_and_model(model)
        completion = create_completion_skip_thinking(
            client,
            model=model_name,
            messages=messages,
            max_tokens=5,
            temperature=0,
        )
        category = (
            strip_reasoning(completion.choices[0].message.content.strip())
            .lower()
            .replace(" ", "_")
        )
        return category
    except Exception as e:
        return f"Error: {e}"


# Generate AI feedback
def generate_ai_feedback(
    category, question, user_response, tokens_for_ai, metadata, model="MODEL_1"
):
    messages = [
        {
            "role": "system",
            "content": f"{tokens_for_ai} Generate a human-readable feedback message based on the following:",
        },
        {
            "role": "user",
            "content": f"Question: {question}\nResponse: {user_response}\nCategory: {category},\nMetadata: {metadata}",
        },
    ]

    try:
        client, model_name = get_openai_client_and_model(model)
        completion = create_completion_skip_thinking(
            client, model=model_name, messages=messages, max_tokens=250, temperature=0.7
        )
        feedback = strip_reasoning(completion.choices[0].message.content.strip())
        return feedback
    except Exception as e:
        return f"Error: {e}"


# Provide feedback based on the category (legacy single feedback system)
def provide_feedback(
    transition,
    category,
    question,
    user_response,
    user_language,
    tokens_for_ai,
    metadata,
    model="MODEL_1",
):
    feedback = ""
    if "ai_feedback" in transition:
        tokens_for_ai += f" Provide the feedback in {user_language}. {transition['ai_feedback'].get('tokens_for_ai', '')}."

        # Filter metadata for feedback if metadata_feedback_filter is specified
        feedback_metadata = metadata
        if "metadata_feedback_filter" in transition:
            filter_keys = transition["metadata_feedback_filter"]
            feedback_metadata = {k: v for k, v in metadata.items() if k in filter_keys}

        ai_feedback = generate_ai_feedback(
            category, question, user_response, tokens_for_ai, feedback_metadata, model
        )
        feedback += f"\n\nAI Feedback: {ai_feedback}"

    return feedback


# Provide feedback using multiple prompts (new system)
def provide_feedback_prompts(
    transition,
    category,
    question,
    feedback_prompts,
    user_response,
    user_language,
    metadata,
    legacy_tokens_for_ai="",
    model="MODEL_1",
):
    """Generate feedback from multiple prompts"""
    feedback_messages = []

    # Add user_response to metadata for filtering purposes
    full_metadata = metadata.copy()
    full_metadata["user_response"] = user_response

    for prompt in feedback_prompts:
        prompt_name = prompt.get("name", "unnamed")
        tokens_for_ai = prompt.get("tokens_for_ai", "")

        # Apply per-prompt metadata filtering if specified
        prompt_metadata = full_metadata
        if "metadata_filter" in prompt:
            filter_keys = prompt["metadata_filter"]
            prompt_metadata = {
                k: v for k, v in full_metadata.items() if k in filter_keys
            }

        # Combine legacy tokens with prompt-specific tokens
        if legacy_tokens_for_ai:
            tokens_for_ai = legacy_tokens_for_ai + " " + tokens_for_ai

        # Add language instruction
        tokens_for_ai += f" Provide the feedback in {user_language}."

        # Add transition-specific AI feedback if present
        if "ai_feedback" in transition:
            tokens_for_ai += f" {transition['ai_feedback'].get('tokens_for_ai', '')}"

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
            prompt_metadata,
            model,
        )

        # Only add feedback if it has content and isn't exactly the STFU token
        if ai_feedback and ai_feedback.strip() and ai_feedback.strip() != "STFU":
            feedback_messages.append(
                {"name": prompt_name, "content": ai_feedback.strip()}
            )

    return feedback_messages


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


def get_next_section_and_step(activity_content, current_section_id, current_step_id):
    for section in activity_content["sections"]:
        if section["section_id"] == current_section_id:
            for i, step in enumerate(section["steps"]):
                if step["step_id"] == current_step_id:
                    if i + 1 < len(section["steps"]):
                        return section["section_id"], section["steps"][i + 1]["step_id"]
                    else:
                        # Move to the next section
                        next_section_index = (
                            activity_content["sections"].index(section) + 1
                        )
                        if next_section_index < len(activity_content["sections"]):
                            next_section = activity_content["sections"][
                                next_section_index
                            ]
                            return (
                                next_section["section_id"],
                                next_section["steps"][0]["step_id"],
                            )
    return None, None


def translate_text(text, target_language, model="MODEL_1"):
    # Guard clause for default language
    if target_language.lower() == "english":
        return text

    messages = [
        {
            "role": "system",
            "content": f"Translate the following text to {target_language}:",
        },
        {
            "role": "user",
            "content": text,
        },
    ]

    try:
        client, model_name = get_openai_client_and_model(model)
        completion = create_completion_skip_thinking(
            client, model=model_name, messages=messages, max_tokens=500, temperature=0.7
        )
        translation = strip_reasoning(completion.choices[0].message.content.strip())
        return translation
    except Exception as e:
        return f"Error: {e}"


class TerminalIO(activity_engine.ActivityIO):
    """Our activity engine (activity_engine.py, the one our server runs) in
    a terminal: everything printed, the state in memory. Calls go through
    this module's names (categorize_response, translate_text…) so a test
    that replaces one here reaches the engine."""

    def say(self, content, username="System", shown_as=None):
        if username == "System (Question)":
            print(f"\nQuestion: {content}")
        elif username.startswith("System (") and username.endswith(")"):
            print(f"\n{username[len('System ('):-1]}: {content}")
        else:
            print(content)

    def notice(self, content, pause=None):
        print(content)

    def categorize(self, question, response, buckets, tokens_for_ai, model):
        return categorize_response(question, response, buckets, tokens_for_ai, model)

    def translate(self, text, language, model):
        return translate_text(text, language, model)

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
        return provide_feedback(
            transition,
            bucket,
            question,
            response,
            language,
            tokens_for_ai,
            json.loads(json_metadata),
            model,
        )

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
        return provide_feedback_prompts(
            transition,
            bucket,
            question,
            prompts,
            response,
            language,
            json.loads(json_metadata),
            legacy_tokens,
            model,
        )

    def run_script(self, metadata, script):
        return execute_processing_script(metadata, script)

    def complete(self, state, username, feedback_model):
        state.completed = True
        print("\nActivity completed!")


def simulate_activity(yaml_file_path):
    """Play an activity in this terminal: each answer typed goes through our
    activity engine exactly as a chat room's would."""
    content = load_yaml_activity(yaml_file_path)
    first = content["sections"][0]
    state = activity_engine.MemoryState(
        first["section_id"],
        first["steps"][0]["step_id"],
        max_attempts=content.get("default_max_attempts_per_step", 3),
        metadata={"language": "English"},
    )
    io = TerminalIO()
    # Default to MODEL_1 (Hermes) here
    _, feedback_model = activity_engine.models_for(content, "MODEL_1")
    activity_engine.show_until_question(content, state, io, "User", feedback_model)
    while not state.completed and activity_engine.current_step(content, state):
        print(
            f"\n\nCurrent section: {state.section_id}, Current step: {state.step_id}\n\n"
        )
        user_response = input("\nYour Response: ")
        activity_engine.answer(
            content, state, user_response, "User", io, default_model="MODEL_1"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Simulate an activity.")
    parser.add_argument(
        "yaml_file_path",
        type=str,
        help="Path to the activity YAML file",
        default="activity0.yaml",
    )
    args = parser.parse_args()
    simulate_activity(args.yaml_file_path)
