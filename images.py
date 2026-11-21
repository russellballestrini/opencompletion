"""Images' pure half: which model ids draw instead of chat, what an image
request holds, & reading an image out of a message's old <img> tag. No
Flask, database or socket; app.py's `generate_image` &
`build_message_content` do the asking, fetching & saving."""

import os
import re


def extract_base64_from_img_tag(content: str) -> tuple[str, str] | None:
    """Extract base64 data and media type from an HTML img tag.

    Returns (media_type, base64_data) or None if not found.
    """
    import re

    # Match data:image/TYPE;base64,DATA patterns in img src
    pattern = r'<img[^>]*src="data:image/(jpeg|png|gif|webp);base64,([^"]+)"'
    match = re.search(pattern, content)
    if match:
        media_type = f"image/{match.group(1)}"
        base64_data = match.group(2)
        return (media_type, base64_data)
    return None


def extract_external_image_url(content: str) -> str | None:
    """Extract external image URL from an HTML img tag.

    Returns the URL or None if not found.
    """
    import re

    # Match external URLs in img src (http/https)
    pattern = r'<img[^>]*src="(https?://[^"]+)"'
    match = re.search(pattern, content)
    if match:
        return match.group(1)
    return None


# Models that draw instead of chat, by id. OpenAI's /v1/models lists them
# beside its chat models (gpt-image-2, ...; dall-e-2 & dall-e-3 left its API
# on 2026-05-12), so our picker offers whichever exist today & no id is
# written here. OPENCOMPLETION_IMAGE_MODELS (a regex) names another server's.
IMAGE_MODELS = re.compile(
    os.environ.get("OPENCOMPLETION_IMAGE_MODELS")
    or r"^(gpt-image|chatgpt-image|dall-e)",
    re.I,
)


def is_image_model(model):
    return bool(model and IMAGE_MODELS.search(model))


def image_request(model, prompt):
    """What we send /images/generations for `model`: one image at
    OPENCOMPLETION_IMAGE_SIZE (1024x1024) & OPENCOMPLETION_IMAGE_QUALITY
    (unset: the model's own default; low, medium or high price an image
    very differently). GPT image models always answer base64 & refuse
    response_format; a DALL-E still served elsewhere must be asked for it."""
    request = {
        "model": model,
        "prompt": prompt,
        "n": 1,
        "size": os.environ.get("OPENCOMPLETION_IMAGE_SIZE") or "1024x1024",
    }
    quality = os.environ.get("OPENCOMPLETION_IMAGE_QUALITY")
    if quality:
        request["quality"] = quality
    if model.lower().startswith("dall-e"):
        request["response_format"] = "b64_json"
    return request
