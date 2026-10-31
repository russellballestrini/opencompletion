// Messages: new, historical & streamed chunks, edits & deletes.
// Part of chat.html's script, split by concern; files load in order &
// share globals (see templates/chat.html). Config comes from CHAT_CONFIG.

// Every message is markdown rendered by marked & sanitized by DOMPurify,
// images included (DOMPurify keeps data: image URLs & drops handlers such
// as onerror). Nothing a sender typed ever reaches innerHTML unsanitized.
function renderMarkdown(markdown) {
    return DOMPurify.sanitize(marked.marked(markdown), dompurify_config);
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

        // Create a container for the buttons
        const buttonContainer = document.createElement("div");
        buttonContainer.className = "button-container";

        // Create the delete button
        const deleteButton = document.createElement("button");
        deleteButton.textContent = "Delete";
        deleteButton.onclick = () => deleteMessage(data.id, room_name);
        buttonContainer.appendChild(deleteButton);

        // Create the edit button
        const editButton = document.createElement("button");
        editButton.textContent = "Edit";
        editButton.className = "edit-button";
        editButton.onclick = () => editMessage(data.id, newMessage, data.content);
        buttonContainer.appendChild(editButton);

        // Create the copy button
        const copyButton = document.createElement("button");
        copyButton.textContent = "Copy";
        copyButton.className = "copy-button";
        copyButton.onclick = () => copyMessageContent(data.content, copyButton);
        buttonContainer.appendChild(copyButton);

        // Create the play button for TTS
        const playButton = document.createElement("button");
        playButton.textContent = "Play";
        playButton.onclick = () => speakText(data.content, playButton, data.id);
        buttonContainer.appendChild(playButton);

        // Create the download button for TTS audio (hidden initially)
        const downloadButton = document.createElement("button");
        downloadButton.textContent = "Download";
        downloadButton.className = "tts-download-button";
        downloadButton.style.display = "none";
        downloadButton.dataset.messageId = data.id;
        buttonContainer.appendChild(downloadButton);

        messageWrapper.appendChild(buttonContainer);
    }

    messageWrapper.appendChild(newMessage);

    document.getElementById("chat").appendChild(messageWrapper);

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
// reply as Message.thinking) collapsed. Text only, never markup.
function thinkingBlock(text, open) {
    const details = document.createElement("details");
    details.className = "message-thinking";
    details.open = open;
    const summary = document.createElement("summary");
    summary.textContent = open ? "thinking…" : "thinking (click to expand)";
    details.appendChild(summary);
    const body = document.createElement("div");
    body.className = "message-thinking-body";
    body.style.opacity = "0.6";
    body.style.fontStyle = "italic";
    body.style.whiteSpace = "pre-wrap";
    body.textContent = text;
    details.appendChild(body);
    return details;
}

// Socket event for receiving previous messages
socket.on("previous_messages", (data) => {

    if (document.getElementById("message-" + data.id)) {
        // If it exists, skip appending it
        return;
    }

    const messageWrapper = document.createElement("div");
    messageWrapper.className = "message-wrapper";
    messageWrapper.id = "message-" + data.id;
    markSender(messageWrapper, data.username, data.from_model);

    const newMessage = document.createElement("div");
    newMessage.className = "message-content";

    newMessage.innerHTML = renderMarkdown(`${senderLabel(data.username)}\n\n${data.content}`);

    newMessage.dataset.rawMarkdown = data.content;

    // Create a container for the buttons
    const buttonContainer = document.createElement("div");
    buttonContainer.className = "button-container";

    // Create the delete button
    const deleteButton = document.createElement("button");
    deleteButton.textContent = "Delete";
    deleteButton.onclick = () => deleteMessage(data.id, room_name);
    buttonContainer.appendChild(deleteButton);

    // Create the edit button
    const editButton = document.createElement("button");
    editButton.textContent = "Edit";
    editButton.className = "edit-button";
    editButton.onclick = () => editMessage(data.id, newMessage);
    buttonContainer.appendChild(editButton);

    // Create the copy button
    const copyButton = document.createElement("button");
    copyButton.textContent = "Copy";
    copyButton.className = "copy-button";
    copyButton.onclick = () => copyMessageContent(data.content, copyButton);
    buttonContainer.appendChild(copyButton);

    // Create the play button for TTS
    const playButton = document.createElement("button");
    playButton.textContent = "Play";
    playButton.onclick = () => speakText(data.content, playButton, data.id);
    buttonContainer.appendChild(playButton);

    // Create the download button for TTS audio (hidden initially)
    const downloadButton = document.createElement("button");
    downloadButton.textContent = "Download";
    downloadButton.className = "tts-download-button";
    downloadButton.style.display = "none";
    downloadButton.dataset.messageId = data.id;
    buttonContainer.appendChild(downloadButton);
    if (data.from_model) addForkButton(buttonContainer, data.id);

    messageWrapper.appendChild(buttonContainer);
    if (data.thinking) messageWrapper.appendChild(thinkingBlock(data.thinking, false));
    // A reply that used tools reads in the order it happened: its text cut
    // at each tool line's `at` (8-tools.js renderFlow).
    if (data.tools && data.tools.length) {
        renderFlow(newMessage, data.content, data.tools, senderLabel(data.username));
    }
    messageWrapper.appendChild(newMessage);

    document.getElementById("chat").appendChild(messageWrapper);

    // Apply syntax highlighting to code blocks within the message
    newMessage.querySelectorAll("pre code").forEach((block) => {
        const wasTruncated = truncateCodeBlock(block);
        hljs.highlightElement(block);
        addLineNumbers(block);
        addCopyButtonToCodeBlock(block, wasTruncated);
    });

    // Scroll to the bottom of the chat container
    document.getElementById("chat").scrollTop = document.getElementById("chat").scrollHeight;

});

// Socket event for deleting a processing message
socket.on("delete_processing_message", (msg_id) => {
    // A reply has ended: its "Processing..." placeholder goes, the oldest
    // one (each reply sent one, in order), & nothing else, so notices &
    // other replies' placeholders stay.
    const placeholder = document.querySelector('#chat [id^="message-processing-"]');
    if (placeholder) placeholder.remove();
    // Clear the message buffer and header for the corresponding message ID
    delete messageBuffers[msg_id];
    delete messageHeaders[msg_id];
});

// A streaming reply's body: its header (who & which model) on top, then
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

function replyHeader(messageWrapper, name, model) {
    const header = messageWrapper.querySelector(".message-header");
    if (header && name && model) {
        header.innerHTML = renderMarkdown(`**${name} (${model}):**`);
    }
}

// The text part new words go into: the last one, unless a tool list came
// after it.
function currentTextPart(content) {
    const last = content.lastElementChild;
    if (last && last.classList.contains("message-part")) return last;
    const part = document.createElement("div");
    part.className = "message-part";
    content.appendChild(part);
    return part;
}

// A dictionary to hold buffers for each message ID
const messageBuffers = {};
// A dictionary to track message headers (username/model) for each message ID
const messageHeaders = {};

// Socket event for receiving chunks of a message
socket.on("message_chunk", (data) => {
    const wrapperId = "message-" + data.id;
    let messageWrapper = document.getElementById(wrapperId);
    let targetMessageElement;

    // If the message wrapper doesn't exist, create it
    if (!messageWrapper) {
        messageWrapper = document.createElement("div");
        messageWrapper.className = "message-wrapper is-model";
        messageWrapper.id = wrapperId;
        document.getElementById("chat").appendChild(messageWrapper);
    }

    // Reasoning channel: lazy-create a collapsible <details> block above
    // the message-content div. Only appears if the server actually streams
    // delta.reasoning_content (i.e., thinking is on AND the model is using it).
    // Auto-collapses below on the first content delta.
    // Fallback guard: with Thinking OFF the server already suppresses reasoning
    // at the model, but if a model ignores the switch and streams anyway, drop it.
    if (data.reasoning_content) {
        if (!enableThinking) return;
        let thinkingDetails = messageWrapper.querySelector(".message-thinking");
        if (!thinkingDetails) {
            // Ensure a message-body wrapper exists to anchor against
            let messageBodyWrapper = messageWrapper.querySelector(".message-body");
            if (!messageBodyWrapper) {
                messageBodyWrapper = document.createElement("div");
                messageBodyWrapper.className = "message-body";
                messageWrapper.appendChild(messageBodyWrapper);
            }
            thinkingDetails = thinkingBlock("", true);
            // Insert at top of message-body so thinking appears above the answer
            messageBodyWrapper.insertBefore(thinkingDetails, messageBodyWrapper.firstChild);
        }
        const body = thinkingDetails.querySelector(".message-thinking-body");
        body.textContent += data.reasoning_content;
        if (!userHasScrolledUp) {
            document.getElementById("chat").scrollTop = document.getElementById("chat").scrollHeight;
        }
        return;  // reasoning deltas don't touch buffer/markdown render
    }

    targetMessageElement = replyFlow(messageWrapper);

    // First content delta after thinking: auto-collapse the thinking block
    // so the answer is the visible focus. Thinking stays one click away. A
    // reply our server stopped (a loop, the token cap) keeps it open: the
    // thinking is what shows why.
    const thinkingDetails = messageWrapper.querySelector(".message-thinking");
    if (thinkingDetails && data.keep_thinking_open) {
        const summary = thinkingDetails.querySelector("summary");
        if (summary) summary.textContent = "thinking (where it stopped)";
    } else if (thinkingDetails && thinkingDetails.open) {
        thinkingDetails.open = false;
        const summary = thinkingDetails.querySelector("summary");
        if (summary) summary.textContent = "thinking (click to expand)";
    }

    // If the message buffer for this ID doesn't exist, create it
    if (!messageBuffers[data.id]) {
        messageBuffers[data.id] = "";
    }

    // Store header info on first chunk and update header element
    if (data.is_first_chunk && data.username && data.model_name) {
        messageHeaders[data.id] = {
            username: data.username,
            model_name: data.model_name
        };
        
        replyHeader(messageWrapper, data.username, data.model_name);
    }

    // Append the chunk to the buffer
    messageBuffers[data.id] += data.content;

    // The text since the last tool line is its own part, so text & tools
    // read in the order they came (8-tools.js adds the tool lists).
    const part = currentTextPart(targetMessageElement);
    part.dataset.raw = (part.dataset.raw || "") + data.content;
    part.innerHTML = renderMarkdown(part.dataset.raw);

    // Store the raw markdown in a data attribute for later use in editing (without header for clean editing)
    targetMessageElement.dataset.rawMarkdown = messageBuffers[data.id];

    // Apply syntax highlighting to code blocks within the content
    // Don't truncate streaming messages - only truncate on page load (previous_messages)
    part.querySelectorAll("pre code").forEach((block) => {
        hljs.highlightElement(block);
        addLineNumbers(block);
        addCopyButtonToCodeBlock(block, false);
    });

    // Scroll to the bottom of the chat container, but skip it if the user has scrolled up.
    if (!userHasScrolledUp) {
        document.getElementById("chat").scrollTop = document.getElementById("chat").scrollHeight;
    }

    // Check if the message is complete and add buttons if they haven't been added
    if (data.is_complete && !messageWrapper.querySelector(".button-container")) {
        // Create a container for the buttons
        const buttonContainer = document.createElement("div");
        buttonContainer.className = "button-container";

        // Create the delete button
        const deleteButton = document.createElement("button");
        deleteButton.textContent = "Delete";
        deleteButton.onclick = () => deleteMessage(data.id, room_name);
        buttonContainer.appendChild(deleteButton);

        // Create the edit button
        const editButton = document.createElement("button");
        editButton.textContent = "Edit";
        editButton.className = "edit-button";
        editButton.onclick = () => editMessage(data.id, targetMessageElement);
        buttonContainer.appendChild(editButton);

        // Create the copy button
        const copyButton = document.createElement("button");
        copyButton.textContent = "Copy";
        copyButton.className = "copy-button";
        copyButton.onclick = () => copyMessageContent(messageBuffers[data.id], copyButton);
        buttonContainer.appendChild(copyButton);

        // Create the play button for TTS
        const playButton = document.createElement("button");
        playButton.textContent = "Play";
        playButton.onclick = () => {
            // Use content from the content element (clean text without header)
            const cleanText = targetMessageElement.textContent || targetMessageElement.innerText || "";
            speakText(cleanText, playButton, data.id);
        };
        buttonContainer.appendChild(playButton);

        // Create the download button for TTS audio (hidden initially)
        const downloadButton = document.createElement("button");
        downloadButton.textContent = "Download";
        downloadButton.className = "tts-download-button";
        downloadButton.style.display = "none";
        downloadButton.dataset.messageId = data.id;
        buttonContainer.appendChild(downloadButton);
        // Every streamed message is a model's reply.
        addForkButton(buttonContainer, data.id);

        // Insert the button container at the beginning of the message wrapper (before header and content)
        messageWrapper.insertBefore(buttonContainer, messageWrapper.firstChild);
        
        // Auto-play TTS if enabled and message is complete (only when streaming finishes)
        if (autoPlayTTS && data.is_complete && messageBuffers[data.id] && messageBuffers[data.id].trim() !== "") {
            const playButton = Array.from(buttonContainer.querySelectorAll("button")).find(btn => btn.textContent === "Play");
            if (playButton) {
                setTimeout(() => {
                    // Use content from the content element (clean text without header)
                    const cleanText = targetMessageElement.textContent || targetMessageElement.innerText || "";
                    queueTTS(cleanText, playButton, data.id);
                }, 50); // Small delay to let the message render
            }
        }
    }
});


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

// Socket event for when a message is deleted
socket.on("message_deleted", (data) => removeMessageElement(data.message_id));

// Edit & resend hid these (the edited message & everything after it); the
// server keeps them as an earlier branch, so we only take them off the page.
socket.on("messages_hidden", (data) => {
    (data.ids || []).forEach(removeMessageElement);
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
