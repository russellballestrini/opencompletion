"""
Utility functions for OpenCompletion Activity System v2.0

Features:
- Template variable rendering ({{metadata.key}}, {{current_attempt}}, etc.)
- Advanced metadata conditions (gte, lt, contains, regex, etc.)
- Conditional content blocks (show_if)
- Conditional navigation (if/elif/else)
- Weighted random selection
- Progressive hints
"""

import math
import re
import random
import operator
from functools import partial
from typing import Any, Dict, List, Optional, Union


def render_template(text: str, context: Dict[str, Any]) -> str:
    """
    Render template variables in text using {{variable}} syntax.

    Supports:
    - {{metadata.key}} - Access metadata values
    - {{current_attempt}} - Current attempt number
    - {{max_attempts}} - Maximum attempts
    - {{attempts_remaining}} - Remaining attempts
    - {{current_section}} - Current section ID
    - {{current_step}} - Current step ID
    - {{username}} - Last responding username

    Args:
        text: Text containing {{variable}} templates
        context: Dictionary with metadata, attempts, section/step info

    Returns:
        Text with variables replaced
    """
    if not isinstance(text, str):
        return text

    # Find all {{variable}} patterns
    pattern = r"\{\{([^}]+)\}\}"

    def replace_variable(match):
        var_name = match.group(1).strip()

        # Handle metadata.key syntax
        if var_name.startswith("metadata."):
            key = var_name[9:]  # Remove 'metadata.' prefix
            metadata = context.get("metadata", {})
            value = metadata.get(
                key, f"{{{{metadata.{key}}}}}"
            )  # Keep original if not found
            return str(value) if value is not None else ""

        # Handle built-in variables
        value = context.get(
            var_name, f"{{{{{var_name}}}}}"
        )  # Keep original if not found
        return str(value) if value is not None else ""

    return re.sub(pattern, replace_variable, text)


def evaluate_condition(
    metadata: Dict[str, Any], condition_key: str, condition_value: Any
) -> bool:
    """
    Evaluate a single condition against metadata.

    Supports operators:
    - key: value - Equality
    - key_ne: value - Not equal
    - key_gt: value - Greater than
    - key_gte: value - Greater than or equal
    - key_lt: value - Less than
    - key_lte: value - Less than or equal
    - key_between: [min, max] - Between (inclusive)
    - key_contains: value - Comma-separated list contains value
    - key_not_contains: value - List does NOT contain value
    - key_matches: pattern - Regex match
    - key_exists: true/false - Key existence check
    - key_not_exists: true/false - Key non-existence check

    Args:
        metadata: Metadata dictionary to check
        condition_key: Condition key (may have operator suffix)
        condition_value: Expected value

    Returns:
        True if condition met, False otherwise
    """
    for suffix, evaluate in _CONDITION_OPERATORS:
        if condition_key.endswith(suffix):
            return evaluate(metadata, condition_key[: -len(suffix)], condition_value)
    return metadata.get(condition_key) == condition_value


def _compare_numeric(metadata, key, expected, compare):
    try:
        return compare(float(metadata.get(key, 0)), float(expected))
    except (ValueError, TypeError):
        return False


def _between(metadata, key, bounds):
    if not isinstance(bounds, list) or len(bounds) != 2:
        return False
    try:
        value = float(metadata.get(key, 0))
        # Keep the chained comparison: the upper bound is converted lazily.
        return float(bounds[0]) <= value <= float(bounds[1])
    except (ValueError, TypeError):
        return False


def _contains(metadata, key, expected):
    value_str = str(metadata.get(key, ""))
    items = [item.strip() for item in value_str.split(",") if item.strip()]
    return str(expected) in items


def _matches(metadata, key, pattern):
    value_str = str(metadata.get(key, ""))
    try:
        return bool(re.search(str(pattern), value_str))
    except re.error:
        return False


def _not_equal(metadata, key, expected):
    return metadata.get(key) != expected


def _not_contains(metadata, key, expected):
    return not _contains(metadata, key, expected)


def _exists(metadata, key, expected, negate=False):
    # Preserve truthiness evaluation before consulting metadata.
    expected_presence = bool(expected) != negate
    return (key in metadata) == expected_presence


# Ordered dispatch: overlapping suffixes must keep the more specific first.
_CONDITION_OPERATORS = (
    ("_ne", _not_equal),
    ("_gt", partial(_compare_numeric, compare=operator.gt)),
    ("_gte", partial(_compare_numeric, compare=operator.ge)),
    ("_lt", partial(_compare_numeric, compare=operator.lt)),
    ("_lte", partial(_compare_numeric, compare=operator.le)),
    ("_between", _between),
    ("_not_contains", _not_contains),
    ("_contains", _contains),
    ("_matches", _matches),
    ("_not_exists", partial(_exists, negate=True)),
    ("_exists", _exists),
)


def check_conditions(metadata: Dict[str, Any], conditions: Dict[str, Any]) -> bool:
    """
    Check if ALL conditions are met (AND logic).

    Args:
        metadata: Metadata dictionary
        conditions: Dictionary of condition_key: condition_value pairs

    Returns:
        True if all conditions met, False otherwise
    """
    if not conditions:
        return True

    return all(
        evaluate_condition(metadata, key, value) for key, value in conditions.items()
    )


def filter_content_blocks(
    content_blocks: List[Union[str, Dict[str, Any]]],
    metadata: Dict[str, Any],
    context: Dict[str, Any],
) -> List[str]:
    """
    Filter and render content blocks based on show_if conditions.

    Content blocks can be:
    - Simple strings: Always shown
    - Objects with 'text' and 'show_if': Conditionally shown

    Args:
        content_blocks: List of content blocks (strings or dicts)
        metadata: Metadata dictionary for condition evaluation
        context: Template rendering context

    Returns:
        List of rendered text strings that passed conditions
    """
    result = []

    for block in content_blocks:
        if isinstance(block, str):
            # Simple string - always show, just render templates
            rendered = render_template(block, context)
            result.append(rendered)

        elif isinstance(block, dict):
            # Conditional block - check show_if condition
            text = block.get("text", "")
            show_if = block.get("show_if", {})

            # Check if conditions are met
            if check_conditions(metadata, show_if):
                rendered = render_template(text, context)
                result.append(rendered)

    return result


def _navigation_branch_matches(branch, metadata):
    """Preserve if/elif/else precedence, even for mixed-key branches."""
    for keyword in ("if", "elif"):
        if keyword in branch:
            return check_conditions(metadata, branch[keyword])
    return "else" in branch


def resolve_conditional_navigation(
    next_section_and_step: Union[str, List[Dict[str, Any]]], metadata: Dict[str, Any]
) -> Optional[str]:
    """
    Resolve conditional navigation (if/elif/else structure).

    Args:
        next_section_and_step: Either a string or list of conditional branches
        metadata: Metadata dictionary for condition evaluation

    Returns:
        Resolved "section:step" string or None
    """
    # Simple string - return as-is
    if isinstance(next_section_and_step, str):
        return next_section_and_step

    # Conditional branches
    if isinstance(next_section_and_step, list):
        for branch in next_section_and_step:
            if _navigation_branch_matches(branch, metadata):
                return branch.get("goto")

    return None


def select_weighted_random(weighted_options: List[Dict[str, Any]]) -> Any:
    """
    Select a random value from weighted options.

    Args:
        weighted_options: List of dicts with 'value' and 'weight' keys

    Returns:
        Selected value
    """
    if not weighted_options:
        return None

    # Extract values and weights
    values = [opt["value"] for opt in weighted_options]
    weights = [opt.get("weight", 1) for opt in weighted_options]

    # Use random.choices for weighted selection
    selected = random.choices(values, weights=weights, k=1)
    return selected[0]


def get_progressive_hint(
    hints: List[Dict[str, Any]], current_attempt: int, context: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """
    Get the hint for the current attempt number, if one exists.

    Args:
        hints: List of hint dicts with 'attempt', 'text', 'counts_as_attempt' keys
        current_attempt: Current attempt number (1, 2, 3, ...)
        context: Template rendering context

    Returns:
        Hint dict with rendered text, or None if no hint for this attempt
    """
    if not hints:
        return None

    for hint in hints:
        if hint.get("attempt") == current_attempt:
            # Render template variables in hint text
            hint_text = render_template(hint.get("text", ""), context)
            return {
                "text": hint_text,
                "counts_as_attempt": hint.get("counts_as_attempt", False),
            }

    return None


def create_template_context(
    metadata: Dict[str, Any],
    current_attempt: int,
    max_attempts: int,
    current_section: str,
    current_step: str,
    username: str = "User",
) -> Dict[str, Any]:
    """
    Create a template rendering context with all built-in variables.

    Args:
        metadata: Activity metadata
        current_attempt: Current attempt number
        max_attempts: Maximum attempts allowed
        current_section: Current section ID
        current_step: Current step ID
        username: Username of last responder

    Returns:
        Context dictionary for template rendering
    """
    return {
        "metadata": metadata,
        "current_attempt": current_attempt,
        "max_attempts": max_attempts,
        "attempts_remaining": max(0, max_attempts - current_attempt),
        "current_section": current_section,
        "current_step": current_step,
        "username": username,
    }


# Reasoning-block tag variants, case-insensitive. Closed pairs are stripped
# anywhere; an unterminated open tag (model truncated mid-reasoning) strips to
# end-of-string. Ported from uncloseai-cli / hermes-agent think_scrubber.
_THINK_TAG_NAMES = ("think", "thinking", "reasoning", "thought", "REASONING_SCRATCHPAD")
_THINK_TAG_ALT = "|".join(_THINK_TAG_NAMES)
_THINK_PAIR_RE = re.compile(
    r"<(?:" + _THINK_TAG_ALT + r")>.*?</(?:" + _THINK_TAG_ALT + r")>",
    re.DOTALL | re.IGNORECASE,
)
_THINK_UNTERMINATED_RE = re.compile(
    r"<(?:" + _THINK_TAG_ALT + r")>.*$", re.DOTALL | re.IGNORECASE
)
_THINK_ORPHAN_CLOSE_RE = re.compile(r"</(?:" + _THINK_TAG_ALT + r")>", re.IGNORECASE)
_THINK_ANY_TAG_RE = re.compile(r"</?(?:" + _THINK_TAG_ALT + r")[^>]*>", re.IGNORECASE)


def strip_reasoning(text: Optional[str]) -> Optional[str]:
    """
    Remove chain-of-thought from model output.

    Handles tag variants (think/thinking/reasoning/thought/scratchpad),
    unterminated opens (model truncated mid-reasoning), and the chat-template
    pre-opened case where only a closing tag appears in the output — there,
    everything before the last orphan close is reasoning.

    Salvage rule: if the input was non-empty but every byte sat inside
    reasoning markup, return the de-tagged trace instead of an empty string —
    reasoning-tuned models commonly emit all-think for hard problems with the
    final answer as the last line of the trace.
    """
    if not text:
        return text
    original = text
    text = _THINK_PAIR_RE.sub("", text)
    text = _THINK_UNTERMINATED_RE.sub("", text)
    # Orphan close with no matching open: template pre-opened the block, so
    # everything up to the last close tag is reasoning.
    match = None
    for match in _THINK_ORPHAN_CLOSE_RE.finditer(text):
        pass
    if match:
        text = text[match.end() :]
    stripped = text.strip()
    if not stripped and original.strip():
        salvaged = _THINK_ANY_TAG_RE.sub("", original).strip()
        if salvaged:
            return salvaged
    return stripped


def _rejects_chat_template_kwargs(exc: Exception) -> bool:
    """True when an endpoint rejected the chat_template_kwargs body param."""
    status = getattr(exc, "status_code", None)
    return status in (400, 404, 422) and "chat_template_kwargs" in str(exc)


def create_completion_skip_thinking(openai_client, **create_kwargs):
    """
    chat.completions.create with chain-of-thought disabled.

    Self-hosted OpenAI-compatible servers (vLLM, SGLang, llama.cpp) accept
    chat_template_kwargs {"enable_thinking": false} to suppress reasoning at
    the template level. Hosted providers (OpenAI, Groq, Mistral, Gemini)
    reject the unknown param with a 4xx naming it, so retry once without.
    """
    extra_body = dict(create_kwargs.pop("extra_body", None) or {})
    extra_body.setdefault("chat_template_kwargs", {"enable_thinking": False})
    try:
        return openai_client.chat.completions.create(
            extra_body=extra_body, **create_kwargs
        )
    except Exception as e:
        if _rejects_chat_template_kwargs(e):
            return openai_client.chat.completions.create(**create_kwargs)
        raise


# ── Categorization readout ───────────────────────────────────────────


def _bucket_name(bucket):
    """A bucket as written in YAML may be a string, an int, a bool or a
    {bucket_name, ...} mapping; the classifier compares on its name."""
    if isinstance(bucket, dict):
        return str(bucket.get("bucket_name", ""))
    return str(bucket)


def resolve_bucket(category, buckets):
    """Map the classifier's text onto one of the step's buckets, or return
    None. Exact match first (case & whitespace folded), then the LONGEST
    bucket name contained in the text — longest first because "correct" is a
    substring of "incorrect", so a first-match scan would grade a wrong answer
    right. A model that answers "Partial Understanding." or "BUCKET: correct
    (the student ...)" lands on its bucket instead of on the Unrecognized
    category error."""
    norm = str(category or "").strip().lower().replace(" ", "_")
    names = [(_bucket_name(b), b) for b in buckets]
    for name, b in names:
        if norm == name.lower().replace(" ", "_"):
            return b
    for name, b in sorted(names, key=lambda nb: -len(nb[0])):
        key = name.lower().replace(" ", "_")
        if key and key in norm:
            return b
    return None


def fold_first_token_logprobs(top, buckets):
    """Fold a first-token `top_logprobs` list onto the bucket names by prefix
    and return (dist, mass): dist maps every bucket name to its renormalised
    probability, mass is the share of listed probability that landed on any
    bucket. A token prefixing exactly one bucket ("partial" for
    partial_understanding, " correct" for correct) carries its mass there; a
    token prefixing several ("correct" where correct & correct_no_work both
    exist) is dropped, never split. Same fold as uncloseai-cli decisions.py &
    unhomeschool nu._fold_first_token. Anything that is not a list of
    {token, logprob} entries folds to zeros."""
    keys = {}
    for b in buckets:
        name = _bucket_name(b)
        keys[name] = re.sub(r"[^a-z0-9]", "", name.lower())
    got = {name: 0.0 for name in keys}
    listed = assigned = 0.0
    if not isinstance(top, list):
        return got, 0.0
    for t in top:
        try:
            pr = math.exp(float(t["logprob"]))
            token = str(t.get("token") or "")
        except (TypeError, ValueError, KeyError, AttributeError):
            continue
        listed += pr
        nt = re.sub(r"[^a-z0-9]", "", token.lower())
        if not nt:
            continue
        hits = [
            n for n, k in keys.items() if k and (k.startswith(nt) or nt.startswith(k))
        ]
        if len(hits) == 1:
            got[hits[0]] += pr
            assigned += pr
    total = sum(got.values())
    dist = {n: (v / total if total else 0.0) for n, v in got.items()}
    return dist, (assigned / listed if listed else 0.0)


def first_token_top_logprobs(completion):
    """The first generated token's top_logprobs from an OpenAI-style
    completion, as a list of {token, logprob} dicts, or None."""
    try:
        content = completion.choices[0].logprobs.content
        first = content[0]
        top = first.top_logprobs
    except (AttributeError, IndexError, TypeError):
        return None
    if not isinstance(top, list):
        return None
    out = []
    for t in top:
        if isinstance(t, dict):
            out.append({"token": t.get("token"), "logprob": t.get("logprob")})
        else:
            out.append(
                {
                    "token": getattr(t, "token", None),
                    "logprob": getattr(t, "logprob", None),
                }
            )
    return out
