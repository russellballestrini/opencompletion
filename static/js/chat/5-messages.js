// Messages: new, historical & streamed chunks, edits & deletes.
// Part of chat.html's script, split by concern; files load in order &
// share globals (see templates/chat.html). Config comes from CHAT_CONFIG.

// Every message is markdown rendered by marked & sanitized by DOMPurify,
// images included (DOMPurify keeps data: image URLs & drops handlers such
// as onerror). Nothing a sender typed ever reaches innerHTML unsanitized.
// Math ($$…$$, \[…\], \(…\), $…$) is typeset by KaTeX (fox, 2026-11-01):
// taken out before marked (which eats its \\ & _), outside code, & put
// back after DOMPurify as KaTeX's own HTML, which escapes what it's given
// & with trust off runs no \href or \html commands.
// A single newline is a line break (marked's `breaks`, as in a GitHub
// comment): people & models write verse & addresses a line at a time.
function renderMarkdown(markdown) {
    const math = protectMath(markdown);
    const html = DOMPurify.sanitize(marked.marked(math.text, {breaks: true}), dompurify_config);
    return math.found.length ? restoreMath(html, math) : html;
}

// Code is never math: fenced blocks (even unclosed, mid-stream) & inline.
const CODE_SPANS = /(```[\s\S]*?(?:```|$)|~~~[\s\S]*?(?:~~~|$)|`[^`\n]*`)/;
// $…$ hugs its text & is no price: "$5 and $10" stays as written.
const MATH_SPANS = /\$\$([\s\S]+?)\$\$|\\\[([\s\S]+?)\\\]|\\\(([\s\S]+?)\\\)|(?<!\\)\$(?=\S)((?:\\\$|[^$\n])+?)(?<=\S)\$(?!\d)/g;

function protectMath(markdown) {
    const found = [];
    if (typeof katex === "undefined" || !/[$\\]/.test(markdown)) return {text: markdown, found};
    const key = "mathx" + Math.random().toString(36).slice(2, 10);
    const text = String(markdown).split(CODE_SPANS).map((part, i) => i % 2 ? part :
        part.replace(MATH_SPANS, (whole, block, bracket, paren, inline) => {
            found.push({
                tex: block ?? bracket ?? paren ?? inline,
                display: block !== undefined || bracket !== undefined,
                whole,
            });
            return `${key}n${found.length - 1}x`;
        })).join("");
    return {text, found, key};
}

function restoreMath(html, math) {
    return html.replace(new RegExp(`${math.key}n(\\d+)x`, "g"), (placeholder, n) => {
        const span = math.found[Number(n)];
        try {
            return katex.renderToString(span.tex, {
                displayMode: span.display, throwOnError: false, trust: false, strict: "ignore",
            });
        } catch (e) {
            const shown = document.createElement("span");
            shown.textContent = span.whole;
            return shown.innerHTML;
        }
    });
}

// A sender's name as a bold markdown label. Our own name links to /profile;
// other people have no public profile page, so theirs is plain text.
function senderLabel(name) {
    const safe = String(name).replace(/([\\`*_{}\[\]()#+\-.!<>|~])/g, '\\$1');
    return name === username ? `**[${safe}](/profile):**` : `**${safe}:**`;
}

// Who a message is from, as classes its look follows: a person's words in
// a bubble (ours on the right), a model's reply as plain text across the
// pane, so the two never read alike (group3r, 2026-10-31).
function markSender(wrapper, name, fromModel) {
    wrapper.classList.toggle("is-model", Boolean(fromModel));
    wrapper.classList.toggle("is-person", !fromModel);
    wrapper.classList.toggle("is-mine", !fromModel && name === username);
}

// Fork, on a model's reply: hover (or focus) says what it makes; a click
// calls forkFromMessage() in base.html & opens the new chat.
function addForkButton(buttonContainer, messageId) {
    const forkButton = document.createElement("button");
    forkButton.textContent = "Fork";
    forkButton.className = "fork-button";
    forkButton.title = `Fork: start a new chat from this reply, holding a copy of every message up to here${CHAT_CONFIG.signedIn ? " (solo, only you)" : " (a public chatroom)"}. This chat stays as it is.`;
    forkButton.onclick = () => forkFromMessage(messageId, forkButton);
    buttonContainer.appendChild(forkButton);
}

// Versions, on a message that was edited & resent: Edit & resend keeps
// what it replaced (the earlier words & everything said after them), & this
// pager opens each earlier version under the message, read only & only on
// this screen. Our chat carries on from the newest; nothing a model reads
// changes. Fetched on first use: GET /api/messages/<id>/versions. An open
// version offers Continue from here: a new chat from that version.
function addVersionPager(wrapper, messageId, count) {
    if (!(count > 1) || wrapper.querySelector(":scope > .message-versions")) return;
    const pager = document.createElement("div");
    pager.className = "message-versions";
    pager.setAttribute("role", "group");
    pager.setAttribute("aria-label", "Versions of this message");
    const step = (text, label) => {
        const button = document.createElement("button");
        button.type = "button";
        button.textContent = text;
        button.setAttribute("aria-label", label);
        button.title = label;
        return button;
    };
    const older = step("‹", "Earlier version");
    const newer = step("›", "Later version");
    const place = document.createElement("span");
    place.className = "message-versions-place";
    place.setAttribute("aria-live", "polite");
    pager.append(older, place, newer);

    let versions = null;
    let shown = count;
    let panel = null;
    const draw = () => {
        place.textContent = `${shown}/${count}`;
        place.title = shown < count ? `Version ${shown} of ${count}, as it was` : `Version ${count} of ${count}, our current one`;
        older.disabled = shown <= 1;
        newer.disabled = shown >= count;
        if (panel) panel.remove();
        panel = shown < count ? earlierVersionPanel(versions[shown - 1], shown, count, messageId) : null;
        if (panel) wrapper.appendChild(panel);
        wrapper.classList.toggle("is-viewing-earlier", Boolean(panel));
    };
    const go = async (by) => {
        if (!versions) {
            try {
                const response = await fetch(`/api/messages/${messageId}/versions`);
                if (!response.ok) throw new Error(response.status);
                versions = (await response.json()).versions;
            } catch (error) {
                chatTools.say("Couldn't load that earlier version. Try again.");
                return;
            }
            // Someone may have deleted an earlier version's messages since.
            count = versions.length + 1;
            shown = count;
        }
        shown = Math.min(count, Math.max(1, shown + by));
        draw();
    };
    older.onclick = () => go(-1);
    newer.onclick = () => go(1);
    draw();
    wrapper.appendChild(pager);
}

// One earlier version: the message as it was & what was said after it,
// drawn like our chat (same bubbles, same markdown) inside a quiet frame.
// Continue from here ends it: forkFromMessage() in base.html makes a new
// chat holding the conversation as it stood in this version & opens it.
function earlierVersionPanel(rows, shown, count, messageId) {
    const panel = document.createElement("div");
    panel.className = "earlier-version";
    const note = document.createElement("p");
    note.className = "earlier-version-note";
    note.textContent = `Version ${shown} of ${count}, as it was. Read only: our chat carries on from version ${count}.`;
    panel.appendChild(note);
    rows.forEach((row) => {
        const item = document.createElement("div");
        item.className = "message-wrapper";
        markSender(item, row.username, row.from_model);
        const words = document.createElement("div");
        words.className = "message-content";
        words.innerHTML = renderMarkdown(`${senderLabel(row.username)}\n\n${row.content}`);
        words.querySelectorAll("pre code").forEach((block) => {
            hljs.highlightElement(block);
            addLineNumbers(block);
            addCopyButtonToCodeBlock(block, false);
        });
        item.appendChild(words);
        panel.appendChild(item);
    });
    const actions = document.createElement("div");
    actions.className = "earlier-version-actions";
    const carryOn = document.createElement("button");
    carryOn.type = "button";
    carryOn.textContent = "Continue from here";
    carryOn.title = `Continue from here: start a new chat holding our conversation as it stood in version ${shown}${CHAT_CONFIG.signedIn ? " (solo, only you)" : " (a public chatroom)"}. This chat stays as it is.`;
    carryOn.onclick = () => forkFromMessage(messageId, carryOn, shown);
    actions.appendChild(carryOn);
    panel.appendChild(actions);
    return panel;
}

// Socket event for receiving a new message
// A message we never store (someone joined, an activity step, a reply's
// "Processing..." placeholder) carries an id of its own, notice-… or
// processing-… (activity_utils.notice_id): its own element, no actions.
function isNoticeId(id) {
    return typeof id === "string" && /^(notice|processing)-/.test(id);
}

socket.on("chat_message", (data) => {
    // A stored message has actions & ends the suggestions; a notice neither.
    const stored = Boolean(data.id) && !isNoticeId(data.id);
    if (stored) clearFollowups();
    const messageWrapper = document.createElement("div");
    messageWrapper.className = "message-wrapper";
    messageWrapper.id = "message-" + data.id;
    if (stored && data.username) markSender(messageWrapper, data.username, data.from_model);

    const newMessage = document.createElement("div");
    newMessage.className = "message-content";

    // Prepend the sender's name when there is one
    const messageContent = data.username
        ? `${senderLabel(data.username)}\n\n${data.content}`
        : data.content;
    newMessage.innerHTML = renderMarkdown(messageContent);

    // A stored message can be deleted, edited, copied & read aloud.
    if (stored) {
        newMessage.dataset.rawMarkdown = data.content;

        const buttonContainer = messageButtons(data.id, newMessage, false);
        messageWrapper.appendChild(buttonContainer);
    }

    messageWrapper.appendChild(newMessage);
    if (stored) addVersionPager(messageWrapper, data.id, data.versions);

    // A reply that failed arrives whole under the id it was streaming as:
    // it takes that reply's place, never a second element with its id.
    const streamed = stored && document.getElementById(messageWrapper.id);
    if (streamed && streamed.dataset.streaming) {
        forgetStream(data.id);
        streamed.replaceWith(messageWrapper);
    } else {
        document.getElementById("chat").appendChild(messageWrapper);
    }

    // Apply syntax highlighting to code blocks within the message
    // Don't truncate new messages - only truncate on page load (previous_messages)
    newMessage.querySelectorAll("pre code").forEach((block) => {
        hljs.highlightElement(block);
        addLineNumbers(block);
        addCopyButtonToCodeBlock(block, false);
    });

    // Scroll to the bottom of the chat container to show the new message.
    if (stored) {
        document.getElementById("chat").scrollTop = document.getElementById("chat").scrollHeight;

        // Auto-run our own posted fix, and only that: in a busy room another
        // person's message can arrive first, and its code must never run
        // unasked. A message that is not our fix leaves the pending run alone.
        // The fix we posted carries a random marker (an HTML comment
        // DOMPurify removes from display) & we run only the block whose
        // text is exactly the fixed code. Anything else leaves it pending.
        const pendingFix = window.pendingAutoExec;
        const fixedBlock = pendingFix && data.username === username
            && data.content.includes(pendingFix.marker)
            ? Array.from(newMessage.querySelectorAll("pre code")).find(
                (block) => block.textContent.trim() === pendingFix.code.trim())
            : null;
        if (fixedBlock) {
            const autoExecData = pendingFix;
            window.pendingAutoExec = null; // Clear it so we don't re-execute
            const codeBlock = fixedBlock;
            // Find the Run button for this code block
            setTimeout(() => {
                // The Run button is in a sibling container after the <pre> element
                const preElement = codeBlock.parentNode;
                const buttonContainer = preElement.nextSibling;
                const runButton = buttonContainer?.querySelector('.play-button');

                if (runButton) {
                    console.log(`Auto-executing fixed code (attempt ${autoExecData.attempt}/3)...`);

                    // Store the attempt count so executeCodeBlock can pick it up
                    // We'll use a data attribute on the code block itself
                    codeBlock.dataset.autoExecAttempt = autoExecData.attempt.toString();

                    // Trigger execution - it will create its own results container
                    runButton.click();
                }
            }, 100); // Delay to ensure buttons are fully rendered
        }

        // Auto-play TTS if enabled and message has content - AFTER buttons are created
        if (autoPlayTTS && data.content && data.content.trim() !== "") {
            setTimeout(() => {
                // Find the play button after buttons have been created
                const buttons = messageWrapper.querySelectorAll("button");
                const playButton = Array.from(buttons).find(btn => btn.textContent === "Play");
                if (playButton) {
                    console.log("Queueing TTS for message:", data.id);
                    queueTTS(data.content, playButton, data.id);
                }
            }, 10); // Very short delay to let buttons be created
        }
    }
});

// A model's thinking as a collapsible block above its reply: open while it
// streams, collapsed once the answer starts, & from history (saved with the
// reply as Message.thinking) collapsed. Text only, never markup: one text
// node, so a streamed piece is appended to it, never the whole re-set.
function thinkingBlock(text, open) {
    const details = document.createElement("details");
    details.className = "message-thinking";
    details.open = open;
    if (open) details.dataset.state = "streaming";
    details.appendChild(document.createElement("summary"));
    const body = document.createElement("div");
    body.className = "message-thinking-body";
    body.style.opacity = "0.6";
    body.style.fontStyle = "italic";
    body.style.whiteSpace = "pre-wrap";
    body.appendChild(document.createTextNode(text));
    details.appendChild(body);
    // Its summary says what a click does now, open or closed.
    details.addEventListener("toggle", () => labelThinking(details));
    labelThinking(details);
    return details;
}

function labelThinking(details, state) {
    if (state !== undefined) details.dataset.state = state;
    const summary = details.querySelector("summary");
    if (details.dataset.state === "stopped") summary.textContent = "thinking (where it stopped)";
    else if (details.dataset.state === "streaming") summary.textContent = "thinking…";
    else summary.textContent = details.open ? "thinking (click to collapse)" : "thinking (click to expand)";
}

// A message's action row. Copy, Edit & Play read its words when clicked
// (`content.dataset.rawMarkdown`), so an edit, or a reply that grew since
// its row was drawn, is never acted on as it once was.
function messageButtons(id, content, fromModel) {
    const buttonContainer = document.createElement("div");
    buttonContainer.className = "button-container";
    const words = () => content.dataset.rawMarkdown || "";

    const deleteButton = document.createElement("button");
    deleteButton.textContent = "Delete";
    deleteButton.onclick = () => deleteMessage(id, room_name);
    buttonContainer.appendChild(deleteButton);

    const editButton = document.createElement("button");
    editButton.textContent = "Edit";
    editButton.className = "edit-button";
    editButton.onclick = () => editMessage(id, content);
    buttonContainer.appendChild(editButton);

    const copyButton = document.createElement("button");
    copyButton.textContent = "Copy";
    copyButton.className = "copy-button";
    copyButton.onclick = () => copyMessageContent(words(), copyButton);
    buttonContainer.appendChild(copyButton);

    const playButton = document.createElement("button");
    playButton.textContent = "Play";
    playButton.onclick = () => speakText(words(), playButton, id);
    buttonContainer.appendChild(playButton);

    // TTS audio download (hidden until there is audio)
    const downloadButton = document.createElement("button");
    downloadButton.textContent = "Download";
    downloadButton.className = "tts-download-button";
    downloadButton.style.display = "none";
    downloadButton.dataset.messageId = id;
    buttonContainer.appendChild(downloadButton);
    if (fromModel) addForkButton(buttonContainer, id);
    return buttonContainer;
}

// Code blocks in freshly drawn words: highlighted, numbered, with Copy.
// History truncates long ones; live words never do.
function dressCodeBlocks(container, truncate) {
    container.querySelectorAll("pre code").forEach((block) => {
        const wasTruncated = truncate ? truncateCodeBlock(block) : false;
        hljs.highlightElement(block);
        addLineNumbers(block);
        addCopyButtonToCodeBlock(block, wasTruncated);
    });
}

// A stored message as our server holds it (history, or a reply's state on
// the instant we joined), drawn but not yet on the page.
function drawStoredMessage(data) {
    const messageWrapper = document.createElement("div");
    messageWrapper.className = "message-wrapper";
    messageWrapper.id = "message-" + data.id;
    markSender(messageWrapper, data.username, data.from_model);

    const newMessage = document.createElement("div");
    newMessage.className = "message-content";
    newMessage.dataset.rawMarkdown = data.content;
    const tools = data.tools || [];
    if (data.from_model || tools.length) {
        // A model's reply is a flow: its name, then its words cut at each
        // tool line's `at` (8-tools.js renderFlow), so a reply still
        // streaming carries on in its last part.
        renderFlow(newMessage, data.content, tools, senderLabel(data.username));
    } else {
        newMessage.innerHTML = renderMarkdown(`${senderLabel(data.username)}\n\n${data.content}`);
    }

    // A reply still streaming gets its actions when it ends, like one we
    // watched from its first word.
    if (!data.streaming) {
        messageWrapper.appendChild(messageButtons(data.id, newMessage, data.from_model));
    }
    if (data.thinking) {
        messageWrapper.appendChild(thinkingBlock(data.thinking, Boolean(data.streaming) && !data.content));
    }
    messageWrapper.appendChild(newMessage);
    addVersionPager(messageWrapper, data.id, data.versions);
    return messageWrapper;
}

// A stored message put on our page: from history on a join, & when a
// deleted one is restored.
function showStoredMessage(data) {
    const chat = document.getElementById("chat");
    const existing = document.getElementById("message-" + data.id);
    // Already on our page (we rejoined): it stays, unless it was streaming
    // when we lost our connection or streams now. Then our server's copy
    // replaces ours: chunks we missed while away are in it, & a reply that
    // ended meanwhile gets its whole text & its actions.
    if (existing && !existing.dataset.streaming && !data.streaming) return;

    forgetStream(data.id);
    const messageWrapper = drawStoredMessage(data);
    if (existing) {
        existing.remove();
        placeById(messageWrapper, data.id);
    } else {
        chat.appendChild(messageWrapper);
    }
    dressCodeBlocks(messageWrapper.querySelector(":scope > .message-content"), true);

    if (data.streaming) {
        // Carry on from exactly here: every chunk sent before this
        // snapshot is in it, every chunk after it is appended to it.
        messageWrapper.dataset.streaming = "1";
        messageBuffers[data.id] = data.content;
        const stream = streamOf(data.id);
        stream.shown = data.content.length;
        stream.answered = Boolean(data.content);
    }

    if (!existing || !userHasScrolledUp) chat.scrollTop = chat.scrollHeight;
}

// Socket event for receiving previous messages: our room as our server
// holds it, sent on every join (the first & each reconnect).
socket.on("previous_messages", showStoredMessage);

// A reply's "Processing..." placeholder goes when its first chunk shows
// (thinking or words), else when it ends. Either way each reply takes one,
// the oldest (each reply sent one, in order), & nothing else, so notices &
// other replies' placeholders stay.
const placeholderGone = new Set();

function removeOldestPlaceholder() {
    const placeholder = document.querySelector('#chat [id^="message-processing-"]');
    if (placeholder) placeholder.remove();
}

socket.on("delete_processing_message", (msg_id) => {
    // A reply has ended: its placeholder goes now unless its first chunk
    // took it already.
    if (!placeholderGone.delete(msg_id)) removeOldestPlaceholder();
});

// A streaming reply's body: its header (which model) on top, then
// its content, a flow of text parts (.message-part) & tool lists
// (.message-tools) in the order they came. Made on its first chunk or
// tool line, whichever comes first.
function replyFlow(messageWrapper) {
    // A reply drawn from history (opened mid-reply) has its content as a
    // direct child: carry on in it rather than drawing a second one.
    const drawn = messageWrapper.querySelector(":scope > .message-content");
    if (drawn) return drawn;
    let body = messageWrapper.querySelector(".message-body");
    if (!body) {
        body = document.createElement("div");
        body.className = "message-body";
        messageWrapper.appendChild(body);
    }
    if (!body.querySelector(".message-header")) {
        const header = document.createElement("div");
        header.className = "message-header";
        body.appendChild(header);
    }
    let content = body.querySelector(".message-content");
    if (!content) {
        content = document.createElement("div");
        content.className = "message-content";
        body.appendChild(content);
    }
    return content;
}

// Whose reply this is, in the same words & look as from history: the
// model's name, as senderLabel() writes every sender. Who asked is not kept
// with a reply, so a live header naming them changed on every reload.
function replyHeader(messageWrapper, model) {
    const header = messageWrapper.querySelector(".message-header");
    if (header && model && header.dataset.model !== model) {
        header.dataset.model = model;
        header.innerHTML = renderMarkdown(senderLabel(model));
    }
}

// The text part new words go into: the last one, unless a tool list came
// after it (or it is the reply's name). A new part starts at `from`, its
// offset into the reply's text.
function currentTextPart(content, from) {
    const last = content.lastElementChild;
    if (last && last.classList.contains("message-part") && !last.dataset.label) return last;
    const part = document.createElement("div");
    part.className = "message-part";
    part.dataset.from = from;
    content.appendChild(part);
    return part;
}

// A reply's words drawn again from `text`, whole: each tool list stays
// where it happened (its `at`, an offset into the text) & the reply's name
// stays on top. For a reply whose text on our page turned out not to be
// our server's.
function redrawWords(content, text) {
    const kept = [...content.children].filter((el) =>
        el.classList.contains("message-tools") || el.dataset.label);
    content.replaceChildren();
    let pos = 0;
    const addText = (upTo) => {
        const raw = text.slice(pos, upTo);
        if (raw.trim()) {
            const part = currentTextPart(content, pos);
            part.innerHTML = renderMarkdown(raw);
        }
        pos = upTo;
    };
    for (const el of kept) {
        if (el.classList.contains("message-tools")) {
            const at = Number(el.dataset.at);
            if (Number.isInteger(at) && at > pos) addText(Math.min(at, text.length));
        }
        content.appendChild(el);
    }
    addText(text.length);
    dressCodeBlocks(content, false);
}

// Each streaming reply's words so far, by message id: what our server has
// sent, whether or not it is on the page yet.
const messageBuffers = {};
// What has arrived for each streaming reply & waits for our next frame.
const streams = {};

function streamOf(id) {
    if (!streams[id]) {
        streams[id] = {
            id,
            thinking: "",       // thinking not on the page yet
            shown: 0,           // how much of messageBuffers[id] is
            words: false,       // an answer chunk waits to be drawn
            answered: false,    // its answer has begun (thinking collapsed)
            model: null,
            keepThinkingOpen: false,
            complete: null,     // its completion event, once it came
            renderAfter: 0,     // no markdown render before this (ms)
        };
    }
    return streams[id];
}

function forgetStream(id) {
    delete streams[id];
    delete messageBuffers[id];
}

// A streaming reply's element, made on its first chunk or tool line.
function streamingWrapper(id) {
    let messageWrapper = document.getElementById("message-" + id);
    if (!messageWrapper) {
        messageWrapper = document.createElement("div");
        messageWrapper.className = "message-wrapper is-model";
        messageWrapper.id = "message-" + id;
        document.getElementById("chat").appendChild(messageWrapper);
    }
    messageWrapper.dataset.streaming = "1";
    return messageWrapper;
}

// Socket event for receiving chunks of a message. A chunk only joins what
// is waiting: our page is written at most once a frame (flushStreams),
// however fast chunks come. Written per chunk, each one cost more as a
// reply grew (6 ms at 1.5k characters of thinking, 42 ms at 15k, chunks
// 19 ms apart), our page fell behind its socket until the connection
// dropped, & the reply ended short (fox, 2026-11-10).
socket.on("message_chunk", (data) => {
    // Reasoning channel (delta.reasoning_content): its own block above the
    // answer. With Thinking OFF the server already suppresses reasoning at
    // the model, but if a model ignores the switch and streams anyway,
    // drop it.
    if (data.reasoning_content && !enableThinking) return;
    const messageWrapper = streamingWrapper(data.id);
    const stream = streamOf(data.id);

    if (data.reasoning_content) {
        stream.thinking += data.reasoning_content;
    } else {
        if (!(data.id in messageBuffers)) {
            // A reply history drew & no one told us was streaming: its new
            // words join the ones it has, never replace them.
            const drawn = messageWrapper.querySelector(":scope > .message-content");
            messageBuffers[data.id] = (drawn && drawn.dataset.rawMarkdown) || "";
            stream.shown = messageBuffers[data.id].length;
        }
        messageBuffers[data.id] += data.content || "";
        stream.words = true;
        if (data.model_name) stream.model = data.model_name;
        if (data.keep_thinking_open) stream.keepThinkingOpen = true;
        if (data.is_complete) stream.complete = data;
    }

    // Its first chunk to show takes its placeholder: thinking & words say
    // more than "Processing..." beside them.
    if ((data.reasoning_content || data.content) && !placeholderGone.has(data.id)) {
        placeholderGone.add(data.id);
        removeOldestPlaceholder();
    }
    scheduleFlush();
});

// One write per frame. requestAnimationFrame pauses in a hidden tab, so a
// timer stands behind it: whichever comes first draws, & a reply finishing
// in a background tab still ends whole (& chimes).
const STREAM_TIMER_MS = 250;
let flushFrame = null;
let flushTimer = null;
let flushLater = null;

function scheduleFlush() {
    if (flushFrame !== null || flushTimer !== null) return;
    const run = () => flushStreamNow(false);
    if (typeof requestAnimationFrame === "function" && !document.hidden) {
        flushFrame = requestAnimationFrame(run);
    }
    flushTimer = setTimeout(run, STREAM_TIMER_MS);
}

// Draw everything waiting, now. Called before anything that must land
// after the words so far (a tool line, a reply's links) & by our tests,
// which have no frames to wait for. `force` skips the render throttle.
function flushStreamNow(force = true) {
    if (flushFrame !== null) cancelAnimationFrame(flushFrame);
    if (flushTimer !== null) clearTimeout(flushTimer);
    flushFrame = flushTimer = null;
    flushStreams(force);
}

// How many times our page was written for streamed chunks (our tests read
// it: far fewer than chunks).
let streamWrites = 0;

function flushStreams(force) {
    let wrote = false;
    let wait = 0;
    const now = performance.now();
    for (const stream of Object.values(streams)) {
        if (!stream.thinking && !stream.words) continue;
        const messageWrapper = document.getElementById("message-" + stream.id);
        if (!messageWrapper) {  // deleted while it streamed
            forgetStream(stream.id);
            continue;
        }
        if (stream.thinking) {
            showThinking(messageWrapper, stream);
            wrote = true;
        }
        if (!stream.words) continue;
        // Markdown is rendered whole each time: a long answer waits a few
        // times what its last render cost, so rendering never takes most
        // of our page's time. Its end never waits.
        if (!force && !stream.complete && now < stream.renderAfter) {
            wait = Math.max(wait, stream.renderAfter - now);
            continue;
        }
        showWords(messageWrapper, stream);
        wrote = true;
    }
    if (wrote) {
        streamWrites++;
        // Follow the stream only for a reader at our newest message.
        if (!userHasScrolledUp) {
            const chat = document.getElementById("chat");
            chat.scrollTop = chat.scrollHeight;
        }
    }
    if (wait && flushLater === null) {
        flushLater = setTimeout(() => {
            flushLater = null;
            scheduleFlush();
        }, wait);
    }
}

function showThinking(messageWrapper, stream) {
    let details = messageWrapper.querySelector(".message-thinking");
    if (!details) {
        details = thinkingBlock("", true);
        // Above the answer: before a reply history drew, else on top of
        // our streaming body.
        const drawn = messageWrapper.querySelector(":scope > .message-content");
        if (drawn) {
            messageWrapper.insertBefore(details, drawn);
        } else {
            let body = messageWrapper.querySelector(".message-body");
            if (!body) {
                body = document.createElement("div");
                body.className = "message-body";
                messageWrapper.appendChild(body);
            }
            body.insertBefore(details, body.firstChild);
        }
    }
    details.querySelector(".message-thinking-body").firstChild.appendData(stream.thinking);
    stream.thinking = "";
}

function showWords(messageWrapper, stream) {
    const id = stream.id;
    const content = replyFlow(messageWrapper);
    stream.words = false;

    // The answer began: collapse its thinking, once, so the answer is the
    // visible focus (thinking stays one click away, & a reader who opens it
    // again keeps it open). A reply our server stopped (a loop, the token
    // cap) keeps it open: the thinking is what shows why.
    const details = messageWrapper.querySelector(".message-thinking");
    if (details && stream.keepThinkingOpen) {
        labelThinking(details, "stopped");
    } else if (details && !stream.answered) {
        details.open = false;
        labelThinking(details, "");
    }
    stream.answered = true;
    replyHeader(messageWrapper, stream.model);

    // The text since the last tool line is its own part, so text & tools
    // read in the order they came (8-tools.js adds the tool lists).
    const text = messageBuffers[id];
    if (text.length > stream.shown) {
        const started = performance.now();
        const part = currentTextPart(content, stream.shown);
        part.innerHTML = renderMarkdown(text.slice(Number(part.dataset.from) || 0));
        dressCodeBlocks(part, false);
        stream.shown = text.length;
        const cost = performance.now() - started;
        stream.renderAfter = performance.now() + Math.min(500, cost * 3);
    }
    // What Copy & Edit use (without header, for clean editing)
    content.dataset.rawMarkdown = text;

    if (stream.complete) finishReply(messageWrapper, content, stream);
}

// A reply ended. Our server's whole text came with the signal (`final`) &
// is what we keep: a chunk we never got (a dropped socket) is put right
// here, never left as a short reply marked complete.
function finishReply(messageWrapper, content, stream) {
    const id = stream.id;
    const data = stream.complete;
    if (typeof data.final === "string" && data.final !== messageBuffers[id]) {
        messageBuffers[id] = data.final;
        content.dataset.rawMarkdown = data.final;
        redrawWords(content, data.final);
        if (messageWrapper.sharedLinks) applyMessageLinks(messageWrapper, messageWrapper.sharedLinks);
    }
    replyHeader(messageWrapper, data.model_name);
    let details = messageWrapper.querySelector(".message-thinking");
    if (enableThinking && typeof data.thinking === "string" && data.thinking) {
        if (!details) {
            stream.thinking = data.thinking;
            showThinking(messageWrapper, stream);
            details = messageWrapper.querySelector(".message-thinking");
            details.open = false;
        } else {
            const node = details.querySelector(".message-thinking-body").firstChild;
            if (node.data !== data.thinking) node.data = data.thinking;
        }
    }
    if (details && details.dataset.state === "streaming") labelThinking(details, "");
    const text = messageBuffers[id];
    delete messageWrapper.dataset.streaming;
    forgetStream(id);

    // Its actions, unless it has them (a reply history drew complete).
    if (messageWrapper.querySelector(".button-container")) return;
    const buttonContainer = messageButtons(id, content, true);
    const playButton = Array.from(buttonContainer.querySelectorAll("button")).find(btn => btn.textContent === "Play");
    // Read aloud from the page: clean text without header
    const spoken = () => content.textContent || content.innerText || "";
    playButton.onclick = () => speakText(spoken(), playButton, id);
    // Before header and content
    messageWrapper.insertBefore(buttonContainer, messageWrapper.firstChild);

    // Auto-play TTS if enabled, only once streaming finishes
    if (autoPlayTTS && text && text.trim() !== "") {
        setTimeout(() => queueTTS(spoken(), playButton, id), 50); // Small delay to let the message render
    }
}


// Take a message off the page & out of our speech queue & cache.
function removeMessageElement(messageId) {
    const data = {message_id: messageId};
    const messageElement = document.getElementById("message-" + data.message_id);
    if (messageElement) {
        messageElement.remove();
    }

    // Drop cached TTS audio for this message (keys are `${messageId}-${voice}`).
    // The trailing dash keeps "12-" from matching "120-...".
    const cachePrefix = data.message_id + "-";
    Object.keys(audioCache).forEach((key) => {
        if (key.startsWith(cachePrefix)) {
            delete audioCache[key];
        }
    });

    // Remove any pending queue entries for this message so the rest play in order.
    ttsQueue = ttsQueue.filter((item) => item.messageId != data.message_id);

    // If it's the message currently playing, stop it and advance the queue.
    if (currentQueuedMessageId != null && currentQueuedMessageId == data.message_id) {
        if (currentQueuedAudio) {
            currentQueuedAudio.pause();
            currentQueuedAudio.currentTime = 0;
            currentQueuedAudio = null;
        }
        currentQueuedMessageId = null;
        isPlayingTTS = false;
        setTimeout(processNextTTS, 10);
    }
}

// Follow-up suggestions: chips under a model's reply; a click sends that
// question. Only our newest reply keeps them.
function clearFollowups() {
    document.querySelectorAll(".followups").forEach((chips) => chips.remove());
}

socket.on("followup_suggestions", (data) => {
    const messageWrapper = document.getElementById("message-" + data.message_id);
    if (!messageWrapper || !(data.suggestions || []).length) return;
    // A newer message arrived meanwhile: these no longer follow anything.
    let later = messageWrapper.nextElementSibling;
    while (later && !/^message-\d+$/.test(later.id)) later = later.nextElementSibling;
    if (later) return;
    clearFollowups();
    const chips = document.createElement("div");
    chips.className = "followups";
    chips.setAttribute("aria-label", "Suggested follow-ups");
    data.suggestions.forEach((question) => {
        const chip = document.createElement("button");
        chip.type = "button";
        chip.className = "followup-chip";
        chip.textContent = question;
        chip.onclick = () => {
            clearFollowups();
            const textarea = document.getElementById("message");
            textarea.value = question;
            sendMessage();
        };
        chips.appendChild(chip);
    });
    messageWrapper.appendChild(chips);
    if (!userHasScrolledUp) {
        document.getElementById("chat").scrollTop = document.getElementById("chat").scrollHeight;
    }
});

// Put a message, or a deleted message's line, where its id belongs in our
// chat: before the first one with a larger id (messages are ordered by
// id), else at the end.
function placeById(element, messageId) {
    const chat = document.getElementById("chat");
    const later = [...chat.children].find((child) => {
        if (child === element) return false;
        const found = /^(?:message|deleted)-(\d+)$/.exec(child.id);
        return found && Number(found[1]) > Number(messageId);
    });
    chat.insertBefore(element, later || null);
}

// A deleted message leaves one quiet line in its place: who wrote it, who
// deleted it & Restore. Its words are gone from our page (the server keeps
// them, & sends them again only when someone restores it).
function deletedLine(data) {
    const line = document.createElement("div");
    line.className = "deleted-line";
    line.id = "deleted-" + data.message_id;
    const words = document.createElement("span");
    const author = data.username || "someone";
    words.textContent = data.deleted_by
        ? `A message from ${author} was deleted by ${data.deleted_by}.`
        : `A message from ${author} was deleted.`;
    line.appendChild(words);
    const restore = document.createElement("button");
    restore.type = "button";
    restore.className = "deleted-line-restore";
    restore.textContent = "Restore";
    restore.onclick = () => restoreMessage(data.message_id, room_name);
    line.appendChild(restore);
    return line;
}

// Socket event for when a message is deleted (& on join, for each message
// deleted lately): the message goes; while it can be restored a line
// takes its place.
socket.on("message_deleted", (data) => {
    const old = document.getElementById("deleted-" + data.message_id);
    if (old) old.remove();
    const wrapper = document.getElementById("message-" + data.message_id);
    const line = data.restorable ? deletedLine(data) : null;
    if (line && wrapper) wrapper.before(line);
    removeMessageElement(data.message_id);
    if (line && !wrapper) placeById(line, data.message_id);
});

// Someone restored a deleted message: it returns to its place for everyone.
socket.on("message_restored", (data) => {
    const chat = document.getElementById("chat");
    const line = document.getElementById("deleted-" + data.id);
    if (line) line.remove();
    if (document.getElementById("message-" + data.id)) return;
    const scrolledTo = chat.scrollTop;
    showStoredMessage(data);
    const wrapper = document.getElementById("message-" + data.id);
    placeById(wrapper, data.id);
    // Back in the middle of our chat, it never pulls a reader to the end.
    if (wrapper !== chat.lastElementChild || userHasScrolledUp) chat.scrollTop = scrolledTo;
});

socket.on("restore_refused", (data) => {
    chatTools.say(data.error);
});

// Edit & resend hid these (the edited message & everything after it); the
// server keeps them as an earlier branch, so we only take them off the page.
socket.on("messages_hidden", (data) => {
    (data.ids || []).forEach((id) => {
        removeMessageElement(id);
        // A deleted message in that branch can no longer be restored.
        const line = document.getElementById("deleted-" + id);
        if (line) line.remove();
    });
});

socket.on("edit_resend_refused", (data) => {
    chatTools.say(data.error);
});

// Socket event for when a message is updated
// Drop cached speech for every voice of a message (keys are `${id}-${voice}`).
function forgetCachedAudio(messageId) {
    const cachePrefix = messageId + "-";
    Object.keys(audioCache).forEach((key) => {
        if (key.startsWith(cachePrefix)) delete audioCache[key];
    });
}

// A finished reply's links to files in its chat's sandbox, now shared
// (app.chat_gpt, tools/sandbox.export_linked_files): each points at its
// attachment; a path with no file becomes words saying so. Swapped in
// place, so the reply's text & tool lines keep their order.
socket.on("message_links", (data) => {
    flushStreamNow();  // the links are in words that may not be drawn yet
    const wrapper = document.getElementById("message-" + data.id);
    if (!wrapper) return;
    // Kept: a reply redrawn at its end (finishReply) gets them again.
    wrapper.sharedLinks = data;
    applyMessageLinks(wrapper, data);
});

function applyMessageLinks(wrapper, data) {
    for (const [path, url] of Object.entries(data.links || {})) {
        wrapper.querySelectorAll("a").forEach((a) => { if (a.getAttribute("href") === path) a.setAttribute("href", url); });
        wrapper.querySelectorAll("img").forEach((img) => { if (img.getAttribute("src") === path) img.setAttribute("src", url); });
    }
    for (const path of data.missing || []) {
        wrapper.querySelectorAll("a, img").forEach((el) => {
            if (el.getAttribute("href") !== path && el.getAttribute("src") !== path) return;
            const said = document.createElement("span");
            said.className = "message-missing-file";
            const label = el.textContent || el.getAttribute("alt") || path;
            said.textContent = path.startsWith("/attachments/")
                ? `${label} (no such file in this chat)`
                : `${label} (not shared: no file at ${path} in this chat's sandbox)`;
            el.replaceWith(said);
        });
    }
}

socket.on("message_updated", (data) => {
    forgetCachedAudio(data.message_id);
    // Find the existing message wrapper by ID
    const messageWrapper = document.getElementById("message-" + data.message_id);

    if (messageWrapper) {
        // Find the specific element that contains the message content
        const messageContentContainer = messageWrapper.querySelector(".message-content");

        // Update the message content; its tool lines stay, above the edit
        // (the text they sat between has changed).
        const toolLists = [...messageContentContainer.querySelectorAll(":scope > .message-tools")];
        messageContentContainer.innerHTML = renderMarkdown(data.content);
        toolLists.reverse().forEach((list) => messageContentContainer.prepend(list));

        // Update the raw markdown stored in the data attribute
        messageContentContainer.dataset.rawMarkdown = data.content;

        // Apply syntax highlighting and other functionalities to code blocks within the message
        // Don't truncate edited messages - only truncate on page load (previous_messages)
        messageContentContainer.querySelectorAll("pre code").forEach((block) => {
            hljs.highlightElement(block);
            addLineNumbers(block);
            addCopyButtonToCodeBlock(block, false);
        });
    }
});


// Function to enter edit mode
function editMessage(messageId, messageContentContainer) {
    // Store the current HTML in a data attribute
    messageContentContainer.dataset.originalHtml = messageContentContainer.innerHTML;
    const rawMarkdown = messageContentContainer.dataset.rawMarkdown;

    // Create a textarea for editing
    const textarea = document.createElement("textarea");
    textarea.value = rawMarkdown;
    textarea.rows = 1;
    textarea.className = "message-edit";

    // Replace the message content with the textarea
    messageContentContainer.innerHTML = '';
    messageContentContainer.appendChild(textarea);

    // Find the message wrapper to access the edit and save buttons
    const messageWrapper = messageContentContainer.closest('.message-wrapper');

    // Create a save button with the 'save-button' class
    const saveButton = document.createElement("button");
    saveButton.textContent = "Save";
    saveButton.className = "save-button"; // Add the class here
    saveButton.onclick = () => saveEditedMessage(messageId, textarea, messageContentContainer);

    // Change the edit button to a cancel button
    const editButton = messageWrapper.querySelector(".edit-button");
    editButton.textContent = "Cancel";
    editButton.onclick = () => cancelEdit(messageId, messageContentContainer);

    // Append the save button next to the cancel button
    editButton.after(saveButton);

    // A person's message can also be resent: models answer the edit again.
    // Model replies carry a Fork button & are only edited in place.
    if (!messageWrapper.querySelector(".fork-button")) {
        const resendButton = document.createElement("button");
        resendButton.textContent = "Save & resend";
        resendButton.className = "resend-button";
        resendButton.title = "Replace this message & everything after it with your edit, for everyone here; the models that answered answer again. Earlier versions are kept, hidden.";
        resendButton.onclick = () => resendEditedMessage(messageId, textarea, messageContentContainer);
        saveButton.after(resendButton);
    }

    // One line to start, growing with its text to EDIT_MAX_LINES (then it
    // scrolls), its Save & Cancel kept in view below it.
    const buttons = messageWrapper.querySelector(".button-container");
    const fit = () => {
        fitEditor(textarea);
        if (buttons) buttons.scrollIntoView({block: "nearest"});
    };
    textarea.addEventListener("input", fit);
    fit();
    textarea.focus();
    textarea.setSelectionRange(textarea.value.length, textarea.value.length);
}

// An editor as tall as its text, from one line to EDIT_MAX_LINES lines.
const EDIT_MAX_LINES = 14;
function fitEditor(textarea) {
    const style = getComputedStyle(textarea);
    const line = parseFloat(style.lineHeight) || 21;
    const frame = parseFloat(style.paddingTop) + parseFloat(style.paddingBottom) +
        parseFloat(style.borderTopWidth) + parseFloat(style.borderBottomWidth);
    textarea.style.height = "auto";
    const tallest = line * EDIT_MAX_LINES + frame;
    const wanted = textarea.scrollHeight + parseFloat(style.borderTopWidth) +
        parseFloat(style.borderBottomWidth);
    textarea.style.height = `${Math.min(wanted, tallest)}px`;
    textarea.style.overflowY = wanted > tallest ? "auto" : "hidden";
}

// Edit & resend: the server hides this message & what followed, posts the
// edit & has it answered again (messages_hidden, then chat_message).
function resendEditedMessage(messageId, textarea, messageContentContainer) {
    const content = textarea.value.trim();
    if (!content) return;
    socket.emit("edit_and_resend", {
        "message_id": messageId,
        "content": content,
        "room_name": room_name,
        "model": document.getElementById("model-select").value,
        "enable_thinking": enableThinking
    });
    // The editor stays open: messages_hidden takes it away once the server
    // resends, & a refusal (edit_resend_refused) leaves the draft to retry.
}

// Function to save the edited message
function saveEditedMessage(messageId, textarea, messageContentContainer) {
    // Get the updated markdown from the textarea
    const updatedMarkdown = textarea.value;

    // Emit the update_message event to the server
    socket.emit("update_message", {
        "message_id": messageId,
        "content": updatedMarkdown,
        "room_name": room_name
    });

    // Edited text needs fresh speech
    forgetCachedAudio(messageId);

    // Reset the edit button to its original state
    const messageWrapper = messageContentContainer.closest('.message-wrapper');
    const editButton = messageWrapper.querySelector(".edit-button");
    editButton.textContent = 'Edit';
    editButton.onclick = () => editMessage(messageId, messageContentContainer, updatedMarkdown);

    // Reset the play button to its initial state
    const playButton = Array.from(messageWrapper.querySelectorAll("button")).find(btn => btn.textContent === 'Pause' || btn.textContent === 'Play');
    if (playButton) {
        playButton.textContent = 'Play';
        playButton.onclick = () => speakText(updatedMarkdown, playButton, messageId); // Ensure it uses the updated content
    }

    // Remove the save & resend buttons
    messageWrapper.querySelectorAll(".save-button, .resend-button").forEach((button) => button.remove());
}

// Function to cancel the edit and revert changes
function cancelEdit(messageId, messageContentContainer) {
    // Restore the original HTML of the message content from the data attribute
    messageContentContainer.innerHTML = messageContentContainer.dataset.originalHtml;

    // Reset the edit button to its original state
    const messageWrapper = messageContentContainer.closest('.message-wrapper');
    const editButton = messageWrapper.querySelector(".edit-button");
    editButton.textContent = 'Edit';
    editButton.onclick = () => editMessage(messageId, messageContentContainer, messageContentContainer.dataset.rawMarkdown);

    // Remove the save & resend buttons
    messageWrapper.querySelectorAll(".save-button, .resend-button").forEach((button) => button.remove());
}
