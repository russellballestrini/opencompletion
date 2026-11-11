"""What our Activities gallery says about an activity: a title, one line
saying what it is & its kind, read from its YAML and never from a model.
No Flask, no database, and no activity script is ever run: we only look at
whether a step has one.

An activity may say all three itself with optional top-level keys
(research/SPEC.yaml):

    title: "Vault Heist"
    description: "Crack a four-digit code together."
    kind: "game"            # game, lesson or tool

Whatever it leaves out is derived:

title        its first step's first `# heading` when that heading shares a
             word with the file's name (activity31-scientific-method &
             "Welcome to Scientific Method Explorer!" -> "Scientific Method
             Explorer"; a heading like "THE DIRECTOR AWAKENS" names a scene,
             not the activity, so the file's name wins), else the file's name
             ("activity29-battleship" -> "Battleship"), else, for a file with
             only a number, a "Welcome to X" opening line, its first
             section's title, then the file's name as it is.
description  the first step's opening prose (headings, lists, tables, code &
             templated blocks skipped; a "Welcome ..." sentence dropped when
             more follows, as is one ending in ":"), whole sentences up to DESCRIPTION_CHARS, else cut
             at a word with an ellipsis. A first step with no prose gives its
             question.
kind         "game" when any step has a `processing_script` or `pre_script`
             (the room plays against code) or any transition draws from
             `metadata_random` / `metadata_tmp_random` (chance decides);
             otherwise "lesson". "tool" is never guessed: only a YAML's own
             `kind: tool` says so.
"""

import os
import re

import yaml

KINDS = ("game", "lesson", "tool")
DESCRIPTION_CHARS = 140

# CSafeLoader reads our sixty files in a fraction of the pure-Python time.
_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)

# Pictographs, dingbats, flags & their joiners: decoration in a heading.
_EMOJI = re.compile("[\U0001f000-\U0001faff⌀-⏿☀-➿⬀-⯿️‍]")
_HEADING = re.compile(r"^\s*#{1,6}\s+(.*\S)\s*$")
_WELCOME = re.compile(r"^welcome(?:\s+aboard)?(?:\s+to)?\s+(?:the\s+|our\s+)?", re.I)
_NOT_PROSE = re.compile(r"^\s*(?:[-*+•✓✔☐|>]|\d+[.)]\s|```|~~~|<)")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
_SMALL_WORDS = {"the", "and", "for", "our", "with", "your", "you"}


def plain(text):
    """Markdown & emoji off, spaces collapsed: words a card can show."""
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", str(text))
    text = re.sub(r"[*_`~]+", "", text)
    text = _EMOJI.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def slug_title(path):
    """research/activity29-battleship.yaml -> "Battleship"; "" when the
    file's name holds no words (activity7.yaml)."""
    stem = os.path.splitext(os.path.basename(path))[0]
    words = re.sub(r"^activity", "", stem, flags=re.I)
    words = re.sub(r"^\d+", "", words).strip("-_ ")
    return " ".join(w.capitalize() for w in re.split(r"[-_\s]+", words) if w)


def _words(text):
    return {
        w
        for w in re.findall(r"[a-z0-9]+", text.lower())
        if len(w) > 2 and w not in _SMALL_WORDS
    }


def _first_step(content):
    try:
        step = content["sections"][0]["steps"][0]
    except (KeyError, IndexError, TypeError):
        return {}
    return step if isinstance(step, dict) else {}


def _blocks(step):
    blocks = step.get("content_blocks")
    return [str(b) for b in blocks] if isinstance(blocks, list) else []


def _name_from(line):
    """A heading or an opening line as a name: no "Welcome to the", no
    closing punctuation."""
    name = _WELCOME.sub("", plain(line)).strip(" !.:-")
    return name[:1].upper() + name[1:]


def derive_title(path, content):
    slug = slug_title(path)
    step = _first_step(content)
    for block in _blocks(step):
        match = _HEADING.match(block.split("\n", 1)[0])
        if match:
            heading = _name_from(match.group(1))
            if heading and (not slug or _words(heading) & _words(slug)):
                return heading
            break  # only a first heading may name an activity
    if slug:
        return slug
    for block in _blocks(step):
        line = plain(_SENTENCE_END.split(block.strip(), maxsplit=1)[0])
        if _WELCOME.match(line) and line.lower().startswith("welcome to"):
            name = _name_from(line)
            if name:
                return name
        break
    try:
        section_title = plain(content["sections"][0].get("title") or "")
    except (KeyError, IndexError, TypeError, AttributeError):
        section_title = ""
    return section_title or os.path.splitext(os.path.basename(path))[0]


def _prose(step):
    """The first step's opening prose blocks, in order, as plain lines."""
    lines, fenced = [], False
    for block in _blocks(step):
        for line in block.split("\n"):
            if line.strip().startswith(("```", "~~~")):
                fenced = not fenced
                continue
            if fenced or not line.strip() or _HEADING.match(line):
                continue
            if _NOT_PROSE.match(line) or "{{" in line:
                continue
            # "You'll learn to:" introduces a list we do not show.
            sentences = _SENTENCE_END.split(plain(line))
            if sentences[-1].endswith(":"):
                sentences.pop()
            if sentences and sentences[0]:
                lines.append(" ".join(sentences))
    return lines


def cut(text, limit=DESCRIPTION_CHARS):
    """Whole sentences up to `limit` characters; a first sentence longer
    than that is cut at a word & says so with an ellipsis."""
    sentences = [s for s in _SENTENCE_END.split(text.strip()) if s]
    if len(sentences) > 1 and sentences[0].lower().startswith("welcome"):
        sentences = sentences[1:]
    kept = ""
    for sentence in sentences:
        longer = f"{kept} {sentence}".strip()
        if len(longer) > limit:
            break
        kept = longer
    if kept:
        return kept
    first = sentences[0] if sentences else ""
    if len(first) <= limit:
        return first
    return first[:limit].rsplit(" ", 1)[0].rstrip(" ,;:-") + "…"


def derive_description(content):
    step = _first_step(content)
    text = " ".join(_prose(step)[:4])
    if not text and isinstance(step.get("question"), str):
        text = plain(step["question"])
    return cut(text)


def derive_kind(content):
    try:
        sections = content.get("sections") or []
    except AttributeError:
        return "lesson"
    for section in sections:
        steps = section.get("steps") if isinstance(section, dict) else None
        for step in steps or []:
            if not isinstance(step, dict):
                continue
            if step.get("processing_script") or step.get("pre_script"):
                return "game"
            transitions = step.get("transitions")
            for transition in (
                transitions.values() if isinstance(transitions, dict) else []
            ):
                if isinstance(transition, dict) and (
                    "metadata_random" in transition
                    or "metadata_tmp_random" in transition
                ):
                    return "game"
    return "lesson"


def names_only(path):
    """A card for an activity we could not read: its name & nothing we
    would have to make up."""
    stem = os.path.splitext(os.path.basename(path))[0]
    return {
        "path": path,
        "name": stem,
        "title": slug_title(path) or stem,
        "description": "",
        "kind": None,
        "readable": False,
        "listed": True,
    }


def _own(content, key):
    value = content.get(key)
    return value.strip() if isinstance(value, str) and value.strip() else ""


def card(path, content):
    """One gallery card for the parsed YAML `content` of `path`: its own
    title, description & kind where it gives them, derived where not."""
    if not isinstance(content, dict) or not isinstance(content.get("sections"), list):
        return names_only(path)
    kind = _own(content, "kind").lower()
    return {
        "path": path,
        "name": os.path.splitext(os.path.basename(path))[0],
        "title": plain(_own(content, "title")) or derive_title(path, content),
        "description": plain(_own(content, "description"))
        or derive_description(content),
        "kind": kind if kind in KINDS else derive_kind(content),
        "readable": True,
        # `listed: false` keeps an activity off our gallery (a test
        # fixture, a draft); `/activity <path>` still starts it.
        "listed": content.get("listed") is not False,
    }


# path -> ((mtime_ns, size), card): a file is parsed once until it changes.
_CACHE = {}


def local_card(path):
    """The card of a YAML file on disk, parsed only when its mtime or size
    changed. A file we cannot read or parse gets its name only."""
    try:
        stat = os.stat(path)
        stamp = (stat.st_mtime_ns, stat.st_size)
        cached = _CACHE.get(path)
        if cached and cached[0] == stamp:
            return dict(cached[1])
        with open(path, "r", encoding="utf-8") as file:
            made = card(path, yaml.load(file, Loader=_LOADER))
    except (OSError, UnicodeDecodeError, yaml.YAMLError, RecursionError):
        return names_only(path)
    _CACHE[path] = (stamp, made)
    return dict(made)


def local_paths(directory="research"):
    """Every YAML in `directory`, as the paths `/activity <path>` takes."""
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return []
    return [f"{directory}/{n}" for n in names if n.endswith((".yaml", ".yml"))]
