"""Code execution through Unsandbox (credentials stay server-side), plus
machine learning helpers that fix code & name artifacts."""

import os

import requests
from flask import Blueprint, jsonify, request

import un
from activity_utils import create_completion_skip_thinking, strip_reasoning
from routes import DEPS

bp = Blueprint("code", __name__)


@bp.route("/api/generate-artifact-name", methods=["POST"])
def generate_artifact_name():
    """Generate a meaningful filename for an artifact using AI.

    Returns a 1-3 word filename with underscores based on what the code does.
    Respects ENABLE_CODE_GEN_FILENAMES environment variable (enabled by default).
    """
    # Check if feature is enabled (default: true)
    enabled = os.environ.get("ENABLE_CODE_GEN_FILENAMES", "true").lower() == "true"
    if not enabled:
        return jsonify({"filename": "compiled_binary"})

    try:
        data = request.get_json()
        code = data.get("code", "")
        language = data.get("language", "")

        if not code:
            return jsonify({"filename": "compiled_binary"})

        # Use MODEL_1 (Hermes) to generate filename
        client, model = DEPS["get_openai_client_and_model"]("MODEL_1")

        system_prompt = """You are a filename generator. Given code, generate a SHORT, descriptive filename that represents what the code does.

Rules:
- Output ONLY the filename, nothing else
- Use 1-3 words maximum
- Use lowercase with underscores between words (e.g., "fizzbuzz" or "hello_world" or "prime_checker")
- NO file extension
- NO explanations or commentary
- Be specific about what the code does

Examples:
- Code that prints "Hello World" → "hello_world"
- Code that checks for prime numbers → "prime_checker"
- Code that plays FizzBuzz → "fizzbuzz"
- Code that sorts an array → "array_sort"
- Code that calculates factorial → "factorial"
"""

        user_prompt = f"Language: {language}\n\nCode:\n{code}\n\nGenerate filename:"

        response = create_completion_skip_thinking(
            client,
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.3,
            max_tokens=20,
        )

        filename = strip_reasoning(response.choices[0].message.content.strip())

        # Clean up the filename (remove quotes, extensions, whitespace)
        filename = filename.strip("\"'")
        filename = filename.split(".")[0]  # Remove any extension
        filename = filename.replace(" ", "_")
        filename = filename.lower()

        # Validate filename (alphanumeric and underscores only)
        import re

        if not re.match(r"^[a-z0-9_]+$", filename):
            filename = "compiled_binary"

        # Ensure it's not too long (max 50 chars)
        if len(filename) > 50:
            filename = filename[:50]

        return jsonify({"filename": filename})

    except Exception as e:
        print(f"Error generating artifact name: {e}")
        return jsonify({"filename": "compiled_binary"})


def _unsandbox_error_response(e, log_label):
    """Turn an Unsandbox SDK exception into an informative JSON error.

    Surfaces the real upstream HTTP status + body when available instead of a
    flat 500, so failures are debuggable from the browser console and logs.
    Never serializes credentials: public/secret keys live in request headers,
    never in response bodies, so echoing the upstream body is safe.
    """
    import traceback

    if isinstance(e, requests.exceptions.HTTPError) and e.response is not None:
        status = e.response.status_code
        body = (e.response.text or "")[:2000]
        print(f"{log_label}: HTTP {status} from Unsandbox: {body}")
        traceback.print_exc()
        out_status = status if 400 <= status < 600 else 502
        return (
            jsonify(
                {
                    "error": "Unsandbox upstream error",
                    "upstream_status": status,
                    "upstream_body": body,
                }
            ),
            out_status,
        )

    print(f"{log_label}: {e}")
    traceback.print_exc()
    return jsonify({"error": f"{log_label}: {e}"}), 500


# Unsandbox API proxy endpoints - keeps API keys server-side
@bp.route("/api/code/execute", methods=["POST"])
def proxy_code_execute():
    """Proxy code execution requests to Unsandbox API.

    Keeps UNSANDBOX_PUBLIC_KEY and UNSANDBOX_SECRET_KEY secure on the server side.
    Uses official Unsandbox Python SDK for authentication and execution.
    Supports artifacts parameter for compiled binaries, images, etc.
    """
    try:
        data = request.get_json(silent=True)
        if not data:
            return jsonify({"error": "Request body required"}), 400

        # Check if credentials are configured
        public_key = os.environ.get("UNSANDBOX_PUBLIC_KEY")
        secret_key = os.environ.get("UNSANDBOX_SECRET_KEY")
        if not public_key or not secret_key:
            return jsonify({"error": "Code execution not configured"}), 503

        # Extract parameters from request
        language = data.get("language")
        code = data.get("code")

        if not language or not code:
            return jsonify({"error": "Language and code are required"}), 400

        # Build request body with all supported parameters
        request_body = {
            "language": language,
            "code": code,
            "return_artifact": True,
        }

        # Use SDK's internal _make_request for full parameter support
        result = un._make_request(
            "POST", "/execute", public_key, secret_key, request_body
        )

        # Return job_id from response
        return jsonify({"job_id": result.get("job_id")}), 200

    except Exception as e:
        return _unsandbox_error_response(e, "Error proxying code execution")


@bp.route("/api/code/jobs/<job_id>", methods=["GET"])
def proxy_job_status(job_id):
    """Proxy job status requests to Unsandbox API using SDK."""
    try:
        # Check if credentials are configured
        if not os.environ.get("UNSANDBOX_PUBLIC_KEY") or not os.environ.get(
            "UNSANDBOX_SECRET_KEY"
        ):
            return jsonify({"error": "Code execution not configured"}), 503

        # Use SDK's get_job method
        result = un.get_job(job_id)

        # SDK returns job status dict
        return jsonify(result), 200

    except Exception as e:
        return _unsandbox_error_response(e, "Error fetching job status")


@bp.route("/api/code/jobs/<job_id>", methods=["DELETE"])
def proxy_job_cancel(job_id):
    """Proxy job cancellation requests to Unsandbox API using SDK."""
    try:
        # Check if credentials are configured
        if not os.environ.get("UNSANDBOX_PUBLIC_KEY") or not os.environ.get(
            "UNSANDBOX_SECRET_KEY"
        ):
            return jsonify({"error": "Code execution not configured"}), 503

        # Use SDK's cancel_job method
        result = un.cancel_job(job_id)

        # SDK returns success status
        return jsonify(result), 200

    except Exception as e:
        return _unsandbox_error_response(e, "Error cancelling job")


@bp.route("/api/fix-code", methods=["POST"])
def fix_code():
    """Auto-fix code errors by asking AI to fix issues based on stderr output.

    Accepts code, language, stderr, and attempt number.
    Returns fixed code block or error message.
    """
    try:
        data = request.get_json()
        code = data.get("code", "")
        language = data.get("language", "")
        stderr = data.get("stderr", "")
        exit_code = data.get("exit_code", 1)
        attempt = data.get("attempt", 1)

        if not code or not language:
            return jsonify({"error": "Code and language are required"}), 400

        # Try MODEL_3 (Qwen Coder) first for better code fixing, fall back to MODEL_1 (Hermes)
        try:
            client, model = DEPS["get_openai_client_and_model"]("MODEL_3")
            print(f"[INFO] Using MODEL_3 for code fixing: {model}")
        except Exception as e:
            print(f"[WARN] MODEL_3 not available, falling back to MODEL_1: {e}")
            client, model = DEPS["get_openai_client_and_model"]("MODEL_1")
            print(f"[INFO] Using MODEL_1 for code fixing: {model}")

        system_prompt = f"""You are an expert {language} programmer and debugger. Your task is to fix code that has errors.

CRITICAL RULES:
- Output ONLY the fixed code, nothing else
- NO explanations, NO comments about what you changed
- NO markdown code fences (```), just the raw code
- Preserve the original code structure and logic as much as possible
- Fix ONLY the errors reported in stderr
- If the error mentions missing imports/includes, add them at the top
- If the error is a syntax error, fix the syntax
- Keep the same variable names and overall approach

The code should be immediately executable without any modifications."""

        user_prompt = f"""The following {language} code has errors:

```{language}
{code}
```

Error output (exit code {exit_code}):
```
{stderr}
```

Fix the code (output ONLY the corrected code, no explanations):"""

        response = create_completion_skip_thinking(
            client,
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.2,  # Low temperature for consistent fixes
            max_tokens=2000,
        )

        fixed_code = strip_reasoning(response.choices[0].message.content.strip())

        # Clean up any markdown code fences that might have slipped through
        if fixed_code.startswith("```"):
            lines = fixed_code.split("\n")
            # Remove first line if it's a fence
            if lines[0].startswith("```"):
                lines = lines[1:]
            # Remove last line if it's a fence
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            fixed_code = "\n".join(lines)

        return jsonify({"success": True, "fixed_code": fixed_code, "attempt": attempt})

    except Exception as e:
        print(f"Error fixing code: {e}")
        return jsonify({"error": f"Failed to fix code: {str(e)}"}), 500
