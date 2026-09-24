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

// Socket event for receiving a new message
socket.on("chat_message", (data) => {
    const messageWrapper = document.createElement("div");
    messageWrapper.className = "message-wrapper";
    messageWrapper.id = "message-" + data.id;

    const newMessage = document.createElement("div");
    newMessage.className = "message-content";

    // Prepend the sender's name when there is one
    const messageContent = data.username
        ? `${senderLabel(data.username)}\n\n${data.content}`
        : data.content;
    newMessage.innerHTML = renderMarkdown(messageContent);

    // Check if the message has an id which means we can delete it.
    if (data.id) {
        newMessage.dataset.rawMarkdown = data.content;

        // Create a container for the buttons
        const buttonContainer = document.createElement("div");
        buttonContainer.className = "button-container";

        // Create the delete button
        const deleteButton = document.createElement("button");
        deleteButton.innerHTML = "x";
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
        copyButton.onclick = () => copyMessageContent(data.content);
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
    if (data.id) {
        document.getElementById("chat").scrollTop = document.getElementById("chat").scrollHeight;

        // Check if this message should auto-execute (from auto-fix)
        if (window.pendingAutoExec) {
            const autoExecData = window.pendingAutoExec;
            window.pendingAutoExec = null; // Clear it so we don't re-execute

            // Find the code block that was just added
            const codeBlocks = newMessage.querySelectorAll("pre code");
            if (codeBlocks.length > 0) {
                // Get the first code block (should be the fixed code)
                const codeBlock = codeBlocks[0];

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

// Socket event for receiving previous messages
socket.on("previous_messages", (data) => {

    if (document.getElementById("message-" + data.id)) {
        // If it exists, skip appending it
        return;
    }

    const messageWrapper = document.createElement("div");
    messageWrapper.className = "message-wrapper";
    messageWrapper.id = "message-" + data.id;

    const newMessage = document.createElement("div");
    newMessage.className = "message-content";

    newMessage.innerHTML = renderMarkdown(`${senderLabel(data.username)}\n\n${data.content}`);

    newMessage.dataset.rawMarkdown = data.content;

    // Create a container for the buttons
    const buttonContainer = document.createElement("div");
    buttonContainer.className = "button-container";

    // Create the delete button
    const deleteButton = document.createElement("button");
    deleteButton.innerHTML = "x";
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
    copyButton.onclick = () => copyMessageContent(data.content);
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
    const tempMessages = document.querySelectorAll("#message-null");
    tempMessages.forEach((tempMessage) => {
        tempMessage.remove();
    });
    // Clear the message buffer and header for the corresponding message ID
    delete messageBuffers[msg_id];
    delete messageHeaders[msg_id];
});

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
        messageWrapper.className = "message-wrapper";
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
        if (!showThinking) return;
        let thinkingDetails = messageWrapper.querySelector(".message-thinking");
        if (!thinkingDetails) {
            // Ensure a message-body wrapper exists to anchor against
            let messageBodyWrapper = messageWrapper.querySelector(".message-body");
            if (!messageBodyWrapper) {
                messageBodyWrapper = document.createElement("div");
                messageBodyWrapper.className = "message-body";
                messageWrapper.appendChild(messageBodyWrapper);
            }
            thinkingDetails = document.createElement("details");
            thinkingDetails.className = "message-thinking";
            thinkingDetails.open = true;
            const summary = document.createElement("summary");
            summary.textContent = "thinking…";
            thinkingDetails.appendChild(summary);
            const body = document.createElement("div");
            body.className = "message-thinking-body";
            body.style.opacity = "0.6";
            body.style.fontStyle = "italic";
            body.style.whiteSpace = "pre-wrap";
            thinkingDetails.appendChild(body);
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

    // If the message-content div doesn't exist, create it
    if (!messageWrapper.querySelector(".message-content")) {
        // Create a message body wrapper to contain both header and content
        let messageBodyWrapper = messageWrapper.querySelector(".message-body");
        if (!messageBodyWrapper) {
            messageBodyWrapper = document.createElement("div");
            messageBodyWrapper.className = "message-body";
            messageWrapper.appendChild(messageBodyWrapper);
        }

        // Create header element for username/model
        const headerElement = document.createElement("div");
        headerElement.className = "message-header";
        messageBodyWrapper.appendChild(headerElement);

        // Create content element for actual message content
        targetMessageElement = document.createElement("div");
        targetMessageElement.className = "message-content";
        messageBodyWrapper.appendChild(targetMessageElement);
    } else {
        targetMessageElement = messageWrapper.querySelector(".message-content");
    }

    // First content delta after thinking: auto-collapse the thinking block
    // so the answer is the visible focus. Thinking stays one click away.
    const thinkingDetails = messageWrapper.querySelector(".message-thinking");
    if (thinkingDetails && thinkingDetails.open) {
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
        
        // Update header element
        const headerElement = messageWrapper.querySelector(".message-header");
        if (headerElement) {
            const headerContent = `**${data.username} (${data.model_name}):**`;
            headerElement.innerHTML = DOMPurify.sanitize(marked.marked(headerContent), dompurify_config);
        }
    }

    // Append the chunk to the buffer
    messageBuffers[data.id] += data.content;
    
    // Process just the content and set it in the content element
    const sanitizedContent = DOMPurify.sanitize(marked.marked(messageBuffers[data.id]), dompurify_config);
    targetMessageElement.innerHTML = sanitizedContent;

    // Store the raw markdown in a data attribute for later use in editing (without header for clean editing)
    targetMessageElement.dataset.rawMarkdown = messageBuffers[data.id];

    // Apply syntax highlighting to code blocks within the content
    // Don't truncate streaming messages - only truncate on page load (previous_messages)
    targetMessageElement.querySelectorAll("pre code").forEach((block) => {
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
        deleteButton.innerHTML = "x";
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
        copyButton.onclick = () => copyMessageContent(messageBuffers[data.id]);
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


// Socket event for when a message is deleted
socket.on("message_deleted", (data) => {
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
});

// Socket event for when a message is updated
socket.on("message_updated", (data) => {
    // Find the existing message wrapper by ID
    const messageWrapper = document.getElementById("message-" + data.message_id);

    if (messageWrapper) {
        // Find the specific element that contains the message content
        const messageContentContainer = messageWrapper.querySelector(".message-content");

        // Update the message content
        messageContentContainer.innerHTML = renderMarkdown(data.content);

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
    textarea.rows = 16;
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

    // Clear the cached audio for this message to recompute TTS
    if (audioCache[messageId]) {
        delete audioCache[messageId];
    }

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

    // Remove the save button using the 'save-button' class
    const saveButton = messageWrapper.querySelector(".save-button");
    if (saveButton) {
        saveButton.remove();
    }
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

    // Remove the save button using the 'save-button' class
    const saveButton = messageWrapper.querySelector(".save-button");
    if (saveButton) {
        saveButton.remove();
    }
}
