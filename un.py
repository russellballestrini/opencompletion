#!/usr/bin/env python3
# This is free software for the public good of a permacomputer hosted at
# permacomputer.com, an always-on computer by the people, for the people.
# One which is durable, easy to repair, & distributed like tap water
# for machine learning intelligence.
#
# The permacomputer is community-owned infrastructure optimized around
# four values:
#
#   TRUTH      First principles, math & science, open source code freely distributed
#   FREEDOM    Voluntary partnerships, freedom from tyranny & corporate control
#   HARMONY    Minimal waste, self-renewing systems with diverse thriving connections
#   LOVE       Be yourself without hurting others, cooperation through natural law
#
# This software contributes to that vision by enabling code execution across 42+ programming languages through a unified interface, accessible to all.
# Code is seeds to sprout on any abandoned technology.

import hashlib
import hmac
import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, Any, List

try:
    import requests
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False
    requests = None  # type: ignore


API_BASE = os.environ.get("UNSANDBOX_API_URL", "https://api.unsandbox.com").rstrip("/")
POLL_DELAYS_MS = [300, 450, 700, 900, 650, 1600, 2000]
LANGUAGES_CACHE_TTL = 3600  # 1 hour


class CredentialsError(Exception):
    """Raised when credentials cannot be found or are invalid."""


class DependencyError(Exception):
    """Raised when a required dependency is not installed."""
    pass


def _check_requests():
    """Check if requests is available, raise helpful error if not."""
    if not REQUESTS_AVAILABLE:
        raise DependencyError(
            "requests is required for HTTP operations. "
            "Install with: pip install requests"
        )
    pass


def _get_unsandbox_dir() -> Path:
    """Get ~/.unsandbox directory path, creating if necessary."""
    home = Path.home()
    unsandbox_dir = home / ".unsandbox"
    unsandbox_dir.mkdir(exist_ok=True, mode=0o700)
    return unsandbox_dir


def _load_credentials_from_csv(csv_path: Path, account_index: int = 0) -> Optional[tuple[str, str]]:
    """Load credentials from CSV file (public_key,secret_key per line)."""
    if not csv_path.exists():
        return None

    try:
        with open(csv_path, "r") as f:
            data_index = 0
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if data_index == account_index:
                    parts = line.split(",")
                    if len(parts) >= 2:
                        return (parts[0].strip(), parts[1].strip())
                data_index += 1
        return None
    except Exception:
        return None


def _resolve_credentials(
    public_key: Optional[str] = None,
    secret_key: Optional[str] = None,
    account_index: Optional[int] = None,
) -> tuple[str, str]:
    """
    Resolve credentials from 4-tier priority system.

    Priority:
        1. Function arguments (public_key, secret_key)
        2. Explicit account_index (>= 0) -> load from accounts.csv
        3. Environment variables (UNSANDBOX_PUBLIC_KEY, UNSANDBOX_SECRET_KEY)
        4. Default CSV lookup (account 0 or UNSANDBOX_ACCOUNT env)
    """
    # Tier 1: Function arguments
    if public_key and secret_key:
        return (public_key, secret_key)

    # Tier 2: Explicit account_index overrides env vars
    if account_index is not None and account_index >= 0:
        unsandbox_dir = _get_unsandbox_dir()
        creds = _load_credentials_from_csv(unsandbox_dir / "accounts.csv", account_index)
        if creds:
            return creds
        creds = _load_credentials_from_csv(Path("accounts.csv"), account_index)
        if creds:
            return creds
        raise CredentialsError(
            f"No credentials found for account index {account_index} in accounts.csv"
        )

    # Tier 3: Environment variables
    env_pk = os.environ.get("UNSANDBOX_PUBLIC_KEY")
    env_sk = os.environ.get("UNSANDBOX_SECRET_KEY")
    if env_pk and env_sk:
        return (env_pk, env_sk)

    # Tier 4: Default CSV lookup (UNSANDBOX_ACCOUNT env or index 0)
    default_index = int(os.environ.get("UNSANDBOX_ACCOUNT", "0"))
    unsandbox_dir = _get_unsandbox_dir()
    creds = _load_credentials_from_csv(unsandbox_dir / "accounts.csv", default_index)
    if creds:
        return creds

    creds = _load_credentials_from_csv(Path("accounts.csv"), default_index)
    if creds:
        return creds

    raise CredentialsError(
        "No credentials found. Please provide via:\n"
        "  1. Function arguments (public_key, secret_key)\n"
        "  2. --account N (select row from accounts.csv)\n"
        "  3. Environment variables (UNSANDBOX_PUBLIC_KEY, UNSANDBOX_SECRET_KEY)\n"
        "  4. ~/.unsandbox/accounts.csv\n"
        "  5. ./accounts.csv"
    )


def _sign_request(
    secret_key: str,
    timestamp: int,
    method: str,
    path: str,
    body: Optional[str] = None,
) -> str:
    """
    Sign a request using HMAC-SHA256.

    Message format: "timestamp:METHOD:path:body"
    Returns: 64-character hex string
    """
    body_str = body or ""
    # The server verifies against the path without its query string
    # (conn.request_path), so sign only the part before '?'.
    path = path.split("?", 1)[0]
    message = f"{timestamp}:{method}:{path}:{body_str}"
    signature = hmac.new(
        secret_key.encode(),
        message.encode(),
        hashlib.sha256,
    ).hexdigest()
    return signature


def _make_request(
    method: str,
    path: str,
    public_key: str,
    secret_key: str,
    data: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Make an authenticated HTTP request to the API.

    Raises requests.RequestException on network errors.
    Raises ValueError if response is not valid JSON.
    Raises DependencyError if requests is not installed.
    """
    _check_requests()
    url = f"{API_BASE}{path}"
    timestamp = int(time.time())
    body = json.dumps(data) if data else ""

    signature = _sign_request(secret_key, timestamp, method, path, body if data else None)

    headers = {
        "Authorization": f"Bearer {public_key}",
        "X-Timestamp": str(timestamp),
        "X-Signature": signature,
        "Content-Type": "application/json",
    }

    if method == "GET":
        response = requests.get(url, headers=headers, timeout=120)
    elif method == "POST":
        response = requests.post(url, headers=headers, json=data, timeout=120)
    elif method == "PATCH":
        response = requests.patch(url, headers=headers, json=data, timeout=120)
    elif method == "DELETE":
        response = requests.delete(url, headers=headers, timeout=120)
    else:
        raise ValueError(f"Unsupported HTTP method: {method}")

    response.raise_for_status()
    return response.json()


def _make_request_with_sudo(
    method: str,
    path: str,
    public_key: str,
    secret_key: str,
    data: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Make an authenticated HTTP request with sudo OTP challenge handling.

    If the server returns 428 (Precondition Required), prompts for OTP
    and retries the request with X-Sudo-OTP and X-Sudo-Challenge headers.

    Used for destructive operations: service destroy/unlock, snapshot delete/unlock,
    image delete/unlock.
    """
    import sys

    url = f"{API_BASE}{path}"
    timestamp = int(time.time())
    body = json.dumps(data) if data else ""

    signature = _sign_request(secret_key, timestamp, method, path, body if data else None)

    headers = {
        "Authorization": f"Bearer {public_key}",
        "X-Timestamp": str(timestamp),
        "X-Signature": signature,
        "Content-Type": "application/json",
    }

    if method == "GET":
        response = requests.get(url, headers=headers, timeout=120)
    elif method == "POST":
        response = requests.post(url, headers=headers, json=data, timeout=120)
    elif method == "PATCH":
        response = requests.patch(url, headers=headers, json=data, timeout=120)
    elif method == "DELETE":
        response = requests.delete(url, headers=headers, timeout=120)
    else:
        raise ValueError(f"Unsupported HTTP method: {method}")

    # Handle 428 sudo OTP challenge
    if response.status_code == 428:
        try:
            challenge_data = response.json()
            challenge_id = challenge_data.get("challenge_id", "")
        except (ValueError, KeyError):
            challenge_id = ""

        print("\033[33mConfirmation required. Check your email for a one-time code.\033[0m", file=sys.stderr)
        try:
            otp = input("Enter OTP: ").strip()
        except (EOFError, KeyboardInterrupt):
            raise ValueError("Operation cancelled")

        if not otp:
            raise ValueError("Operation cancelled")

        # Retry with sudo headers
        retry_timestamp = int(time.time())
        retry_signature = _sign_request(secret_key, retry_timestamp, method, path, body if data else None)

        retry_headers = {
            "Authorization": f"Bearer {public_key}",
            "X-Timestamp": str(retry_timestamp),
            "X-Signature": retry_signature,
            "Content-Type": "application/json",
            "X-Sudo-OTP": otp,
            "X-Sudo-Challenge": challenge_id,
        }

        if method == "GET":
            response = requests.get(url, headers=retry_headers, timeout=120)
        elif method == "POST":
            response = requests.post(url, headers=retry_headers, json=data, timeout=120)
        elif method == "PATCH":
            response = requests.patch(url, headers=retry_headers, json=data, timeout=120)
        elif method == "DELETE":
            response = requests.delete(url, headers=retry_headers, timeout=120)

    response.raise_for_status()
    return response.json()


def _get_languages_cache_path() -> Path:
    """Get path to languages cache file."""
    return _get_unsandbox_dir() / "languages.json"


def _load_languages_cache() -> Optional[List[str]]:
    """Load languages from cache if valid (< 1 hour old)."""
    cache_path = _get_languages_cache_path()
    if not cache_path.exists():
        return None

    try:
        with open(cache_path, "r") as f:
            data = json.load(f)

        # Check if cache is fresh
        mtime = cache_path.stat().st_mtime
        age_seconds = time.time() - mtime
        if age_seconds < LANGUAGES_CACHE_TTL:
            return data.get("languages")
    except Exception:
        pass

    return None


def _save_languages_cache(languages: List[str]) -> None:
    """Save languages to cache."""
    try:
        cache_path = _get_languages_cache_path()
        with open(cache_path, "w") as f:
            json.dump({"languages": languages, "timestamp": int(time.time())}, f)
    except Exception:
        pass  # Cache failures are non-fatal


def execute_code(
    language: str,
    code: str,
    public_key: Optional[str] = None,
    secret_key: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Execute code synchronously (blocks until completion).

    Args:
        language: Programming language (e.g., "python", "javascript", "go")
        code: Source code to execute
        public_key: Optional API key (uses credentials resolution if not provided)
        secret_key: Optional API secret (uses credentials resolution if not provided)

    Returns:
        Response dict containing stdout, stderr, exit code, etc.

    Raises:
        requests.RequestException: Network errors
        ValueError: Invalid response format
        CredentialsError: Missing credentials
    """
    public_key, secret_key = _resolve_credentials(public_key, secret_key)
    response = _make_request(
        "POST",
        "/execute",
        public_key,
        secret_key,
        {"language": language, "code": code},
    )

    # If we got a job_id, poll until completion
    job_id = response.get("job_id")
    status = response.get("status")

    if job_id and status in ("pending", "running"):
        return wait_for_job(job_id, public_key, secret_key)

    return response


def execute_async(
    language: str,
    code: str,
    public_key: Optional[str] = None,
    secret_key: Optional[str] = None,
) -> str:
    """
    Execute code asynchronously (returns immediately with job_id).

    Args:
        language: Programming language (e.g., "python", "javascript")
        code: Source code to execute
        public_key: Optional API key
        secret_key: Optional API secret

    Returns:
        Job ID string

    Raises:
        requests.RequestException: Network errors
        ValueError: Invalid response format
        CredentialsError: Missing credentials
    """
    public_key, secret_key = _resolve_credentials(public_key, secret_key)
    response = _make_request(
        "POST",
        "/execute",
        public_key,
        secret_key,
        {"language": language, "code": code},
    )
    return response.get("job_id")


def get_job(
    job_id: str,
    public_key: Optional[str] = None,
    secret_key: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Get current status/result of a job (single poll, no waiting).

    Args:
        job_id: Job ID from execute_async()
        public_key: Optional API key
        secret_key: Optional API secret

    Returns:
        Job response dict

    Raises:
        requests.RequestException: Network errors
        ValueError: Invalid response format
        CredentialsError: Missing credentials
    """
    public_key, secret_key = _resolve_credentials(public_key, secret_key)
    return _make_request("GET", f"/jobs/{job_id}", public_key, secret_key)


def wait_for_job(
    job_id: str,
    public_key: Optional[str] = None,
    secret_key: Optional[str] = None,
    timeout: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Wait for job completion with exponential backoff polling.

    Polling delays (ms): [300, 450, 700, 900, 650, 1600, 2000, ...]
    Cumulative: 300, 750, 1450, 2350, 3000, 4600, 6600ms+

    Args:
        job_id: Job ID from execute_async()
        public_key: Optional API key
        secret_key: Optional API secret
        timeout: Optional maximum wait time in seconds (None = wait indefinitely)

    Returns:
        Final job result when status is terminal (completed, failed, timeout, cancelled)

    Raises:
        requests.RequestException: Network errors
        ValueError: Invalid response format
        CredentialsError: Missing credentials
        TimeoutError: If timeout is exceeded before job completes
    """
    public_key, secret_key = _resolve_credentials(public_key, secret_key)
    poll_count = 0
    start_time = time.time()

    while True:
        # Check timeout
        if timeout is not None:
            elapsed = time.time() - start_time
            if elapsed >= timeout:
                raise TimeoutError(f"Job {job_id} did not complete within {timeout} seconds")

        # Sleep before polling
        delay_idx = min(poll_count, len(POLL_DELAYS_MS) - 1)
        time.sleep(POLL_DELAYS_MS[delay_idx] / 1000.0)
        poll_count += 1

        response = get_job(job_id, public_key, secret_key)
        status = response.get("status")

        if status in ("completed", "failed", "timeout", "cancelled"):
            return response

        # Still running, continue polling


def cancel_job(
    job_id: str,
    public_key: Optional[str] = None,
    secret_key: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Cancel a running job.

    Args:
        job_id: Job ID to cancel
        public_key: Optional API key
        secret_key: Optional API secret

    Returns:
        Response dict with cancellation confirmation

    Raises:
        requests.RequestException: Network errors
        ValueError: Invalid response format
        CredentialsError: Missing credentials
    """
    public_key, secret_key = _resolve_credentials(public_key, secret_key)
    return _make_request("DELETE", f"/jobs/{job_id}", public_key, secret_key)


def list_jobs(
    public_key: Optional[str] = None,
    secret_key: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    List all jobs for the authenticated account.

    Args:
        public_key: Optional API key
        secret_key: Optional API secret

    Returns:
        List of job dicts

    Raises:
        requests.RequestException: Network errors
        ValueError: Invalid response format
        CredentialsError: Missing credentials
    """
    public_key, secret_key = _resolve_credentials(public_key, secret_key)
    response = _make_request("GET", "/jobs", public_key, secret_key)
    return response.get("jobs", [])


def get_languages(
    public_key: Optional[str] = None,
    secret_key: Optional[str] = None,
) -> List[str]:
    """
    Get list of supported programming languages.

    Results are cached for 1 hour in ~/.unsandbox/languages.json

    Args:
        public_key: Optional API key
        secret_key: Optional API secret

    Returns:
        List of language identifiers (e.g., ["python", "javascript", "go", ...])

    Raises:
        requests.RequestException: Network errors
        ValueError: Invalid response format
        CredentialsError: Missing credentials
    """
    # Try cache first
    cached = _load_languages_cache()
    if cached:
        return cached

    public_key, secret_key = _resolve_credentials(public_key, secret_key)
    response = _make_request("GET", "/languages", public_key, secret_key)
    languages = response.get("languages", [])

    # Cache the result
    _save_languages_cache(languages)
    return languages


# Language detection mapping (file extension -> language)
_LANGUAGE_MAP = {
    "py": "python",
    "js": "javascript",
    "ts": "typescript",
    "rb": "ruby",
    "php": "php",
    "pl": "perl",
    "sh": "bash",
    "r": "r",
    "R": "r",
    "lua": "lua",
    "go": "go",
    "rs": "rust",
    "c": "c",
    "cpp": "cpp",
    "cc": "cpp",
    "cxx": "cpp",
    "java": "java",
    "kt": "kotlin",
    "m": "objc",
    "cs": "csharp",
    "fs": "fsharp",
    "hs": "haskell",
    "ml": "ocaml",
    "clj": "clojure",
    "scm": "scheme",
    "ss": "scheme",
    "erl": "erlang",
    "ex": "elixir",
    "exs": "elixir",
    "jl": "julia",
    "d": "d",
    "nim": "nim",
    "zig": "zig",
    "v": "v",
    "cr": "crystal",
    "dart": "dart",
    "groovy": "groovy",
    "f90": "fortran",
    "f95": "fortran",
    "lisp": "commonlisp",
    "lsp": "commonlisp",
    "cob": "cobol",
    "tcl": "tcl",
    "raku": "raku",
    "pro": "prolog",
    "p": "prolog",
    "4th": "forth",
    "forth": "forth",
    "fth": "forth",
}


def detect_language(filename: str) -> Optional[str]:
    """
    Detect programming language from filename extension.

    Args:
        filename: Filename to detect language from (e.g., "script.py")

    Returns:
        Language identifier (e.g., "python") or None if unknown

    Examples:
        detect_language("hello.py")   # -> "python"
        detect_language("script.js")  # -> "javascript"
        detect_language("main.go")    # -> "go"
        detect_language("unknown")    # -> None
    """
    if not filename or "." not in filename:
        return None

    ext = filename.rsplit(".", 1)[-1].lower()
    return _LANGUAGE_MAP.get(ext)


def session_snapshot(
    session_id: str,
    public_key: Optional[str] = None,
    secret_key: Optional[str] = None,
    name: Optional[str] = None,
    ephemeral: bool = False,
) -> str:
    """
    Create a snapshot of a session.

    Args:
        session_id: Session ID to snapshot
        public_key: Optional API key
        secret_key: Optional API secret
        name: Optional snapshot name
        ephemeral: If True, snapshot is temporary and may be auto-deleted

    Returns:
        Snapshot ID

    Raises:
        requests.RequestException: Network errors
        ValueError: Invalid response format
        CredentialsError: Missing credentials
    """
    public_key, secret_key = _resolve_credentials(public_key, secret_key)
    data = {"session_id": session_id, "ephemeral": ephemeral}
    if name:
        data["name"] = name

    response = _make_request("POST", "/snapshots", public_key, secret_key, data)
    return response.get("snapshot_id")


def service_snapshot(
    service_id: str,
    public_key: Optional[str] = None,
    secret_key: Optional[str] = None,
    name: Optional[str] = None,
) -> str:
    """
    Create a snapshot of a service.

    Args:
        service_id: Service ID to snapshot
        public_key: Optional API key
        secret_key: Optional API secret
        name: Optional snapshot name

    Returns:
        Snapshot ID

    Raises:
        requests.RequestException: Network errors
        ValueError: Invalid response format
        CredentialsError: Missing credentials
    """
    public_key, secret_key = _resolve_credentials(public_key, secret_key)
    data = {"service_id": service_id}
    if name:
        data["name"] = name

    response = _make_request("POST", "/snapshots", public_key, secret_key, data)
    return response.get("snapshot_id")


def list_snapshots(
    public_key: Optional[str] = None,
    secret_key: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    List all snapshots.

    Args:
        public_key: Optional API key
        secret_key: Optional API secret

    Returns:
        List of snapshot dicts

    Raises:
        requests.RequestException: Network errors
        ValueError: Invalid response format
        CredentialsError: Missing credentials
    """
    public_key, secret_key = _resolve_credentials(public_key, secret_key)
    response = _make_request("GET", "/snapshots", public_key, secret_key)
    return response.get("snapshots", [])


def get_snapshot(
    snapshot_id: str,
    public_key: Optional[str] = None,
    secret_key: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Get details of a specific snapshot.

    Args:
        snapshot_id: Snapshot ID to get details for
        public_key: Optional API key
        secret_key: Optional API secret

    Returns:
        Snapshot details dict containing:
        - id: Snapshot ID
        - name: Snapshot name
        - type: "session" or "service"
        - source_id: Original resource ID
        - hot: Whether snapshot preserves running state
        - locked: Whether snapshot is locked
        - created_at: Creation timestamp
        - size_bytes: Size in bytes

    Raises:
        requests.RequestException: Network errors
        ValueError: Invalid response format
        CredentialsError: Missing credentials
    """
    public_key, secret_key = _resolve_credentials(public_key, secret_key)
    return _make_request("GET", f"/snapshots/{snapshot_id}", public_key, secret_key)


def restore_snapshot(
    snapshot_id: str,
    public_key: Optional[str] = None,
    secret_key: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Restore a snapshot (NEW).

    Args:
        snapshot_id: Snapshot ID to restore
        public_key: Optional API key
        secret_key: Optional API secret

    Returns:
        Response dict with restored resource info

    Raises:
        requests.RequestException: Network errors
        ValueError: Invalid response format
        CredentialsError: Missing credentials
    """
    public_key, secret_key = _resolve_credentials(public_key, secret_key)
    return _make_request("POST", f"/snapshots/{snapshot_id}/restore", public_key, secret_key, {})


def delete_snapshot(
    snapshot_id: str,
    public_key: Optional[str] = None,
    secret_key: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Delete a snapshot (NEW).

    Args:
        snapshot_id: Snapshot ID to delete
        public_key: Optional API key
        secret_key: Optional API secret

    Returns:
        Response dict with deletion confirmation

    Raises:
        requests.RequestException: Network errors
        ValueError: Invalid response format
        CredentialsError: Missing credentials
    """
    public_key, secret_key = _resolve_credentials(public_key, secret_key)
    return _make_request_with_sudo("DELETE", f"/snapshots/{snapshot_id}", public_key, secret_key)


# =============================================================================
# Session Management Functions
# =============================================================================


def list_sessions(
    public_key: Optional[str] = None,
    secret_key: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    List all sessions for the authenticated account.

    Args:
        public_key: Optional API key
        secret_key: Optional API secret

    Returns:
        List of session dicts containing id, container_name, status, etc.

    Raises:
        requests.RequestException: Network errors
        ValueError: Invalid response format
        CredentialsError: Missing credentials
    """
    public_key, secret_key = _resolve_credentials(public_key, secret_key)
    response = _make_request("GET", "/sessions", public_key, secret_key)
    return response.get("sessions", [])


def get_session(
    session_id: str,
    public_key: Optional[str] = None,
    secret_key: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Get details of a specific session.

    Args:
        session_id: Session ID to get details for
        public_key: Optional API key
        secret_key: Optional API secret

    Returns:
        Session details dict

    Raises:
        requests.RequestException: Network errors
        ValueError: Invalid response format
        CredentialsError: Missing credentials
    """
    public_key, secret_key = _resolve_credentials(public_key, secret_key)
    return _make_request("GET", f"/sessions/{session_id}", public_key, secret_key)


def create_session(
    language: Optional[str] = None,
    network_mode: str = "zerotrust",
    ttl: int = 3600,
    public_key: Optional[str] = None,
    secret_key: Optional[str] = None,
    shell: Optional[str] = None,
    multiplexer: Optional[str] = None,
    vcpu: int = 1,
) -> Dict[str, Any]:
    """
    Create a new interactive session.

    Args:
        language: Optional programming language for the session
        network_mode: Network mode - "zerotrust" (default, no network) or "semitrusted" (with network)
        ttl: Time to live in seconds (default 3600)
        public_key: Optional API key
        secret_key: Optional API secret
        shell: Optional shell to use (e.g., "bash", "python3")
        multiplexer: Optional terminal multiplexer ("tmux" or "screen")
        vcpu: Number of vCPUs (1-8, default 1)

    Returns:
        Response dict containing session_id, container_name, etc.

    Raises:
        requests.RequestException: Network errors
        ValueError: Invalid response format
        CredentialsError: Missing credentials
    """
    public_key, secret_key = _resolve_credentials(public_key, secret_key)
    data: Dict[str, Any] = {
        "network_mode": network_mode,
        "ttl": ttl,
    }
    if language:
        data["language"] = language
    if shell:
        data["shell"] = shell
    if multiplexer:
        data["multiplexer"] = multiplexer
    if vcpu > 1:
        data["vcpu"] = vcpu

    return _make_request("POST", "/sessions", public_key, secret_key, data)


def delete_session(
    session_id: str,
    public_key: Optional[str] = None,
    secret_key: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Delete/terminate a session.

    Args:
        session_id: Session ID to delete
        public_key: Optional API key
        secret_key: Optional API secret

    Returns:
        Response dict with deletion confirmation and optional artifacts

    Raises:
        requests.RequestException: Network errors
        ValueError: Invalid response format
        CredentialsError: Missing credentials
    """
    public_key, secret_key = _resolve_credentials(public_key, secret_key)
    return _make_request("DELETE", f"/sessions/{session_id}", public_key, secret_key)


def freeze_session(
    session_id: str,
    public_key: Optional[str] = None,
    secret_key: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Freeze a session (pause execution, preserve state).

    Args:
        session_id: Session ID to freeze
        public_key: Optional API key
        secret_key: Optional API secret

    Returns:
        Response dict with freeze confirmation

    Raises:
        requests.RequestException: Network errors
        ValueError: Invalid response format
        CredentialsError: Missing credentials
    """
    public_key, secret_key = _resolve_credentials(public_key, secret_key)
    return _make_request("POST", f"/sessions/{session_id}/freeze", public_key, secret_key, {})


def unfreeze_session(
    session_id: str,
    public_key: Optional[str] = None,
    secret_key: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Unfreeze a session (resume execution).

    Args:
        session_id: Session ID to unfreeze
        public_key: Optional API key
        secret_key: Optional API secret

    Returns:
        Response dict with unfreeze confirmation

    Raises:
        requests.RequestException: Network errors
        ValueError: Invalid response format
        CredentialsError: Missing credentials
    """
    public_key, secret_key = _resolve_credentials(public_key, secret_key)
    return _make_request("POST", f"/sessions/{session_id}/unfreeze", public_key, secret_key, {})


def boost_session(
    session_id: str,
    public_key: Optional[str] = None,
    secret_key: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Boost a session (increase resources).

    Args:
        session_id: Session ID to boost
        public_key: Optional API key
        secret_key: Optional API secret

    Returns:
        Response dict with boost confirmation

    Raises:
        requests.RequestException: Network errors
        ValueError: Invalid response format
        CredentialsError: Missing credentials
    """
    public_key, secret_key = _resolve_credentials(public_key, secret_key)
    return _make_request("POST", f"/sessions/{session_id}/boost", public_key, secret_key, {})


def unboost_session(
    session_id: str,
    public_key: Optional[str] = None,
    secret_key: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Unboost a session (return to normal resources).

    Args:
        session_id: Session ID to unboost
        public_key: Optional API key
        secret_key: Optional API secret

    Returns:
        Response dict with unboost confirmation

    Raises:
        requests.RequestException: Network errors
        ValueError: Invalid response format
        CredentialsError: Missing credentials
    """
    public_key, secret_key = _resolve_credentials(public_key, secret_key)
    return _make_request("POST", f"/sessions/{session_id}/unboost", public_key, secret_key, {})


def shell_session(
    session_id: str,
    command: str,
    public_key: Optional[str] = None,
    secret_key: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Execute a shell command in a session.

    Note: This is for one-off commands. For interactive shell access,
    use WebSocket connection to /sessions/{id}/shell.

    Args:
        session_id: Session ID to execute command in
        command: Shell command to execute
        public_key: Optional API key
        secret_key: Optional API secret

    Returns:
        Response dict with command output

    Raises:
        requests.RequestException: Network errors
        ValueError: Invalid response format
        CredentialsError: Missing credentials
    """
    public_key, secret_key = _resolve_credentials(public_key, secret_key)
    return _make_request(
        "POST",
        f"/sessions/{session_id}/shell",
        public_key,
        secret_key,
        {"command": command},
    )


# =============================================================================
# Service Management Functions
# =============================================================================


def list_services(
    public_key: Optional[str] = None,
    secret_key: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    List all services for the authenticated account.

    Args:
        public_key: Optional API key
        secret_key: Optional API secret

    Returns:
        List of service dicts containing id, name, status, ports, etc.

    Raises:
        requests.RequestException: Network errors
        ValueError: Invalid response format
        CredentialsError: Missing credentials
    """
    public_key, secret_key = _resolve_credentials(public_key, secret_key)
    response = _make_request("GET", "/services", public_key, secret_key)
    return response.get("services", [])


def create_service(
    name: str,
    ports: Optional[List[int]] = None,
    bootstrap: Optional[str] = None,
    public_key: Optional[str] = None,
    secret_key: Optional[str] = None,
    network_mode: str = "semitrusted",
    custom_domains: Optional[List[str]] = None,
    vcpu: int = 1,
    service_type: Optional[str] = None,
    unfreeze_on_demand: bool = False,
    golden_image: Optional[str] = None,
    input_files: Optional[List[Dict[str, str]]] = None,
) -> Dict[str, Any]:
    """
    Create a new persistent service.

    Args:
        name: Service name (used for subdomain: name.on.unsandbox.com)
        ports: List of ports to expose (e.g., [80, 443])
        bootstrap: Bootstrap script content, URL, or inline command
        public_key: Optional API key
        secret_key: Optional API secret
        network_mode: Network mode (default "semitrusted" for services)
        custom_domains: Optional list of custom domain names
        vcpu: Number of vCPUs (1-8, default 1)
        service_type: Optional service type for SRV records (e.g., "minecraft")
        unfreeze_on_demand: If True, automatically unfreeze service on incoming requests
        golden_image: Optional LXD image alias (e.g., "golden-alpine-3.24")
        input_files: Optional list of dicts with "filename" and "content" (base64)

    Returns:
        Response dict containing service_id, etc.

    Raises:
        requests.RequestException: Network errors
        ValueError: Invalid response format
        CredentialsError: Missing credentials
    """
    public_key, secret_key = _resolve_credentials(public_key, secret_key)
    data: Dict[str, Any] = {
        "name": name,
        "network_mode": network_mode,
    }
    if ports:
        data["ports"] = ports
    if bootstrap:
        # Check if it looks like a URL
        if bootstrap.startswith("http://") or bootstrap.startswith("https://"):
            data["bootstrap"] = bootstrap
        else:
            data["bootstrap_content"] = bootstrap
    if custom_domains:
        data["custom_domains"] = custom_domains
    if vcpu > 1:
        data["vcpu"] = vcpu
    if service_type:
        data["service_type"] = service_type
    if unfreeze_on_demand:
        data["unfreeze_on_demand"] = unfreeze_on_demand
    if golden_image:
        data["golden_image"] = golden_image
    if input_files:
        data["input_files"] = input_files

    return _make_request("POST", "/services", public_key, secret_key, data)


def get_service(
    service_id: str,
    public_key: Optional[str] = None,
    secret_key: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Get details of a specific service.

    Args:
        service_id: Service ID to get details for
        public_key: Optional API key
        secret_key: Optional API secret

    Returns:
        Service details dict

    Raises:
        requests.RequestException: Network errors
        ValueError: Invalid response format
        CredentialsError: Missing credentials
    """
    public_key, secret_key = _resolve_credentials(public_key, secret_key)
    return _make_request("GET", f"/services/{service_id}", public_key, secret_key)


def update_service(
    service_id: str,
    public_key: Optional[str] = None,
    secret_key: Optional[str] = None,
    vcpu: Optional[int] = None,
    **kwargs,
) -> Dict[str, Any]:
    """
    Update a service (e.g., resize vCPU/memory).

    Args:
        service_id: Service ID to update
        public_key: Optional API key
        secret_key: Optional API secret
        vcpu: Optional new vCPU count (1-8)
        **kwargs: Additional fields to update

    Returns:
        Response dict with update confirmation

    Raises:
        requests.RequestException: Network errors
        ValueError: Invalid response format
        CredentialsError: Missing credentials
    """
    public_key, secret_key = _resolve_credentials(public_key, secret_key)
    data: Dict[str, Any] = {}
    if vcpu is not None:
        data["vcpu"] = vcpu
    data.update(kwargs)

    return _make_request("PATCH", f"/services/{service_id}", public_key, secret_key, data)


def delete_service(
    service_id: str,
    public_key: Optional[str] = None,
    secret_key: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Delete/destroy a service.

    Args:
        service_id: Service ID to delete
        public_key: Optional API key
        secret_key: Optional API secret

    Returns:
        Response dict with deletion confirmation

    Raises:
        requests.RequestException: Network errors
        ValueError: Invalid response format
        CredentialsError: Missing credentials
    """
    public_key, secret_key = _resolve_credentials(public_key, secret_key)
    return _make_request_with_sudo("DELETE", f"/services/{service_id}", public_key, secret_key)


def freeze_service(
    service_id: str,
    public_key: Optional[str] = None,
    secret_key: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Freeze a service (pause execution, preserve state).

    Args:
        service_id: Service ID to freeze
        public_key: Optional API key
        secret_key: Optional API secret

    Returns:
        Response dict with freeze confirmation

    Raises:
        requests.RequestException: Network errors
        ValueError: Invalid response format
        CredentialsError: Missing credentials
    """
    public_key, secret_key = _resolve_credentials(public_key, secret_key)
    return _make_request("POST", f"/services/{service_id}/freeze", public_key, secret_key, {})


def unfreeze_service(
    service_id: str,
    public_key: Optional[str] = None,
    secret_key: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Unfreeze a service (resume execution).

    Args:
        service_id: Service ID to unfreeze
        public_key: Optional API key
        secret_key: Optional API secret

    Returns:
        Response dict with unfreeze confirmation

    Raises:
        requests.RequestException: Network errors
        ValueError: Invalid response format
        CredentialsError: Missing credentials
    """
    public_key, secret_key = _resolve_credentials(public_key, secret_key)
    return _make_request("POST", f"/services/{service_id}/unfreeze", public_key, secret_key, {})


def lock_service(
    service_id: str,
    public_key: Optional[str] = None,
    secret_key: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Lock a service to prevent accidental deletion.

    Args:
        service_id: Service ID to lock
        public_key: Optional API key
        secret_key: Optional API secret

    Returns:
        Response dict with lock confirmation

    Raises:
        requests.RequestException: Network errors
        ValueError: Invalid response format
        CredentialsError: Missing credentials
    """
    public_key, secret_key = _resolve_credentials(public_key, secret_key)
    return _make_request("POST", f"/services/{service_id}/lock", public_key, secret_key, {})


def unlock_service(
    service_id: str,
    public_key: Optional[str] = None,
    secret_key: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Unlock a service to allow deletion.

    Args:
        service_id: Service ID to unlock
        public_key: Optional API key
        secret_key: Optional API secret

    Returns:
        Response dict with unlock confirmation

    Raises:
        requests.RequestException: Network errors
        ValueError: Invalid response format
        CredentialsError: Missing credentials
    """
    public_key, secret_key = _resolve_credentials(public_key, secret_key)
    return _make_request_with_sudo("POST", f"/services/{service_id}/unlock", public_key, secret_key, {})


def update_service_domains(
    service_id: str,
    domains: list,
    action: str = "custom_domains",
    public_key: Optional[str] = None,
    secret_key: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Update custom domains for a service.

    Args:
        service_id: Service ID
        domains: List of domain strings
        action: "custom_domains" (replace), "add" (merge), or "remove" (subtract)
        public_key: Optional API key
        secret_key: Optional API secret

    Returns:
        Response dict with updated domains
    """
    public_key, secret_key = _resolve_credentials(public_key, secret_key)
    return _make_request("PUT", f"/services/{service_id}/domains", public_key, secret_key, {action: domains})


def set_unfreeze_on_demand(
    service_id: str,
    enabled: bool,
    public_key: Optional[str] = None,
    secret_key: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Enable or disable automatic unfreezing on incoming requests.

    When enabled, a frozen service will automatically wake up when it
    receives an incoming HTTP request.

    Args:
        service_id: Service ID to configure
        enabled: True to enable auto-unfreeze, False to disable
        public_key: Optional API key
        secret_key: Optional API secret

    Returns:
        Response dict with update confirmation

    Raises:
        requests.RequestException: Network errors
        ValueError: Invalid response format
        CredentialsError: Missing credentials
    """
    public_key, secret_key = _resolve_credentials(public_key, secret_key)
    return _make_request(
        "PATCH",
        f"/services/{service_id}",
        public_key,
        secret_key,
        {"unfreeze_on_demand": enabled},
    )


def set_show_freeze_page(
    service_id: str,
    enabled: bool,
    public_key: Optional[str] = None,
    secret_key: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Enable or disable freeze page display for a service.

    When disabled, frozen services return a JSON error response instead of
    displaying the payment/unfreeze page to visitors.

    Args:
        service_id: Service ID to configure
        enabled: True to show freeze page, False to return JSON error
        public_key: Optional API key
        secret_key: Optional API secret

    Returns:
        Response dict with update confirmation

    Raises:
        requests.RequestException: Network errors
        ValueError: Invalid response format
        CredentialsError: Missing credentials
    """
    public_key, secret_key = _resolve_credentials(public_key, secret_key)
    return _make_request(
        "PATCH",
        f"/services/{service_id}",
        public_key,
        secret_key,
        {"show_freeze_page": enabled},
    )


def get_service_logs(
    service_id: str,
    all_logs: bool = False,
    public_key: Optional[str] = None,
    secret_key: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Get bootstrap/runtime logs for a service.

    Args:
        service_id: Service ID to get logs for
        all_logs: If True, get all logs; if False, get last ~9000 lines (tail)
        public_key: Optional API key
        secret_key: Optional API secret

    Returns:
        Response dict containing "log" field with log content

    Raises:
        requests.RequestException: Network errors
        ValueError: Invalid response format
        CredentialsError: Missing credentials
    """
    public_key, secret_key = _resolve_credentials(public_key, secret_key)
    path = f"/services/{service_id}/logs"
    if all_logs:
        path += "?all=true"
    return _make_request("GET", path, public_key, secret_key)


def get_service_env(
    service_id: str,
    public_key: Optional[str] = None,
    secret_key: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Get environment vault status for a service.

    Returns metadata about the vault (has_vault, count, updated_at)
    but NOT the actual secrets. Use export_service_env to retrieve secrets.

    Args:
        service_id: Service ID to get env status for
        public_key: Optional API key
        secret_key: Optional API secret

    Returns:
        Response dict with has_vault, count, updated_at fields

    Raises:
        requests.RequestException: Network errors
        ValueError: Invalid response format
        CredentialsError: Missing credentials
    """
    public_key, secret_key = _resolve_credentials(public_key, secret_key)
    return _make_request("GET", f"/services/{service_id}/env", public_key, secret_key)


def set_service_env(
    service_id: str,
    env_dict: Dict[str, str],
    public_key: Optional[str] = None,
    secret_key: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Set environment variables for a service.

    Replaces the entire environment vault with the provided variables.
    Variables are encrypted at rest and injected into the container.

    Args:
        service_id: Service ID to set env for
        env_dict: Dictionary of environment variables (KEY: VALUE)
        public_key: Optional API key
        secret_key: Optional API secret

    Returns:
        Response dict with count of variables set

    Raises:
        requests.RequestException: Network errors
        ValueError: Invalid response format
        CredentialsError: Missing credentials
    """
    public_key, secret_key = _resolve_credentials(public_key, secret_key)
    # Convert dict to .env format for the API
    env_content = "\n".join(f"{k}={v}" for k, v in env_dict.items())

    # Note: This endpoint expects text/plain body, but we'll send as JSON
    # and let the API handle conversion
    return _make_request(
        "POST",
        f"/services/{service_id}/env",
        public_key,
        secret_key,
        {"env": env_content},
    )


def delete_service_env(
    service_id: str,
    keys: Optional[List[str]] = None,
    public_key: Optional[str] = None,
    secret_key: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Delete environment vault or specific keys from a service.

    Args:
        service_id: Service ID to delete env from
        keys: Optional list of specific keys to delete; if None, deletes entire vault
        public_key: Optional API key
        secret_key: Optional API secret

    Returns:
        Response dict with deletion confirmation

    Raises:
        requests.RequestException: Network errors
        ValueError: Invalid response format
        CredentialsError: Missing credentials
    """
    public_key, secret_key = _resolve_credentials(public_key, secret_key)
    path = f"/services/{service_id}/env"
    # If specific keys provided, could add as query params (API dependent)
    return _make_request("DELETE", path, public_key, secret_key)


def export_service_env(
    service_id: str,
    public_key: Optional[str] = None,
    secret_key: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Export environment vault secrets for a service.

    Requires HMAC authentication to prove ownership.
    Returns the actual secret values in .env format.

    Args:
        service_id: Service ID to export env from
        public_key: Optional API key
        secret_key: Optional API secret

    Returns:
        Response dict containing "env" field with KEY=VALUE content

    Raises:
        requests.RequestException: Network errors
        ValueError: Invalid response format
        CredentialsError: Missing credentials
    """
    public_key, secret_key = _resolve_credentials(public_key, secret_key)
    return _make_request("POST", f"/services/{service_id}/env/export", public_key, secret_key, {})


def redeploy_service(
    service_id: str,
    bootstrap: Optional[str] = None,
    public_key: Optional[str] = None,
    secret_key: Optional[str] = None,
    input_files: Optional[List[Dict[str, str]]] = None,
) -> Dict[str, Any]:
    """
    Redeploy a service (re-run bootstrap script).

    Bootstrap scripts should be idempotent for proper upgrade behavior.

    Args:
        service_id: Service ID to redeploy
        bootstrap: Optional new bootstrap script/URL (uses existing if not provided)
        public_key: Optional API key
        secret_key: Optional API secret
        input_files: Optional list of dicts with "filename" and "content" (base64)

    Returns:
        Response dict with redeploy confirmation

    Raises:
        requests.RequestException: Network errors
        ValueError: Invalid response format
        CredentialsError: Missing credentials
    """
    public_key, secret_key = _resolve_credentials(public_key, secret_key)
    data: Dict[str, Any] = {}
    if bootstrap:
        if bootstrap.startswith("http://") or bootstrap.startswith("https://"):
            data["bootstrap"] = bootstrap
        else:
            data["bootstrap_content"] = bootstrap
    if input_files:
        data["input_files"] = input_files

    return _make_request("POST", f"/services/{service_id}/redeploy", public_key, secret_key, data)


def execute_in_service(
    service_id: str,
    command: str,
    timeout: int = 30000,
    public_key: Optional[str] = None,
    secret_key: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Execute a command in a running service container.

    Uses async job polling for long-running commands.

    Args:
        service_id: Service ID to execute command in
        command: Shell command to execute
        timeout: Command timeout in milliseconds (default 30000)
        public_key: Optional API key
        secret_key: Optional API secret

    Returns:
        Response dict with job_id for async polling, or direct result

    Raises:
        requests.RequestException: Network errors
        ValueError: Invalid response format
        CredentialsError: Missing credentials
    """
    public_key, secret_key = _resolve_credentials(public_key, secret_key)
    return _make_request(
        "POST",
        f"/services/{service_id}/execute",
        public_key,
        secret_key,
        {"command": command, "timeout": timeout},
    )


def resize_service(
    service_id: str,
    vcpu: int,
    public_key: Optional[str] = None,
    secret_key: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Resize a service's vCPU allocation.

    Args:
        service_id: Service ID to resize
        vcpu: Number of vCPUs (1-8 typically)
        public_key: Optional API key
        secret_key: Optional API secret

    Returns:
        Response dict with updated service info

    Raises:
        requests.RequestException: Network errors
        ValueError: Invalid response format
        CredentialsError: Missing credentials
    """
    public_key, secret_key = _resolve_credentials(public_key, secret_key)
    return _make_request(
        "PATCH",
        f"/services/{service_id}",
        public_key,
        secret_key,
        {"vcpu": vcpu},
    )


# =============================================================================
# Additional Snapshot Functions
# =============================================================================


def lock_snapshot(
    snapshot_id: str,
    public_key: Optional[str] = None,
    secret_key: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Lock a snapshot to prevent accidental deletion.

    Args:
        snapshot_id: Snapshot ID to lock
        public_key: Optional API key
        secret_key: Optional API secret

    Returns:
        Response dict with lock confirmation

    Raises:
        requests.RequestException: Network errors
        ValueError: Invalid response format
        CredentialsError: Missing credentials
    """
    public_key, secret_key = _resolve_credentials(public_key, secret_key)
    return _make_request("POST", f"/snapshots/{snapshot_id}/lock", public_key, secret_key, {})


def unlock_snapshot(
    snapshot_id: str,
    public_key: Optional[str] = None,
    secret_key: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Unlock a snapshot to allow deletion.

    Args:
        snapshot_id: Snapshot ID to unlock
        public_key: Optional API key
        secret_key: Optional API secret

    Returns:
        Response dict with unlock confirmation

    Raises:
        requests.RequestException: Network errors
        ValueError: Invalid response format
        CredentialsError: Missing credentials
    """
    public_key, secret_key = _resolve_credentials(public_key, secret_key)
    return _make_request_with_sudo("POST", f"/snapshots/{snapshot_id}/unlock", public_key, secret_key, {})


def clone_snapshot(
    snapshot_id: str,
    clone_type: str = "session",
    name: Optional[str] = None,
    public_key: Optional[str] = None,
    secret_key: Optional[str] = None,
    shell: Optional[str] = None,
    ports: Optional[List[int]] = None,
) -> Dict[str, Any]:
    """
    Clone a snapshot to create a new session or service.

    Args:
        snapshot_id: Snapshot ID to clone from
        clone_type: Type of resource to create ("session" or "service")
        name: Optional name for the new resource
        public_key: Optional API key
        secret_key: Optional API secret
        shell: Optional shell for session clones
        ports: Optional ports list for service clones

    Returns:
        Response dict containing session_id or service_id

    Raises:
        requests.RequestException: Network errors
        ValueError: Invalid response format
        CredentialsError: Missing credentials
    """
    public_key, secret_key = _resolve_credentials(public_key, secret_key)
    data: Dict[str, Any] = {"type": clone_type}
    if name:
        data["name"] = name
    if shell:
        data["shell"] = shell
    if ports:
        data["ports"] = ports

    return _make_request("POST", f"/snapshots/{snapshot_id}/clone", public_key, secret_key, data)


# =============================================================================
# Images (LXD Container Images)
# =============================================================================


def image_publish(
    source_type: str,
    source_id: str,
    name: Optional[str] = None,
    description: Optional[str] = None,
    public_key: Optional[str] = None,
    secret_key: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Publish a service or snapshot as a portable LXD image.

    Images are independent of containers and survive service deletion.
    They can be shared with other users or made public.

    Args:
        source_type: Type of source ("service" or "snapshot")
        source_id: ID of the service or snapshot to publish
        name: Optional friendly name for the image
        description: Optional description
        public_key: Optional API key
        secret_key: Optional API secret

    Returns:
        Response dict containing:
        - id: Image ID (unsb-image-xxxx-xxxx-xxxx-xxxx)
        - name: Image name
        - fingerprint: LXD image fingerprint
        - size_bytes: Image size in bytes

    Example:
        >>> img = image_publish("service", "unsb-service-xxxx")
        >>> print(img["id"])
    """
    public_key, secret_key = _resolve_credentials(public_key, secret_key)
    data: Dict[str, Any] = {"source_type": source_type, "source_id": source_id}
    if name:
        data["name"] = name
    if description:
        data["description"] = description
    return _make_request("POST", "/images", public_key, secret_key, data)


def list_images(
    filter_type: Optional[str] = None,
    public_key: Optional[str] = None,
    secret_key: Optional[str] = None,
) -> Dict[str, Any]:
    """
    List images accessible to this API key.

    By default lists all accessible images (owned + shared + public).
    Use filter_type to narrow results.

    Args:
        filter_type: Optional filter - "owned", "shared", or "public"
        public_key: Optional API key
        secret_key: Optional API secret

    Returns:
        Response dict containing:
        - images: List of image objects
        - count: Total number of images

    Example:
        >>> result = list_images()
        >>> for img in result["images"]:
        ...     print(f"{img['id']}: {img['name']} ({img['access']})")
    """
    public_key, secret_key = _resolve_credentials(public_key, secret_key)
    endpoint = "/images"
    if filter_type:
        endpoint = f"/images/{filter_type}"
    return _make_request("GET", endpoint, public_key, secret_key)


def get_image(
    image_id: str,
    public_key: Optional[str] = None,
    secret_key: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Get details of a specific image.

    Args:
        image_id: Image ID
        public_key: Optional API key
        secret_key: Optional API secret

    Returns:
        Response dict containing image details:
        - id, name, description, fingerprint
        - source_type, source_id
        - size_bytes, locked, visibility
        - trusted_keys, created_at
    """
    public_key, secret_key = _resolve_credentials(public_key, secret_key)
    return _make_request("GET", f"/images/{image_id}", public_key, secret_key)


def delete_image(
    image_id: str,
    public_key: Optional[str] = None,
    secret_key: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Delete an image.

    Cannot delete locked images - unlock first.

    Args:
        image_id: Image ID to delete
        public_key: Optional API key
        secret_key: Optional API secret

    Returns:
        Response dict with deletion confirmation
    """
    public_key, secret_key = _resolve_credentials(public_key, secret_key)
    return _make_request_with_sudo("DELETE", f"/images/{image_id}", public_key, secret_key)


def lock_image(
    image_id: str,
    public_key: Optional[str] = None,
    secret_key: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Lock an image to prevent accidental deletion.

    Args:
        image_id: Image ID to lock
        public_key: Optional API key
        secret_key: Optional API secret

    Returns:
        Response dict with lock status
    """
    public_key, secret_key = _resolve_credentials(public_key, secret_key)
    return _make_request("POST", f"/images/{image_id}/lock", public_key, secret_key, {})


def unlock_image(
    image_id: str,
    public_key: Optional[str] = None,
    secret_key: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Unlock an image to allow deletion.

    Args:
        image_id: Image ID to unlock
        public_key: Optional API key
        secret_key: Optional API secret

    Returns:
        Response dict with unlock confirmation
    """
    public_key, secret_key = _resolve_credentials(public_key, secret_key)
    return _make_request_with_sudo("POST", f"/images/{image_id}/unlock", public_key, secret_key, {})


def set_image_visibility(
    image_id: str,
    visibility: str,
    public_key: Optional[str] = None,
    secret_key: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Set image visibility.

    Args:
        image_id: Image ID
        visibility: One of "private", "unlisted", or "public"
            - private: Only owner can see/use
            - unlisted: Hidden but can be shared via trusted_keys
            - public: Visible to all users (marketplace)
        public_key: Optional API key
        secret_key: Optional API secret

    Returns:
        Response dict with updated image info
    """
    public_key, secret_key = _resolve_credentials(public_key, secret_key)
    return _make_request("POST", f"/images/{image_id}/visibility", public_key, secret_key, {"visibility": visibility})


def grant_image_access(
    image_id: str,
    trusted_api_key: str,
    public_key: Optional[str] = None,
    secret_key: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Grant access to an image for another API key.

    Works for private/unlisted images. Public images are already accessible.

    Args:
        image_id: Image ID
        trusted_api_key: Public key of the user to grant access
        public_key: Optional API key
        secret_key: Optional API secret

    Returns:
        Response dict with updated trusted_keys list
    """
    public_key, secret_key = _resolve_credentials(public_key, secret_key)
    return _make_request("POST", f"/images/{image_id}/grant", public_key, secret_key, {"trusted_api_key": trusted_api_key})


def revoke_image_access(
    image_id: str,
    trusted_api_key: str,
    public_key: Optional[str] = None,
    secret_key: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Revoke access to an image from another API key.

    Args:
        image_id: Image ID
        trusted_api_key: Public key of the user to revoke access
        public_key: Optional API key
        secret_key: Optional API secret

    Returns:
        Response dict with updated trusted_keys list
    """
    public_key, secret_key = _resolve_credentials(public_key, secret_key)
    return _make_request("POST", f"/images/{image_id}/revoke", public_key, secret_key, {"trusted_api_key": trusted_api_key})


def list_image_trusted(
    image_id: str,
    public_key: Optional[str] = None,
    secret_key: Optional[str] = None,
) -> Dict[str, Any]:
    """
    List all API keys that have access to an image.

    Args:
        image_id: Image ID
        public_key: Optional API key
        secret_key: Optional API secret

    Returns:
        Response dict containing:
        - trusted_keys: List of API public keys with access
    """
    public_key, secret_key = _resolve_credentials(public_key, secret_key)
    return _make_request("GET", f"/images/{image_id}/trusted", public_key, secret_key)


def transfer_image(
    image_id: str,
    to_api_key: str,
    public_key: Optional[str] = None,
    secret_key: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Transfer image ownership to another API key.

    This is a database-only operation - the LXD image stays on the same node.

    Args:
        image_id: Image ID to transfer
        to_api_key: Public key of the recipient
        public_key: Optional API key
        secret_key: Optional API secret

    Returns:
        Response dict with updated owner info
    """
    public_key, secret_key = _resolve_credentials(public_key, secret_key)
    return _make_request("POST", f"/images/{image_id}/transfer", public_key, secret_key, {"to_api_key": to_api_key})


def spawn_from_image(
    image_id: str,
    name: Optional[str] = None,
    ports: Optional[List[int]] = None,
    bootstrap: Optional[str] = None,
    network_mode: str = "zerotrust",
    public_key: Optional[str] = None,
    secret_key: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Create a new service from an image.

    Spawns a new container using the image as the base.

    Args:
        image_id: Image ID to spawn from
        name: Optional service name
        ports: Optional list of ports to expose
        bootstrap: Optional bootstrap script (image may already have app)
        network_mode: "zerotrust" or "semitrusted"
        public_key: Optional API key
        secret_key: Optional API secret

    Returns:
        Response dict containing service info with source_image field

    Example:
        >>> svc = spawn_from_image("unsb-image-xxxx", name="my-app", ports=[8080])
        >>> print(svc["service_id"])
    """
    public_key, secret_key = _resolve_credentials(public_key, secret_key)
    data: Dict[str, Any] = {"network_mode": network_mode}
    if name:
        data["name"] = name
    if ports:
        data["ports"] = ports
    if bootstrap:
        data["bootstrap"] = bootstrap
    return _make_request("POST", f"/images/{image_id}/spawn", public_key, secret_key, data)


def clone_image(
    image_id: str,
    name: Optional[str] = None,
    description: Optional[str] = None,
    public_key: Optional[str] = None,
    secret_key: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Clone an image to create a copy owned by you.

    The clone inherits the source image's content but:
    - Gets a new unique ID
    - Is owned by the requesting user
    - Starts as private visibility
    - Has empty trusted_keys

    Args:
        image_id: Image ID to clone
        name: Optional name for the clone
        description: Optional description
        public_key: Optional API key
        secret_key: Optional API secret

    Returns:
        Response dict containing new image info
    """
    public_key, secret_key = _resolve_credentials(public_key, secret_key)
    data: Dict[str, Any] = {}
    if name:
        data["name"] = name
    if description:
        data["description"] = description
    return _make_request("POST", f"/images/{image_id}/clone", public_key, secret_key, data)


# =============================================================================
# Key Validation
# =============================================================================



def validate_keys(
    public_key: Optional[str] = None,
    secret_key: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Validate API keys against the portal.

    Checks if the keys are valid, not expired, and not suspended.

    Args:
        public_key: Optional API key
        secret_key: Optional API secret

    Returns:
        Response dict with validation result:
        - valid: True if keys are valid
        - tier: Account tier level
        - expires_at: Expiration timestamp (if applicable)
        - reason: Reason for invalid status (if applicable)

    Raises:
        requests.RequestException: Network errors
        ValueError: Invalid response format
        CredentialsError: Missing credentials
    """
    public_key, secret_key = _resolve_credentials(public_key, secret_key)

    url = f"{API_BASE}/keys/validate"
    timestamp = int(time.time())
    body = ""

    signature = _sign_request(secret_key, timestamp, "POST", "/keys/validate", body)

    headers = {
        "Authorization": f"Bearer {public_key}",
        "X-Timestamp": str(timestamp),
        "X-Signature": signature,
        "Content-Type": "application/json",
    }

    response = requests.post(url, headers=headers, data=body, timeout=30)
    response.raise_for_status()
    return response.json()


# =============================================================================
# Image Generation
# =============================================================================


def image(
    prompt: str,
    *,
    model: str = None,
    size: str = "1024x1024",
    quality: str = "standard",
    n: int = 1,
    public_key: str = None,
    secret_key: str = None,
) -> Dict[str, Any]:
    """
    Generate images from text prompt using AI.

    Args:
        prompt: Text description of the image to generate
        model: Model to use (optional, uses default)
        size: Image size (e.g., "1024x1024", "512x512")
        quality: "standard" or "hd"
        n: Number of images to generate
        public_key: API public key (optional)
        secret_key: API secret key (optional)

    Returns:
        dict with keys: images (list of base64 or URLs), created_at

    Example:
        >>> result = image("A sunset over mountains")
        >>> print(result["images"][0])
    """
    public_key, secret_key = _resolve_credentials(public_key, secret_key)
    payload = {
        "prompt": prompt,
        "size": size,
        "quality": quality,
        "n": n,
    }
    if model:
        payload["model"] = model

    return _make_request("POST", "/image", public_key, secret_key, payload)


# =============================================================================
# PaaS Logs Functions
# =============================================================================

_last_error: Optional[str] = None


PORTAL_BASE = (os.environ.get("UNSANDBOX_PORTAL_URL") or "https://unsandbox.com").rstrip("/")

# Platform logs live on the portal (RequireLogAccess, partner keys), not the API host:
# GET {PORTAL_BASE}/logs/{api|portal|pool/NODE|all}[/stream]. HMAC signs the path only.


def _logs_path(source: str, stream: bool = False) -> str:
    source = (source or "all").strip("/") or "all"
    return f"/logs/{source}" + ("/stream" if stream else "")


def _logs_query(**params) -> str:
    from urllib.parse import quote
    parts = [f"{k}={quote(str(v), safe='-_.~')}" for k, v in params.items() if v not in (None, "")]
    return ("?" + "&".join(parts)) if parts else ""


def _portal_logs_get(path: str, query: str, public_key: str, secret_key: str, stream: bool = False):
    """Signed GET to the portal; returns the requests.Response (caller checks status)."""
    _check_requests()
    timestamp = int(time.time())
    headers = {
        "Authorization": f"Bearer {public_key}",
        "X-Timestamp": str(timestamp),
        "X-Signature": _sign_request(secret_key, timestamp, "GET", path, None),
    }
    if stream:
        headers["Accept"] = "text/event-stream"
        return requests.get(f"{PORTAL_BASE}{path}{query}", headers=headers, stream=True,
                            timeout=(30, 120))
    return requests.get(f"{PORTAL_BASE}{path}{query}", headers=headers, timeout=120)


def logs_fetch(
    source: str = "all",
    lines: int = 100,
    since: str = "5m",
    grep: Optional[str] = None,
    public_key: Optional[str] = None,
    secret_key: Optional[str] = None,
    level: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Fetch batch logs from the portal (partner log access).

    Args:
        source: Log source - "all", "api", "portal", "pool/cammy", "pool/ai"
        lines: Number of lines to fetch (1-10000)
        since: Time window - "1m", "5m", "1h", "1d"
        grep: Optional filter pattern
        public_key: Optional API key
        secret_key: Optional API secret
        level: Optional minimum level (debug, info, notice, warning, err, crit)

    Returns:
        Dict: {"source","lines":[...]} or, for "all", {"sources","lines":[{"source","line"}]}

    Raises:
        requests.RequestException: Network errors / non-2xx (HTTPError)
        CredentialsError: Missing credentials
    """
    public_key, secret_key = _resolve_credentials(public_key, secret_key)
    lines = max(1, min(10000, int(lines)))
    response = _portal_logs_get(_logs_path(source),
                                _logs_query(lines=lines, since=since, grep=grep, level=level),
                                public_key, secret_key)
    response.raise_for_status()
    return response.json()


def _sse_entries(response):
    """Yield (source, line) for each SSE data event; comments/keepalives yield nothing."""
    for raw in response.iter_lines():
        if not raw:
            continue
        decoded = raw.decode("utf-8", errors="replace")
        if not decoded.startswith("data: "):
            continue
        data = decoded[6:]
        if data == ":keepalive":
            continue
        try:
            entry = json.loads(data)
        except json.JSONDecodeError:
            yield (None, data)
            continue
        if isinstance(entry, dict):
            if "line" in entry:
                yield (entry.get("source"), entry["line"])
        else:
            yield (None, data)


def logs_stream(
    source: str = "all",
    grep: Optional[str] = None,
    callback=None,
    public_key: Optional[str] = None,
    secret_key: Optional[str] = None,
    level: Optional[str] = None,
) -> None:
    """
    Stream logs from the portal via Server-Sent Events (one connection).

    Blocks until interrupted or server closes connection.

    Args:
        source: Log source - "all", "api", "portal", "pool/cammy", "pool/ai"
        grep: Optional filter pattern
        callback: Function called for each log line (signature: callback(source, line))
        public_key: Optional API key
        secret_key: Optional API secret
        level: Optional minimum level

    Raises:
        requests.RequestException: Network errors / non-2xx (HTTPError)
        CredentialsError: Missing credentials
    """
    public_key, secret_key = _resolve_credentials(public_key, secret_key)
    with _portal_logs_get(_logs_path(source, stream=True), _logs_query(grep=grep, level=level),
                          public_key, secret_key, stream=True) as response:
        response.raise_for_status()
        for src, line in _sse_entries(response):
            src = src or source
            if callback:
                callback(src, line)
            else:
                print(f"{src:<12} {line}", flush=True)


def _log_cursor_path() -> Path:
    return Path(os.environ.get("HOME", str(Path.home()))) / ".unsandbox" / "paas_log_cursor"


def _read_log_cursor() -> Optional[str]:
    try:
        ts = int(_log_cursor_path().read_text().strip())
    except (OSError, ValueError):
        return None
    if ts <= 0:
        return None
    return f"{max(5, int(time.time()) - ts)}s"


def _write_log_cursor() -> None:
    try:
        p = _log_cursor_path()
        p.parent.mkdir(mode=0o700, exist_ok=True)
        p.write_text(f"{int(time.time())}\n")
    except OSError:
        pass


def _http_error_text(response) -> str:
    try:
        err = response.json().get("error")
    except Exception:
        err = None
    return f"Error: HTTP {response.status_code}" + (f" \u2014 {err}" if err else "")


_PAAS_LOGS_HELP = """Usage: un.py paas logs [options]

Fetch or stream PaaS production logs.

Sources:
  --all              All sources (default)
  --api              API server logs
  --portal           Portal server logs
  --pool NODE        Pool node logs (e.g., cammy, ai)
  --source SOURCE    Source string (api, portal, pool/cammy, all)

Options:
  --lines N, -n N    Number of lines (default: 100, max: 10000)
  --since TIME       Time window: 1m, 5m, 15m, 1h, 6h, 1d (default: 5m)
  --grep PATTERN     Filter log lines by pattern
  --level LVL, -l    Min log level: debug, info, notice, warning, err, crit
  --follow, -f       Follow logs in real-time (Ctrl+C to stop)
  --json             Output raw JSON response
  -p KEY             Public key
  -k KEY             Secret key
  -h                 Show this help

Requires a partner API key with log access enabled."""


def _paas_command(argv: List[str]) -> int:
    """`un paas logs ...` - mirrors the C client's flags, output and exit codes."""
    if not argv:
        print("Usage: un.py paas <command> [options]\n\nCommands:\n"
              "  logs    Fetch or stream production logs", file=sys.stderr)
        return 1
    if argv[0] != "logs":
        print(f"Error: Unknown paas command '{argv[0]}'", file=sys.stderr)
        return 1
    source, lines, since, grep, level = "all", 100, None, None, None
    follow = as_json = False
    pk = sk = None
    account = None
    args = argv[1:]
    i = 0
    def nxt():
        nonlocal i
        i += 1
        return args[i] if i < len(args) else None
    def clamp(v):
        try:
            n = int(v)
        except (TypeError, ValueError):
            n = 0
        return max(1, min(10000, n))
    while i < len(args):
        a = args[i]
        has = i + 1 < len(args)
        if a == "-p" and has: pk = nxt()
        elif a == "-k" and has: sk = nxt()
        elif a == "--account" and has:
            try: account = int(nxt())
            except ValueError: account = 0
        elif a == "--source" and has: source = nxt()
        elif a == "--all": source = "all"
        elif a == "--api": source = "api"
        elif a == "--portal": source = "portal"
        elif a == "--pool" and has: source = f"pool/{nxt()}"
        elif a in ("--lines", "-n") and has: lines = clamp(nxt())
        elif a == "--since" and has: since = nxt()
        elif a == "--grep" and has: grep = nxt()
        elif a in ("--level", "-l") and has: level = nxt()
        elif a in ("--follow", "-f"): follow = True
        elif a == "--json": as_json = True
        elif a in ("-h", "--help"):
            print(_PAAS_LOGS_HELP, file=sys.stderr)
            return 0
        i += 1
    try:
        public_key, secret_key = _resolve_credentials(pk, sk, account)
    except CredentialsError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    if since is None:
        since = _read_log_cursor() or "5m"
    _check_requests()
    if follow:
        return _paas_logs_follow(source, grep, level, public_key, secret_key)
    try:
        response = _portal_logs_get(_logs_path(source),
                                    _logs_query(lines=lines, since=since, grep=grep, level=level),
                                    public_key, secret_key)
    except requests.exceptions.RequestException as e:
        print(f"Error: Network error fetching logs: {e}", file=sys.stderr)
        return 1
    if response.status_code != 200:
        print(_http_error_text(response), file=sys.stderr)
        return 1
    _write_log_cursor()
    if as_json:
        print(response.text)
        return 0
    try:
        data = response.json()
        entries = data["lines"]
    except Exception:
        print("Error: Invalid response format", file=sys.stderr)
        return 1
    for e in entries:
        if isinstance(e, dict):
            if e.get("source") is not None:
                print(f"{e['source']:<12} {e.get('line', '')}")
            else:
                print(e.get("line", ""))
        else:
            print(e)
    return 0


def _paas_logs_follow(source, grep, level, public_key, secret_key) -> int:
    print(f"Streaming {source} logs... (Ctrl+C to stop)", file=sys.stderr)
    path = _logs_path(source, stream=True)
    delay, last_recv, reconnect = 2, 0.0, False
    try:
        while True:
            since = None
            if reconnect and last_recv > 0:
                since = f"{max(10, int(time.time() - last_recv) + 5)}s"
            connected = time.time()
            status = None
            try:
                # fresh signature each connection (timestamps expire)
                with _portal_logs_get(path, _logs_query(grep=grep, level=level, since=since),
                                      public_key, secret_key, stream=True) as response:
                    status = response.status_code
                    if status in (401, 403):
                        print(f"Error: HTTP {status} (auth failed)", file=sys.stderr)
                        return 1
                    if status == 200:
                        for src, line in _sse_entries(response):
                            if src is not None:
                                print(f"{src:<12} {line}", flush=True)
                            else:
                                print(line, flush=True)
                            last_recv = time.time()
                    elif status < 500:
                        print(_http_error_text(response), file=sys.stderr)
                        return 1
            except requests.exceptions.RequestException:
                pass  # dropped / timed out: reconnect silently
            if time.time() - connected > 30:
                delay = 2
            time.sleep(delay)
            delay = min(30, delay * 2)
            reconnect = True
    except KeyboardInterrupt:
        return 0


# =============================================================================
# Utility Functions
# =============================================================================

SDK_VERSION = "4.2.0"


def version() -> str:
    """
    Get the SDK version string.

    Returns:
        Version string (e.g., "4.2.0")
    """
    return SDK_VERSION


def health_check() -> bool:
    """
    Check if the API is healthy and responding.

    Returns:
        True if API is healthy, False otherwise
    """
    global _last_error
    try:
        response = requests.get(f"{API_BASE}/health", timeout=10)
        if response.status_code == 200:
            return True
        _last_error = f"Health check failed: HTTP {response.status_code}"
        return False
    except Exception as e:
        _last_error = f"Health check failed: {str(e)}"
        return False


def last_error() -> Optional[str]:
    """
    Get the last error message.

    Returns:
        Last error message or None
    """
    return _last_error


def hmac_sign(secret_key: str, message: str) -> str:
    """
    Sign a message using HMAC-SHA256.

    This is the underlying signing function used for request authentication.
    Exposed for testing and debugging purposes.

    Args:
        secret_key: The secret key for signing
        message: The message to sign

    Returns:
        64-character lowercase hex string
    """
    return hmac.new(
        secret_key.encode(),
        message.encode(),
        hashlib.sha256,
    ).hexdigest()


# =============================================================================
# CLI Implementation
# =============================================================================

import sys
import argparse


def _parse_env_file(file_path: str) -> Dict[str, str]:
    """Parse a .env file into a dictionary."""
    env_dict = {}
    try:
        with open(file_path, "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, _, value = line.partition("=")
                    # Handle quoted values
                    value = value.strip()
                    if (value.startswith('"') and value.endswith('"')) or \
                       (value.startswith("'") and value.endswith("'")):
                        value = value[1:-1]
                    env_dict[key.strip()] = value
    except Exception as e:
        print(f"Error: Failed to parse env file: {e}", file=sys.stderr)
        sys.exit(1)
    return env_dict


def _format_list_output(items: List[Dict[str, Any]], resource_type: str) -> str:
    """Format list output in table format."""
    if not items:
        return f"No {resource_type}s found."

    # Determine columns based on resource type
    if resource_type == "session":
        headers = ["ID", "STATUS", "SHELL", "CREATED"]
        rows = []
        for item in items:
            rows.append([
                item.get("id", item.get("session_id", ""))[:36],
                item.get("status", "unknown"),
                item.get("shell", "bash"),
                item.get("created_at", "")[:19] if item.get("created_at") else "",
            ])
    elif resource_type == "service":
        headers = ["ID", "NAME", "STATUS", "PORTS", "CREATED"]
        rows = []
        for item in items:
            ports = item.get("ports", [])
            ports_str = ",".join(str(p) for p in ports) if ports else ""
            rows.append([
                item.get("id", item.get("service_id", ""))[:36],
                item.get("name", "")[:20],
                item.get("status", "unknown"),
                ports_str[:15],
                item.get("created_at", "")[:19] if item.get("created_at") else "",
            ])
    elif resource_type == "snapshot":
        headers = ["ID", "NAME", "TYPE", "SIZE", "CREATED"]
        rows = []
        for item in items:
            rows.append([
                item.get("id", item.get("snapshot_id", ""))[:36],
                item.get("name", "")[:20],
                item.get("source_type", "unknown"),
                item.get("size", ""),
                item.get("created_at", "")[:19] if item.get("created_at") else "",
            ])
    elif resource_type == "image":
        headers = ["ID", "NAME", "VISIBILITY", "SOURCE", "CREATED"]
        rows = []
        for item in items:
            rows.append([
                item.get("id", item.get("image_id", ""))[:36],
                item.get("name", "")[:20],
                item.get("visibility", "private"),
                item.get("source_type", "")[:10],
                item.get("created_at", "")[:19] if item.get("created_at") else "",
            ])
    else:
        headers = ["ID", "STATUS"]
        rows = [[str(item.get("id", "")), str(item.get("status", ""))] for item in items]

    # Calculate column widths
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))

    # Build output
    lines = []
    header_line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    lines.append(header_line)
    for row in rows:
        line = "  ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(row))
        lines.append(line)

    return "\n".join(lines)


def _build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the CLI."""
    parser = argparse.ArgumentParser(
        prog="un.py",
        description="Unsandbox CLI - Execute code in secure containers",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python un.py script.py                    Execute Python script
  python un.py -s bash 'echo hello'         Inline bash command
  python un.py session --list               List active sessions
  python un.py service --list               List all services
  python un.py snapshot --list              List all snapshots
  python un.py key                          Check API key
  python un.py paas logs --api -n 500       Platform logs (partner keys)
  python un.py languages                    List available languages
  python un.py languages --json             List languages as JSON
""",
    )

    # Global options
    parser.add_argument("-s", "--shell", metavar="LANG",
                        help="Language for inline code execution")
    parser.add_argument("-e", "--env", action="append", metavar="KEY=VAL",
                        help="Set environment variable (can be used multiple times)")
    parser.add_argument("-f", "--file", action="append", metavar="FILE",
                        help="Add input file to /tmp/ (can be used multiple times)")
    parser.add_argument("-F", "--file-path", action="append", metavar="FILE",
                        help="Add input file with path preserved")
    parser.add_argument("-a", "--artifacts", action="store_true",
                        help="Return compiled artifacts")
    parser.add_argument("-o", "--output", metavar="DIR",
                        help="Output directory for artifacts")
    parser.add_argument("-p", "--public-key", metavar="KEY",
                        help="API public key")
    parser.add_argument("-k", "--secret-key", metavar="KEY",
                        help="API secret key")
    parser.add_argument("-n", "--network", choices=["zerotrust", "semitrusted"],
                        default="zerotrust", help="Network mode (default: zerotrust)")
    parser.add_argument("-v", "--vcpu", type=int, default=1, choices=range(1, 9),
                        metavar="N", help="vCPU count (1-8, default: 1)")
    parser.add_argument("-y", "--yes", action="store_true",
                        help="Skip confirmation prompts")
    parser.add_argument("--account", type=int, metavar="N",
                        help="Select account row N from accounts.csv (overrides env vars)")

    # Subcommands
    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # Session subcommand
    session_parser = subparsers.add_parser("session", help="Manage interactive sessions")
    session_group = session_parser.add_mutually_exclusive_group()
    session_group.add_argument("-l", "--list", action="store_true",
                               help="List active sessions")
    session_group.add_argument("--attach", metavar="ID",
                               help="Reconnect to existing session")
    session_group.add_argument("--kill", metavar="ID",
                               help="Terminate a session")
    session_group.add_argument("--freeze", metavar="ID",
                               help="Pause session")
    session_group.add_argument("--unfreeze", metavar="ID",
                               help="Resume session")
    session_group.add_argument("--boost", metavar="ID",
                               help="Add resources to session")
    session_group.add_argument("--unboost", metavar="ID",
                               help="Remove boost from session")
    session_group.add_argument("--snapshot", metavar="ID",
                               help="Create snapshot of session")
    session_parser.add_argument("--shell", metavar="SHELL",
                                help="Shell/REPL to use (default: bash)")
    session_parser.add_argument("--tmux", action="store_true",
                                help="Enable persistence with tmux")
    session_parser.add_argument("--screen", action="store_true",
                                help="Enable persistence with screen")
    session_parser.add_argument("--snapshot-name", metavar="NAME",
                                help="Name for snapshot")
    session_parser.add_argument("--hot", action="store_true",
                                help="Live snapshot (no freeze)")
    session_parser.add_argument("--audit", action="store_true",
                                help="Record session")

    # Service subcommand
    service_parser = subparsers.add_parser("service", help="Manage persistent services")
    service_group = service_parser.add_mutually_exclusive_group()
    service_group.add_argument("-l", "--list", action="store_true",
                               help="List all services")
    service_group.add_argument("--info", metavar="ID",
                               help="Get service details")
    service_group.add_argument("--logs", metavar="ID",
                               help="Get all logs")
    service_group.add_argument("--tail", metavar="ID",
                               help="Get last 9000 lines of logs")
    service_group.add_argument("--freeze", metavar="ID",
                               help="Pause service")
    service_group.add_argument("--unfreeze", metavar="ID",
                               help="Resume service")
    service_group.add_argument("--destroy", metavar="ID",
                               help="Delete service")
    service_group.add_argument("--lock", metavar="ID",
                               help="Prevent deletion")
    service_group.add_argument("--unlock", metavar="ID",
                               help="Allow deletion")
    service_group.add_argument("--add-domain", metavar="ID",
                               help="Add domains (with --domains)")
    service_group.add_argument("--remove-domain", metavar="ID",
                               help="Remove domains (with --domains)")
    service_group.add_argument("--set-domains", metavar="ID",
                               help="Replace all domains (with --domains)")
    service_group.add_argument("--resize", metavar="ID",
                               help="Resize service (with --vcpu)")
    service_group.add_argument("--redeploy", metavar="ID",
                               help="Re-run bootstrap")
    service_group.add_argument("--execute", nargs=2, metavar=("ID", "CMD"),
                               help="Run command in service")
    service_group.add_argument("--snapshot", metavar="ID",
                               help="Create snapshot of service")
    service_parser.add_argument("--name", metavar="NAME",
                                help="Service name (creates new)")
    service_parser.add_argument("--ports", metavar="PORTS",
                                help="Comma-separated ports")
    service_parser.add_argument("--domains", metavar="DOMAINS",
                                help="Custom domains")
    service_parser.add_argument("--type", metavar="TYPE", dest="service_type",
                                help="Service type (minecraft, tcp, udp)")
    service_parser.add_argument("--bootstrap", metavar="CMD",
                                help="Bootstrap command")
    service_parser.add_argument("--bootstrap-file", metavar="FILE",
                                help="Bootstrap from file")
    service_parser.add_argument("--env-file", metavar="FILE",
                                help="Load env from .env file")
    service_parser.add_argument("--snapshot-name", metavar="NAME",
                                help="Name for snapshot")
    service_parser.add_argument("--hot", action="store_true",
                                help="Live snapshot (no freeze)")
    service_parser.add_argument("--golden-image", metavar="IMAGE",
                                help="LXD image alias (e.g., golden-alpine-3.24)")
    service_parser.add_argument("-f", "--file", action="append", dest="files",
                                metavar="FILE",
                                help="Input file to upload (written to /tmp/ in container)")

    # Service env subcommand
    service_env_parser = subparsers.add_parser("service-env",
                                                help="Manage service environment vault")
    service_env_parser.add_argument("action", choices=["status", "set", "export", "delete"],
                                    help="Environment action")
    service_env_parser.add_argument("service_id", metavar="ID",
                                    help="Service ID")
    service_env_parser.add_argument("--env-file", metavar="FILE",
                                    help="Load env from .env file (for set)")

    # Snapshot subcommand
    snapshot_parser = subparsers.add_parser("snapshot", help="Manage snapshots")
    snapshot_group = snapshot_parser.add_mutually_exclusive_group()
    snapshot_group.add_argument("-l", "--list", action="store_true",
                                help="List all snapshots")
    snapshot_group.add_argument("--info", metavar="ID",
                                help="Get snapshot details")
    snapshot_group.add_argument("--delete", metavar="ID",
                                help="Delete snapshot")
    snapshot_group.add_argument("--lock", metavar="ID",
                                help="Prevent deletion")
    snapshot_group.add_argument("--unlock", metavar="ID",
                                help="Allow deletion")
    snapshot_group.add_argument("--clone", metavar="ID",
                                help="Clone snapshot")
    snapshot_parser.add_argument("--type", choices=["session", "service"],
                                 dest="clone_type", help="Clone type")
    snapshot_parser.add_argument("--name", metavar="NAME",
                                 help="Name for cloned resource")
    snapshot_parser.add_argument("--shell", metavar="SHELL",
                                 help="Shell for cloned session")
    snapshot_parser.add_argument("--ports", metavar="PORTS",
                                 help="Ports for cloned service")

    # Image subcommand
    image_parser = subparsers.add_parser("image", help="Manage images")
    image_group = image_parser.add_mutually_exclusive_group()
    image_group.add_argument("-l", "--list", action="store_true",
                             help="List all images")
    image_group.add_argument("--info", metavar="ID",
                             help="Get image details")
    image_group.add_argument("--delete", metavar="ID",
                             help="Delete image")
    image_group.add_argument("--lock", metavar="ID",
                             help="Prevent deletion")
    image_group.add_argument("--unlock", metavar="ID",
                             help="Allow deletion")
    image_group.add_argument("--publish", metavar="ID",
                             help="Publish image from service/snapshot (requires --source-type)")
    image_group.add_argument("--visibility", nargs=2, metavar=("ID", "MODE"),
                             help="Set visibility (private, unlisted, public)")
    image_group.add_argument("--spawn", metavar="ID",
                             help="Spawn new service from image")
    image_group.add_argument("--clone", metavar="ID",
                             help="Clone an image")
    image_parser.add_argument("--source-type", metavar="TYPE",
                              choices=["service", "snapshot"],
                              help="Source type for publish (service or snapshot)")
    image_parser.add_argument("--name", metavar="NAME",
                              help="Name for spawned service or cloned image")
    image_parser.add_argument("--ports", metavar="PORTS",
                              help="Ports for spawned service (comma-separated)")

    # Key subcommand
    key_parser = subparsers.add_parser("key", help="Check API key validity, or rotate its secret")
    key_parser.add_argument("--rotate", action="store_true",
                            help="Rotate this key's secret (sudo OTP); updates accounts.csv in place")
    key_parser.add_argument("--otp", metavar="CODE",
                            help="Finish a rotation started without a terminal")

    # Languages subcommand
    languages_parser = subparsers.add_parser("languages", help="List available languages")
    languages_parser.add_argument("--json", action="store_true",
                                   help="Output as JSON array")

    # Positional argument for source file or inline code
    parser.add_argument("source", nargs="?",
                        help="Source file or inline code (with -s)")

    return parser


def cli_main():
    """Main entry point for CLI."""
    if len(sys.argv) > 1 and sys.argv[1] == "paas":
        sys.exit(_paas_command(sys.argv[2:]))
    parser = _build_parser()
    args = parser.parse_args()

    # Resolve credentials
    try:
        public_key, secret_key = _resolve_credentials(
            args.public_key, args.secret_key, args.account
        )
    except CredentialsError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(3)

    try:
        # Handle subcommands
        if args.command == "session":
            _handle_session_command(args, public_key, secret_key)
        elif args.command == "service":
            _handle_service_command(args, public_key, secret_key)
        elif args.command == "service-env":
            _handle_service_env_command(args, public_key, secret_key)
        elif args.command == "snapshot":
            _handle_snapshot_command(args, public_key, secret_key)
        elif args.command == "image":
            _handle_image_command(args, public_key, secret_key)
        elif args.command == "key":
            if getattr(args, "rotate", False):
                _handle_key_rotate(args, public_key, secret_key)
            else:
                _handle_key_command(public_key, secret_key)
        elif args.command == "languages":
            _handle_languages_command(args, public_key, secret_key)
        elif args.source or args.shell:
            _handle_execute_command(args, public_key, secret_key)
        else:
            parser.print_help()
            sys.exit(2)
    except CredentialsError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(3)
    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 401:
            print("Error: Authentication failed", file=sys.stderr)
            sys.exit(3)
        print(f"Error: API error - {e}", file=sys.stderr)
        sys.exit(4)
    except requests.exceptions.RequestException as e:
        print(f"Error: Network error - {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def _handle_execute_command(args, public_key: str, secret_key: str):
    """Handle code execution command."""
    # Determine language and code
    if args.shell:
        # Inline code mode
        if not args.source:
            print("Error: Code required with -s/--shell", file=sys.stderr)
            sys.exit(2)
        language = args.shell
        code = args.source
    else:
        # File mode
        if not args.source:
            print("Error: Source file required", file=sys.stderr)
            sys.exit(2)

        # Detect language from filename
        language = detect_language(args.source)
        if not language:
            print(f"Error: Cannot detect language from '{args.source}'", file=sys.stderr)
            sys.exit(2)

        # Read source file
        try:
            with open(args.source, "r") as f:
                code = f.read()
        except FileNotFoundError:
            print(f"Error: File not found: {args.source}", file=sys.stderr)
            sys.exit(1)
        except Exception as e:
            print(f"Error: Failed to read file: {e}", file=sys.stderr)
            sys.exit(1)

    # Execute code
    result = execute_code(language, code, public_key, secret_key)

    # Output result
    stdout = result.get("stdout", "")
    stderr = result.get("stderr", "")
    exit_code = result.get("exit_code", 0)
    execution_time = result.get("execution_time_ms", 0)

    if stdout:
        print(stdout, end="")
        if not stdout.endswith("\n"):
            print()

    if stderr:
        print(stderr, end="", file=sys.stderr)
        if not stderr.endswith("\n"):
            print(file=sys.stderr)

    print("---")
    print(f"Exit code: {exit_code}")
    print(f"Execution time: {execution_time}ms")

    sys.exit(exit_code if exit_code else 0)


def _handle_session_command(args, public_key: str, secret_key: str):
    """Handle session subcommand."""
    if args.list:
        sessions = list_sessions(public_key, secret_key)
        print(_format_list_output(sessions, "session"))
    elif args.attach:
        # Get session info for attach
        session = get_session(args.attach, public_key, secret_key)
        print(f"Session ID: {session.get('id', session.get('session_id', ''))}")
        print(f"Status: {session.get('status', 'unknown')}")
        print(f"WebSocket URL: wss://api.unsandbox.com/sessions/{args.attach}/shell")
        print("\nUse a WebSocket client to connect interactively.")
    elif args.kill:
        result = delete_session(args.kill, public_key, secret_key)
        print(f"Session {args.kill} terminated")
    elif args.freeze:
        result = freeze_session(args.freeze, public_key, secret_key)
        print(f"Session {args.freeze} frozen")
    elif args.unfreeze:
        result = unfreeze_session(args.unfreeze, public_key, secret_key)
        print(f"Session {args.unfreeze} unfrozen")
    elif args.boost:
        result = boost_session(args.boost, public_key, secret_key)
        print(f"Session {args.boost} boosted")
    elif args.unboost:
        result = unboost_session(args.unboost, public_key, secret_key)
        print(f"Session {args.unboost} unboosted")
    elif args.snapshot:
        snapshot_id = session_snapshot(
            args.snapshot, public_key, secret_key,
            name=args.snapshot_name,
            ephemeral=not args.hot
        )
        print(f"Snapshot created: {snapshot_id}")
    else:
        # Create new session
        multiplexer = None
        if args.tmux:
            multiplexer = "tmux"
        elif args.screen:
            multiplexer = "screen"

        result = create_session(
            shell=args.shell,
            network_mode="semitrusted" if hasattr(args, 'network') and args.network == "semitrusted" else "zerotrust",
            public_key=public_key,
            secret_key=secret_key,
            multiplexer=multiplexer,
        )

        session_id = result.get("session_id", result.get("id", ""))
        print(f"Session created: {session_id}")
        print(f"WebSocket URL: wss://api.unsandbox.com/sessions/{session_id}/shell")


def _handle_service_command(args, public_key: str, secret_key: str):
    """Handle service subcommand."""
    if args.list:
        services = list_services(public_key, secret_key)
        print(_format_list_output(services, "service"))
    elif args.info:
        service = get_service(args.info, public_key, secret_key)
        print(json.dumps(service, indent=2))
    elif args.logs:
        result = get_service_logs(args.logs, all_logs=True, public_key=public_key, secret_key=secret_key)
        print(result.get("log", ""))
    elif args.tail:
        result = get_service_logs(args.tail, all_logs=False, public_key=public_key, secret_key=secret_key)
        print(result.get("log", ""))
    elif args.freeze:
        result = freeze_service(args.freeze, public_key, secret_key)
        print(f"Service {args.freeze} frozen")
    elif args.unfreeze:
        result = unfreeze_service(args.unfreeze, public_key, secret_key)
        print(f"Service {args.unfreeze} unfrozen")
    elif args.destroy:
        result = delete_service(args.destroy, public_key, secret_key)
        print(f"Service {args.destroy} destroyed")
    elif args.lock:
        result = lock_service(args.lock, public_key, secret_key)
        print(f"Service {args.lock} locked")
    elif args.unlock:
        result = unlock_service(args.unlock, public_key, secret_key)
        print(f"Service {args.unlock} unlocked")
    elif getattr(args, 'add_domain', None):
        if not args.domains:
            print("Error: --domains required with --add-domain", file=sys.stderr)
            sys.exit(2)
        domains = [d.strip() for d in args.domains.split(",")]
        result = update_service_domains(args.add_domain, domains, "add", public_key, secret_key)
        print(f"Domains added to {args.add_domain}")
        print(f"Domains: {result.get('custom_domains', [])}")
    elif getattr(args, 'remove_domain', None):
        if not args.domains:
            print("Error: --domains required with --remove-domain", file=sys.stderr)
            sys.exit(2)
        domains = [d.strip() for d in args.domains.split(",")]
        result = update_service_domains(args.remove_domain, domains, "remove", public_key, secret_key)
        print(f"Domains removed from {args.remove_domain}")
        print(f"Domains: {result.get('custom_domains', [])}")
    elif getattr(args, 'set_domains', None):
        if not args.domains:
            print("Error: --domains required with --set-domains", file=sys.stderr)
            sys.exit(2)
        domains = [d.strip() for d in args.domains.split(",")]
        result = update_service_domains(args.set_domains, domains, "custom_domains", public_key, secret_key)
        print(f"Domains set for {args.set_domains}")
        print(f"Domains: {result.get('custom_domains', [])}")
    elif args.resize:
        vcpu = getattr(args, 'vcpu', 1) or 1
        result = update_service(args.resize, public_key, secret_key, vcpu=vcpu)
        print(f"Service {args.resize} resized to {vcpu} vCPU(s)")
    elif args.redeploy:
        bootstrap = None
        if args.bootstrap_file:
            with open(args.bootstrap_file, "r") as f:
                bootstrap = f.read()
        elif args.bootstrap:
            bootstrap = args.bootstrap

        # Build input_files from -f args
        service_input_files = None
        if getattr(args, 'files', None):
            import base64 as _b64
            service_input_files = []
            for fpath in args.files:
                with open(fpath, "rb") as ff:
                    encoded = _b64.b64encode(ff.read()).decode("ascii")
                service_input_files.append({
                    "filename": os.path.basename(fpath),
                    "content": encoded,
                })

        result = redeploy_service(args.redeploy, bootstrap=bootstrap, input_files=service_input_files, public_key=public_key, secret_key=secret_key)
        print(f"Service {args.redeploy} redeployed")
    elif args.execute:
        service_id, command = args.execute
        result = execute_in_service(service_id, command, public_key=public_key, secret_key=secret_key)
        # Handle async result
        if result.get("job_id"):
            job_result = wait_for_job(result["job_id"], public_key, secret_key)
            stdout = job_result.get("stdout", "")
            stderr = job_result.get("stderr", "")
            if stdout:
                print(stdout, end="")
            if stderr:
                print(stderr, end="", file=sys.stderr)
        else:
            stdout = result.get("stdout", "")
            stderr = result.get("stderr", "")
            if stdout:
                print(stdout, end="")
            if stderr:
                print(stderr, end="", file=sys.stderr)
    elif args.snapshot:
        snapshot_id = service_snapshot(
            args.snapshot, public_key, secret_key,
            name=getattr(args, 'snapshot_name', None)
        )
        print(f"Snapshot created: {snapshot_id}")
    elif args.name:
        # Create new service
        if not args.ports:
            print("Error: --ports required when creating service", file=sys.stderr)
            sys.exit(2)

        ports = [int(p.strip()) for p in args.ports.split(",")]

        bootstrap = None
        if args.bootstrap_file:
            with open(args.bootstrap_file, "r") as f:
                bootstrap = f.read()
        elif args.bootstrap:
            bootstrap = args.bootstrap

        custom_domains = None
        if args.domains:
            custom_domains = [d.strip() for d in args.domains.split(",")]

        # Build input_files from -f args
        service_input_files = None
        if getattr(args, 'files', None):
            import base64 as _b64
            service_input_files = []
            for fpath in args.files:
                with open(fpath, "rb") as ff:
                    encoded = _b64.b64encode(ff.read()).decode("ascii")
                service_input_files.append({
                    "filename": os.path.basename(fpath),
                    "content": encoded,
                })

        result = create_service(
            name=args.name,
            ports=ports,
            bootstrap=bootstrap,
            public_key=public_key,
            secret_key=secret_key,
            custom_domains=custom_domains,
            vcpu=getattr(args, 'vcpu', 1) or 1,
            service_type=args.service_type,
            golden_image=getattr(args, 'golden_image', None),
            input_files=service_input_files,
        )

        service_id = result.get("service_id", result.get("id", ""))
        print(f"Service created: {service_id}")
        print(f"URL: https://{args.name}.on.unsandbox.com")
    else:
        print("Error: No action specified for service command", file=sys.stderr)
        sys.exit(2)


def _handle_service_env_command(args, public_key: str, secret_key: str):
    """Handle service env subcommand."""
    if args.action == "status":
        result = get_service_env(args.service_id, public_key, secret_key)
        print(f"Has vault: {result.get('has_vault', False)}")
        print(f"Variable count: {result.get('count', 0)}")
        if result.get('updated_at'):
            print(f"Updated at: {result.get('updated_at')}")
    elif args.action == "set":
        # Read env from file or stdin
        if args.env_file:
            env_dict = _parse_env_file(args.env_file)
        else:
            # Read from stdin
            print("Enter environment variables (KEY=VALUE), one per line. Ctrl+D to finish:", file=sys.stderr)
            env_dict = {}
            for line in sys.stdin:
                line = line.strip()
                if line and "=" in line:
                    key, _, value = line.partition("=")
                    env_dict[key.strip()] = value.strip()

        result = set_service_env(args.service_id, env_dict, public_key, secret_key)
        print(f"Environment set: {result.get('count', len(env_dict))} variables")
    elif args.action == "export":
        result = export_service_env(args.service_id, public_key, secret_key)
        env_content = result.get("env", "")
        print(env_content)
    elif args.action == "delete":
        result = delete_service_env(args.service_id, public_key=public_key, secret_key=secret_key)
        print(f"Environment vault deleted for service {args.service_id}")


def _handle_snapshot_command(args, public_key: str, secret_key: str):
    """Handle snapshot subcommand."""
    if args.list:
        snapshots = list_snapshots(public_key, secret_key)
        print(_format_list_output(snapshots, "snapshot"))
    elif args.info:
        # Get snapshot info via listing and filtering
        snapshots = list_snapshots(public_key, secret_key)
        snapshot = next((s for s in snapshots if s.get("id") == args.info or s.get("snapshot_id") == args.info), None)
        if snapshot:
            print(json.dumps(snapshot, indent=2))
        else:
            print(f"Error: Snapshot {args.info} not found", file=sys.stderr)
            sys.exit(1)
    elif args.delete:
        result = delete_snapshot(args.delete, public_key, secret_key)
        print(f"Snapshot {args.delete} deleted")
    elif args.lock:
        result = lock_snapshot(args.lock, public_key, secret_key)
        print(f"Snapshot {args.lock} locked")
    elif args.unlock:
        result = unlock_snapshot(args.unlock, public_key, secret_key)
        print(f"Snapshot {args.unlock} unlocked")
    elif args.clone:
        clone_type = args.clone_type or "session"
        ports = None
        if args.ports:
            ports = [int(p.strip()) for p in args.ports.split(",")]

        result = clone_snapshot(
            args.clone,
            clone_type=clone_type,
            name=args.name,
            public_key=public_key,
            secret_key=secret_key,
            shell=args.shell,
            ports=ports,
        )

        if clone_type == "session":
            print(f"Session created: {result.get('session_id', result.get('id', ''))}")
        else:
            print(f"Service created: {result.get('service_id', result.get('id', ''))}")
    else:
        print("Error: No action specified for snapshot command", file=sys.stderr)
        sys.exit(2)


def _handle_image_command(args, public_key: str, secret_key: str):
    """Handle image subcommand."""
    if args.list:
        images = list_images(public_key=public_key, secret_key=secret_key)
        print(_format_list_output(images, "image"))
    elif args.info:
        image = get_image(args.info, public_key=public_key, secret_key=secret_key)
        print(json.dumps(image, indent=2))
    elif args.delete:
        result = delete_image(args.delete, public_key=public_key, secret_key=secret_key)
        print(f"Image {args.delete} deleted")
    elif args.lock:
        result = lock_image(args.lock, public_key=public_key, secret_key=secret_key)
        print(f"Image {args.lock} locked")
    elif args.unlock:
        result = unlock_image(args.unlock, public_key=public_key, secret_key=secret_key)
        print(f"Image {args.unlock} unlocked")
    elif args.publish:
        if not args.source_type:
            print("Error: --source-type required for --publish", file=sys.stderr)
            sys.exit(2)
        result = image_publish(
            source_type=args.source_type,
            source_id=args.publish,
            name=args.name,
            public_key=public_key,
            secret_key=secret_key,
        )
        image_id = result.get("image_id", result.get("id", ""))
        print(f"Image published: {image_id}")
    elif args.visibility:
        image_id, mode = args.visibility
        if mode not in ("private", "unlisted", "public"):
            print(f"Error: Invalid visibility mode '{mode}'. Must be private, unlisted, or public", file=sys.stderr)
            sys.exit(2)
        result = set_image_visibility(image_id, mode, public_key=public_key, secret_key=secret_key)
        print(f"Image {image_id} visibility set to {mode}")
    elif args.spawn:
        if not args.name:
            print("Error: --name required for --spawn", file=sys.stderr)
            sys.exit(2)
        ports = None
        if args.ports:
            ports = [int(p.strip()) for p in args.ports.split(",")]
        result = spawn_from_image(
            args.spawn,
            name=args.name,
            ports=ports,
            public_key=public_key,
            secret_key=secret_key,
        )
        service_id = result.get("service_id", result.get("id", ""))
        print(f"Service spawned: {service_id}")
    elif args.clone:
        result = clone_image(
            args.clone,
            name=args.name,
            public_key=public_key,
            secret_key=secret_key,
        )
        image_id = result.get("image_id", result.get("id", ""))
        print(f"Image cloned: {image_id}")
    else:
        print("Error: No action specified for image command", file=sys.stderr)
        sys.exit(2)


def _handle_key_command(public_key: str, secret_key: str):
    """Handle key validation command (CLI_SPEC.md: `un key` output)."""
    public_key, secret_key = _resolve_credentials(public_key, secret_key)
    timestamp = int(time.time())
    headers = {
        "Authorization": f"Bearer {public_key}",
        "X-Timestamp": str(timestamp),
        "X-Signature": _sign_request(secret_key, timestamp, "POST", "/keys/validate", ""),
        "Content-Type": "application/json",
    }
    try:
        response = requests.post(f"{API_BASE}/keys/validate", headers=headers, data="", timeout=30)
    except requests.RequestException as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    try:
        result = response.json()
    except ValueError:
        result = {}
    if not isinstance(result, dict):
        result = {}

    if response.status_code == 200:
        state = result.get("status") or ("active" if result.get("valid") else "invalid")
        reason = f"status {state}"
    elif response.status_code == 401:
        reason = str(result.get("error") or "unauthorized")
        state = {":expired": "expired", "expired": "expired",
                 ":suspended": "suspended", "suspended": "suspended"}.get(reason, "invalid")
    else:
        msg = result.get("error") or result.get("message") or response.text
        print(f"Error: HTTP {response.status_code} {msg}", file=sys.stderr)
        sys.exit(4)

    if state != "active":
        if state == "expired":
            print("Expired")
            print(f"Public Key: {public_key}")
            print(f"To renew: Visit {PORTAL_BASE}/keys/extend")
        elif state == "suspended":
            print("Suspended")
            print(f"Public Key: {public_key}")
        else:
            print("Invalid")
            print(f"Public Key: {public_key}")
            print(f"Reason: {reason}")
        sys.exit(3)

    expires = remaining = "N/A"
    vt = result.get("valid_through")
    if isinstance(vt, (int, float)):
        secs = int(vt // 1000)
        expires = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(secs))
        left = secs - int(time.time())
        if left <= 0:
            remaining = "expired"
        else:
            remaining = f"{left // 86400}d {left % 86400 // 3600}h {left % 3600 // 60}m"
    tier = result.get("tier")
    print("Valid")
    print(f"Public Key: {public_key}")
    print(f"Tier: {tier if tier is not None else 'N/A'}")
    print(f"Status: {result.get('status') or 'N/A'}")
    print(f"Expires: {expires}")
    print(f"Time Remaining: {remaining}")


def _credentials_csv_path(args) -> Optional[Path]:
    """The accounts.csv the CLI's credentials came from, or None (flags / env vars).
    Mirrors _resolve_credentials' tiers."""
    if args.public_key and args.secret_key:
        return None
    if args.account is None and os.environ.get("UNSANDBOX_PUBLIC_KEY") and os.environ.get("UNSANDBOX_SECRET_KEY"):
        return None
    index = args.account if args.account is not None else int(os.environ.get("UNSANDBOX_ACCOUNT", "0"))
    for path in (_get_unsandbox_dir() / "accounts.csv", Path("accounts.csv")):
        if _load_credentials_from_csv(path, index):
            return path
    return None


def _rewrite_accounts_csv(path: Path, public_key: str, new_secret: str) -> bool:
    """Replace the secret on the line whose first field is public_key. Every other
    line, field, comment and the order stay as they are. Temp file (0600) + rename."""
    lines = path.read_text().split("\n")
    changed = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        fields = line.split(",")
        if len(fields) >= 2 and fields[0].strip() == public_key:
            fields[1] = new_secret
            lines[i] = ",".join(fields)
            changed = True
            break
    if not changed:
        return False
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(lines))
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return True


def _handle_key_rotate(args, public_key: str, secret_key: str):
    """un key --rotate [--otp CODE]: rotate this key's secret (see CLI_SPEC.md)."""
    _check_requests()
    path = "/keys/self/rotate-secret"
    body = "{}"
    unsandbox_dir = _get_unsandbox_dir()
    challenge_file = unsandbox_dir / (
        "rotate-" + hashlib.sha256(public_key.encode()).hexdigest()[:16] + ".challenge"
    )

    def post(otp=None, challenge=None):
        ts = int(time.time())
        headers = {
            "Authorization": f"Bearer {public_key}",
            "X-Timestamp": str(ts),
            "X-Signature": _sign_request(secret_key, ts, "POST", path, body),
            "Content-Type": "application/json",
        }
        if otp:
            headers["X-Sudo-OTP"] = otp
            headers["X-Sudo-Challenge"] = challenge or ""
        return requests.post(f"{API_BASE}{path}", headers=headers, data=body, timeout=120)

    def fail(resp):
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if resp.status_code == 403 and data.get("error") == "email_required":
            print("Error: this key has no email; add one or use POST /keys/recover", file=sys.stderr)
            sys.exit(3)
        if resp.status_code == 401:
            print("Error: authentication failed", file=sys.stderr)
            sys.exit(3)
        print(f"Error: {data.get('message') or data.get('error') or 'HTTP ' + str(resp.status_code)}", file=sys.stderr)
        sys.exit(4)

    if args.otp:
        try:
            challenge = challenge_file.read_text().strip()
        except OSError:
            print("Error: no rotation in progress; run: un key --rotate", file=sys.stderr)
            sys.exit(2)
        resp = post(args.otp.strip(), challenge)
        challenge_file.unlink(missing_ok=True)
    else:
        resp = post()
        if resp.status_code == 428:
            challenge = (resp.json() or {}).get("challenge_id", "")
            if sys.stdin.isatty():
                print("One-time code emailed to the key owner.", file=sys.stderr)
                print("Code: ", end="", file=sys.stderr, flush=True)
                otp = sys.stdin.readline().strip()
                if not otp:
                    print("Error: cancelled", file=sys.stderr)
                    sys.exit(1)
                resp = post(otp, challenge)
            else:
                unsandbox_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
                fd = os.open(challenge_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w") as f:
                    f.write(challenge)
                os.chmod(challenge_file, 0o600)
                print("One-time code emailed to the key owner. Finish with: un key --rotate --otp CODE")
                return

    if resp.status_code != 200:
        fail(resp)
    new_secret = (resp.json() or {}).get("secret")
    if not new_secret:
        print("Error: API returned no secret", file=sys.stderr)
        sys.exit(4)

    csv_path = _credentials_csv_path(args)
    if csv_path and _rewrite_accounts_csv(csv_path, public_key, new_secret):
        print(f"Secret rotated. Updated {csv_path} (public key {public_key}).")
    else:
        print(f"New secret (shown once, store it now): {new_secret}")
        print("Warning: UNSANDBOX_SECRET_KEY / -k still hold the old, now invalid, secret.")


def _handle_languages_command(args, public_key: str, secret_key: str):
    """Handle languages list command."""
    languages = get_languages(public_key, secret_key)

    if args.json:
        # Output as JSON array
        print(json.dumps(languages))
    else:
        # Output one language per line (pipe-friendly)
        for lang in languages:
            print(lang)


if __name__ == "__main__":
    cli_main()
