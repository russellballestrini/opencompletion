"""Uploaded files: where their bytes live & what a file may be.

Bytes sit in SQLite (table `blob`), once per SHA-256, beside the row that
names them (`attachment`). Everything else reaches them through this module
(put, get & save_attachment), so outgrowing SQLite means writing one class
with put/get (a document object store's API) & pointing `store` at it.

Limits: OPENCOMPLETION_UPLOAD_MAX_MB (default 7) per file.
"""

import base64
import hashlib
import mimetypes
import os
import re
from urllib.parse import quote

from models import Attachment, Blob, db

DEFAULT_MAX_MB = 7

# Served inline (shown in the page); anything else downloads. SVG & HTML
# never render inline: either can carry script.
INLINE_TYPES = {
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
    "audio/mpeg",
    "audio/ogg",
    "audio/wav",
    "audio/webm",
    "video/mp4",
    "video/webm",
    "application/pdf",
    "text/plain",
}
IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}

# Files a model can read as text (code, data, notes), by extension.
TEXT_EXTENSIONS = {
    ".txt", ".md", ".rst", ".csv", ".tsv", ".json", ".jsonl", ".yaml", ".yml",
    ".toml", ".ini", ".cfg", ".xml", ".html", ".css", ".js", ".ts", ".tsx",
    ".jsx", ".py", ".rb", ".go", ".rs", ".c", ".h", ".cpp", ".hpp", ".java",
    ".kt", ".swift", ".cs", ".php", ".pl", ".lua", ".sh", ".bash", ".zsh",
    ".fish", ".ps1", ".sql", ".r", ".jl", ".hs", ".scala", ".ex", ".exs",
    ".erl", ".clj", ".lisp", ".el", ".vim", ".tex", ".log", ".diff", ".patch",
}  # fmt: skip
# How much of one text file a model is shown (characters).
TEXT_CHARS_FOR_MODEL = 60000


def max_bytes():
    """OPENCOMPLETION_UPLOAD_MAX_MB in bytes."""
    try:
        mb = float(os.environ.get("OPENCOMPLETION_UPLOAD_MAX_MB", DEFAULT_MAX_MB))
    except ValueError:
        mb = DEFAULT_MAX_MB
    return int(max(mb, 0) * 1024 * 1024)


def clean_filename(name):
    """A name safe in a URL path & a Content-Disposition header: no
    directories, no control or quote characters, at most 120 characters."""
    name = os.path.basename(str(name or "").replace("\\", "/")).strip()
    name = re.sub(r"[^\w.\- ()+]", "_", name)[:120].strip(" .")
    return name or "file"


def guess_type(filename, claimed=None):
    """Our MIME type for a file: the browser's claim when it is a real
    type, else one guessed from the extension, else octet-stream."""
    claimed = (claimed or "").split(";")[0].strip().lower()
    if re.fullmatch(r"[a-z0-9.+-]+/[a-z0-9.+-]+", claimed) and claimed not in (
        "application/octet-stream",
    ):
        return claimed
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


def is_text(attachment):
    """True when a model can read this file as text."""
    ext = os.path.splitext(attachment.filename)[1].lower()
    return attachment.mime_type.startswith("text/") or ext in TEXT_EXTENSIONS


class SqliteBlobStore:
    """Bytes in our SQLite database, keyed by SHA-256."""

    def put(self, data):
        """Store `data` (once); return its SHA-256. The caller commits."""
        digest = hashlib.sha256(data).hexdigest()
        if db.session.get(Blob, digest) is None:
            db.session.add(Blob(sha256=digest, size=len(data), data=data))
        return digest

    def get(self, digest):
        """The bytes for `digest`, or None."""
        blob = db.session.get(Blob, digest)
        return blob.data if blob else None


store = SqliteBlobStore()


def save_attachment(room_id, data, filename, mime_type, username, source_url=None):
    """Store `data` & name it in `room_id`; commits & returns the
    Attachment. The size limit is the caller's to enforce (it knows how to
    say no to a person or a fetch)."""
    filename = clean_filename(filename)
    attachment = Attachment(
        room_id=room_id,
        sha256=store.put(data),
        filename=filename,
        mime_type=guess_type(filename, mime_type),
        size=len(data),
        username=username,
        source_url=source_url,
    )
    db.session.add(attachment)
    db.session.commit()
    return attachment


def format_size(size):
    """1536 -> "2 KB", as composer.js's formatSize says it."""
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{round(size / 1024)} KB"
    return f"{size / (1024 * 1024):.1f} MB"


MARKDOWN_SPECIAL = re.compile(r"([\\`*_{}\[\]()#+\-.!<>|~])")


def attachment_markdown(attachment, label=None):
    """How a file reads in a message, exactly as composer.js's
    attachmentMarkdown writes an upload: an image shows, anything else is a
    📎 link with its size. The label is escaped so it can't add markup."""
    name = MARKDOWN_SPECIAL.sub(r"\\\1", str(label or attachment.filename))
    url = quote(attachment.url, safe="/").replace("(", "%28").replace(")", "%29")
    if attachment.mime_type in IMAGE_TYPES:
        return f"![{name}]({url})"
    return f"[📎 {name} · {format_size(attachment.size)}]({url})"


def attachment_json(attachment):
    """A file as our API & our tools describe it, with the markdown that
    shows it, so a model pastes the link instead of composing one."""
    return {
        "id": attachment.public_id,
        "url": attachment.url,
        "filename": attachment.filename,
        "mime_type": attachment.mime_type,
        "size": attachment.size,
        "is_image": attachment.mime_type in IMAGE_TYPES,
        "markdown": attachment_markdown(attachment),
    }


# An image kept inside a message as base64 (DALL-E replies & vision images
# before blob_store), the whole <img> tag.
INLINE_IMAGE = re.compile(
    r'<img\b[^>]*?src="data:(image/(?:jpeg|png|gif|webp));base64,([A-Za-z0-9+/=\s]+)"[^>]*>'
)
IMG_ALT = re.compile(r'\balt="([^"]*)"')


def inline_images_to_attachments(content, room_id, username):
    """`content` with every base64 <img> in it saved to our blob store as an
    attachment of `room_id` & replaced by its markdown; (content, count)."""
    import html

    count = 0

    def save(match):
        nonlocal count
        try:
            data = base64.b64decode(match.group(2))
        except ValueError:
            return match.group(0)
        alt = IMG_ALT.search(match.group(0))
        label = html.unescape(alt.group(1)) if alt else ""
        ext = match.group(1).split("/")[1].replace("jpeg", "jpg")
        attachment = save_attachment(
            room_id, data, f"image.{ext}", match.group(1), username
        )
        count += 1
        return attachment_markdown(attachment, label[:200] or None)

    return INLINE_IMAGE.sub(save, content or ""), count


def read_attachment(attachment):
    """An attachment's bytes (None if its blob has gone)."""
    return store.get(attachment.sha256)


# How a message points at a file: our own path, never a full URL, by the
# file's random public_id.
ATTACHMENT_LINK = re.compile(r"/attachments/([0-9a-f]{32})/")


def find_attachment(public_id):
    """The attachment a link names, or None."""
    return Attachment.query.filter_by(public_id=public_id).first()


def copy_attachments(content, source_room_id, new_room_id, copied):
    """`content` with every link to `source_room_id`'s files pointing at a
    copy in `new_room_id` (a fork keeps its files when the original goes).
    Copies share bytes, so they cost a row each. `copied` maps old public ids
    to new across one fork's messages; the caller commits."""

    def relink(match):
        old_id = match.group(1)
        if old_id not in copied:
            original = find_attachment(old_id)
            if original is None or original.room_id != source_room_id:
                copied[old_id] = old_id
            else:
                copy = Attachment(
                    room_id=new_room_id,
                    sha256=original.sha256,
                    filename=original.filename,
                    mime_type=original.mime_type,
                    size=original.size,
                    username=original.username,
                    source_url=original.source_url,
                )
                db.session.add(copy)
                db.session.flush()
                copied[old_id] = copy.public_id
        return f"/attachments/{copied[old_id]}/"

    return ATTACHMENT_LINK.sub(relink, content)


def delete_room_attachments(room_id):
    """Drop a room's files, then any bytes no other room still names.
    The caller commits."""
    digests = {
        digest
        for (digest,) in db.session.query(Attachment.sha256).filter_by(room_id=room_id)
    }
    Attachment.query.filter_by(room_id=room_id).delete()
    db.session.flush()
    for digest in digests:
        if not Attachment.query.filter_by(sha256=digest).first():
            Blob.query.filter_by(sha256=digest).delete()


def data_url(attachment):
    """An image attachment as a data: URL, for a vision model."""
    data = read_attachment(attachment) or b""
    return f"data:{attachment.mime_type};base64,{base64.b64encode(data).decode()}"


def room_attachments(content, room_id):
    """Attachments `content` links to, in order, that belong to `room_id`.
    Only its own room's: a message in a public room naming a private
    room's file id must never pull that file into a model's context."""
    seen, found = set(), []
    for match in ATTACHMENT_LINK.finditer(content or ""):
        public_id = match.group(1)
        if public_id in seen:
            continue
        seen.add(public_id)
        attachment = find_attachment(public_id)
        if attachment is not None and attachment.room_id == room_id:
            found.append(attachment)
    return found


def has_image_attachment(content, room_id):
    return any(a.mime_type in IMAGE_TYPES for a in room_attachments(content, room_id))


def attachment_context(content, room_id, include_images=False):
    """A message as a model should read it: text & code files it carries
    are inlined (up to TEXT_CHARS_FOR_MODEL each); with `include_images`
    (a vision model, its newest image turn) images ride along as image
    parts. Other files stay links, so a model sees their names."""
    attachments = room_attachments(content, room_id)
    if not attachments:
        return content
    text = content
    for attachment in attachments:
        if not is_text(attachment):
            continue
        body = (read_attachment(attachment) or b"").decode("utf-8", "replace")
        cut = len(body) > TEXT_CHARS_FOR_MODEL
        text += f"\n\n[File: {attachment.filename}]\n{body[:TEXT_CHARS_FOR_MODEL]}"
        if cut:
            text += f"\n[...cut at {TEXT_CHARS_FOR_MODEL} characters]"
    images = [a for a in attachments if a.mime_type in IMAGE_TYPES]
    if include_images and images:
        return [{"type": "text", "text": text}] + [
            {"type": "image_url", "image_url": {"url": data_url(a)}} for a in images
        ]
    return text
