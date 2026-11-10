// Joining our room: model & voice pickers, users, sending, room list.
// Part of chat.html's script, split by concern; files load in order &
// share globals (see templates/chat.html). Config comes from CHAT_CONFIG.

// Function to sanitize the username
function sanitizeUsername(username) {
    // Split the username on commas and take the first part.
    // The backend denormalizes the user list in the room table via csv.
    return username.split(',')[0].trim();
}

// A message that is one fenced code block & nothing else copies as its code,
// without the fences (fox, 2026-11-08). Two blocks, words around one, or an
// unclosed fence (still streaming) copy as written: null.
function soleCodeBlockText(content) {
    const m = /^(`{3,}|~{3,})[^\n]*\n([\s\S]*?)\n?\1[ \t]*$/.exec((content || '').trim());
    if (!m) return null;
    const fence = new RegExp('^' + m[1][0] + '{' + m[1].length + ',}[ \\t]*$', 'm');
    return fence.test(m[2]) ? null : m[2];
}

// Function to copy message content to clipboard
// `button` is passed in: window.event is gone by the time the promise settles.
function copyMessageContent(content, button) {
    const code = soleCodeBlockText(content);
    navigator.clipboard.writeText(code === null ? content : code).then(() => {
        if (!button) return;
        const originalText = button.textContent;
        button.textContent = 'Copied!';
        setTimeout(() => {
            button.textContent = originalText;
        }, 2000);
    }).catch(err => {
        console.error('Error copying text: ', err);
    });
}

// Function to sync dropdowns and save to localStorage
function syncDropdownsAndQueryString() {
    const modelSelectDesktop = document.getElementById("model-select");
    const voiceSelectDesktop = document.getElementById("voice-select");
    // Get current values
    const currentModel = modelSelectDesktop.value;
    const currentVoice = voiceSelectDesktop.value;

    // Sync dropdowns
    modelSelectDesktop.value = currentModel;
    voiceSelectDesktop.value = currentVoice;

    // Save to localStorage for persistence
    localStorage.setItem('selectedModel', currentModel);
    localStorage.setItem('selectedVoice', currentVoice);
}

// Our composer grew & took height from the messages: at our newest message
// (or within 40px of it) before, we stay there; scrolled up, nothing moves.
// `scrollTopBefore` is where the messages were scrolled before the change,
// when our caller knows (measuring a textarea lets a browser move it).
// True when it pinned us to the bottom.
let chatPaneHeight = null;
function keepNewestInView(scrollTopBefore) {
    const chat = document.getElementById("chat");
    const before = chatPaneHeight;
    chatPaneHeight = chat.clientHeight;
    // First look, or more room: a browser keeps the bottom itself.
    if (before === null || chatPaneHeight >= before) return false;
    const top = scrollTopBefore === undefined ? chat.scrollTop : scrollTopBefore;
    if (chat.scrollHeight - top - before > 40) return false;
    chat.scrollTop = chat.scrollHeight;
    return true;
}

document.addEventListener('DOMContentLoaded', (event) => {
    const chatContainer = document.getElementById("chat");
    const modelSelectDesktop = document.getElementById("model-select");
    const voiceSelectDesktop = document.getElementById("voice-select");
    
    // Initialize auto-play TTS button state from localStorage
    updateAutoPlayTTSDisplay();

    // Initialize thinking button state from localStorage
    updateThinkingDisplay();
    updateDoneChimeDisplay();

    // Check for vision model availability (enables image hover descriptions)
    initVisionCapability();

    // Function to populate the model dropdown
    function populateModelDropdown(models) {
        chatModels.done();
        // Clear options starting from index 1 (preserve "None" at index 0)
        while (modelSelectDesktop.options.length > 1) {
            modelSelectDesktop.remove(1);
        }
        // Append new model options
        models.forEach(modelId => {
            const option = document.createElement('option');
            option.value = modelId;
            option.textContent = modelId;
            modelSelectDesktop.appendChild(option);
        });
        // Restore from localStorage (check if value exists in options)
        const storedModel = localStorage.getItem('selectedModel') || "None";
        const validOptions = Array.from(modelSelectDesktop.options).map(o => o.value);
        const modelToSelect = validOptions.includes(storedModel) ? storedModel : "None";
        modelSelectDesktop.value = modelToSelect;
    }

    // Memoization with localStorage (1-minute cache)
    const cacheKey = 'modelList';
    const cacheExpirationKey = 'modelListExpiration';
    const cacheDuration = 60 * 1000; // 1 minute in milliseconds

    const cachedData = localStorage.getItem(cacheKey);
    const cachedExpiration = localStorage.getItem(cacheExpirationKey);

    if (cachedData && cachedExpiration && Date.now() < parseInt(cachedExpiration)) {
        // Use cached data if it exists and hasn't expired
        const models = JSON.parse(cachedData);
        populateModelDropdown(models);
    } else {
        // Fetch from backend and update cache. Until it answers the picker
        // says it is loading (utils.js modelsLoading) & Send waits for it.
        chatModels = modelsLoading(modelSelectDesktop);
        fetch('/models')
            .then(response => response.json())
            .then(data => {
                const models = data.models;
                populateModelDropdown(models);
                // Store in localStorage with expiration
                localStorage.setItem(cacheKey, JSON.stringify(models));
                localStorage.setItem(cacheExpirationKey, Date.now() + cacheDuration);
            })
            .catch(error => {
                // No list: people only, & the picker says so.
                chatModels.done();
                console.error("Error fetching models:", error);
            });
    }

    // Voices: the same list, saved choice & default as our home page (utils.js)
    loadVoices(voiceSelectDesktop);

    // Scrolled up from our newest message: stop following the stream & offer ↓.
    const scrollBottomButton = document.getElementById('scroll-bottom-button');
    chatContainer.addEventListener('scroll', () => {
        const distanceFromBottom = chatContainer.scrollHeight - chatContainer.scrollTop - chatContainer.clientHeight;
        userHasScrolledUp = distanceFromBottom > 5;
        scrollBottomButton.hidden = distanceFromBottom < 120;
    });
    scrollBottomButton.addEventListener('click', () => {
        chatContainer.scrollTo({top: chatContainer.scrollHeight, behavior: 'smooth'});
        userHasScrolledUp = false;
    });

    // Whatever takes height from our messages (a long draft, attached
    // files, a phone's keyboard) keeps our newest message in view.
    keepNewestInView();
    new ResizeObserver(() => keepNewestInView()).observe(chatContainer);

    // NOTE: Model and voice restoration happens in populateModelDropdown() and
    // fillVoiceSelect() (utils.js) AFTER the async fetch completes. Don't set values
    // here or call syncDropdownsAndQueryString() - that would overwrite localStorage
    // with empty values before the dropdowns are populated.

    // Persist model & voice choices
    modelSelectDesktop.addEventListener("change", syncDropdownsAndQueryString);
    voiceSelectDesktop.addEventListener("change", syncDropdownsAndQueryString);
});

// Socket event when the user connects
socket.on("connect", () => {
    // Sanitize the username before joining
    const sanitizedUsername = sanitizeUsername(username);
    socket.emit("join", {"username": sanitizedUsername, "room_name": room_name});
});

// We hear our room now: remember it for our home page's "Your chats" and
// send the first message our home page handed over, if it was for here.
socket.on("joined", () => {
    if (!CHAT_CONFIG.isPrivate) rememberRecentChat(room_name);
    const pending = takePendingMessage(room_name);
    if (!pending || !pending.message) return;
    socket.emit("chat_message", {
        "username": username,
        "message": pending.message,
        "model": pending.model || "None",
        "room_name": room_name,
        "enable_thinking": enableThinking
    });
});

// The server decides our posting name: a signed-in display name, or a
// guest name that does not impersonate an account or a model.
socket.on("your_username", (data) => {
    if (data.username && data.username !== username) {
        username = data.username;
        if (!CHAT_CONFIG.signedIn) {
            localStorage.setItem(GUEST_USERNAME_KEY, username);
        }
    }
});

socket.on("access_denied", () => {
    forgetRecentChat(room_name);
    const chat = document.getElementById("chat");
    chat.textContent = "This is a solo chat. Sign in as its owner to open it.";
});

// Function to update the active and inactive user lists in the DOM
function updateUserLists(activeUsers, inactiveUsers) {
    const activeUserListElement = document.getElementById("active-users");
    const inactiveUserListElement = document.getElementById("inactive-users");

    activeUserListElement.innerHTML = ''; // Clear the current list
    inactiveUserListElement.innerHTML = ''; // Clear the current list
    // A heading over nobody says nothing: each list shows with its heading
    // only while it has someone in it.
    document.getElementById("active-users-list").hidden = !activeUsers.length;
    document.getElementById("inactive-users-list").hidden = !inactiveUsers.length;

    // Populate the list with active users (desktop)
    activeUsers.forEach(user => {
        const userItem = document.createElement("li");
        // If it's the logged-in user, make it clickable to profile
        if (user === username) {
            const userLink = document.createElement("a");
            userLink.href = "/profile";
            userLink.textContent = user;
            userLink.style.color = "var(--text-primary)";
            userLink.style.textDecoration = "none";
            userItem.appendChild(userLink);
        } else {
            userItem.textContent = user;
        }
        activeUserListElement.appendChild(userItem);
    });

    // Populate the list with inactive users (desktop)
    inactiveUsers.forEach(user => {
        const userItem = document.createElement("li");
        // If it's the logged-in user, make it clickable to profile
        if (user === username) {
            const userLink = document.createElement("a");
            userLink.href = "/profile";
            userLink.textContent = user;
            userLink.style.color = "var(--text-secondary)";
            userLink.style.textDecoration = "none";
            userItem.appendChild(userLink);
        } else {
            userItem.textContent = user;
        }
        inactiveUserListElement.appendChild(userItem);
    });

}

// Update user lists whenever the event is received
// Who came & went, folded into one quiet line at the bottom of the chat
// instead of a line per join (fox, 2026-11-01: trash piled up). A person
// who leaves & comes back within a minute (a refresh) cancels out; our own
// joins never show (Active users lists everyone).
const PRESENCE_SETTLE_MS = 60000;
let presenceLine = null;
const presenceShown = new Map();  // username -> what the line says they did
const presenceLast = new Map();   // username -> {action, at}, for refreshes

socket.on("presence", (data) => {
    if (!data || !data.username || data.username === username) return;
    const chat = document.getElementById("chat");
    // Anything said since starts a new line; the old one stays where it was.
    if (presenceLine && presenceLine !== chat.lastElementChild) {
        presenceLine = null;
        presenceShown.clear();
    }
    const now = Date.now();
    const last = presenceLast.get(data.username);
    presenceLast.set(data.username, {action: data.action, at: now});
    const refresh = last && last.action !== data.action && now - last.at < PRESENCE_SETTLE_MS;
    if (refresh && presenceShown.has(data.username)) presenceShown.delete(data.username);
    else presenceShown.set(data.username, data.action);
    if (!presenceShown.size) {
        if (presenceLine) presenceLine.remove();
        presenceLine = null;
        return;
    }
    if (!presenceLine) {
        presenceLine = document.createElement("div");
        presenceLine.className = "presence-line";
        chat.appendChild(presenceLine);
    }
    presenceLine.textContent = [...presenceShown].map(([name, action]) => `${name} ${action}`).join(" · ");
});

// The conversation's size, in the room controls (not a chat line).
socket.on("room_stats", (data) => {
    const stat = document.getElementById("room-token-estimate");
    if (!stat || !data || !Number.isFinite(data.tokens)) return;
    // The room's total, & what the next reply sends of its model's window:
    // a reply reads only the latest messages, so the total is no fullness.
    let text = `Conversation: about ${data.tokens.toLocaleString()} tokens`;
    if (Number.isFinite(data.next) && Number.isFinite(data.window)) {
        text += ` · next reply sends about ${data.next.toLocaleString()} of ${data.window.toLocaleString()}`;
    }
    stat.textContent = text;
    stat.hidden = false;
});

socket.on("active_users", (data) => {
    updateUserLists(data.active_users, data.inactive_users);
});

// Attach & speak (static/js/composer.js), in our message box.
const chatTools = composerTools(document.getElementById("message-form"), document.getElementById("message"));
// This room's unsent words, kept apart from home's & every other room's.
const chatDraft = keepDraft(document.getElementById("message"), "room:" + room_name);
// Enter or Send again while files upload must not send them twice.
let uploadingFiles = false;
// Our model picker while its list loads (set where the list is fetched);
// a Send in that window waits for it, once.
let chatModels = {loading: () => false, done() {}, wait: () => Promise.resolve(true)};
let waitingForModels = false;

// Function to handle sending the message. Attached files upload first & go
// as links below the text; if one fails, nothing is sent.
async function sendMessage() {
    const messageTextarea = document.getElementById("message");
    let model = document.getElementById("model-select").value;
    // A Send faster than our model list waits for it; a list that never
    // comes sends with the model picked before, never silently with none.
    if (chatModels.loading()) {
        if (waitingForModels) return;
        waitingForModels = true;
        const loaded = await chatModels.wait();
        waitingForModels = false;
        model = loaded ? document.getElementById("model-select").value : savedModel();
        if (!model) {
            chatTools.say(MODELS_NOT_LOADED);
            return;
        }
    }
    const message = messageTextarea.value;
    let messageToSend = message.trim();
    if (uploadingFiles) return;
    if (chatTools.hasFiles()) {
        uploadingFiles = true;
        try {
            const links = await chatTools.uploadAll(room_name);
            messageToSend = [messageToSend, links].filter(Boolean).join("\n\n");
        } catch (error) {
            chatTools.say(error.message);
            return;
        } finally {
            uploadingFiles = false;
        }
    }

    if (messageToSend !== "") {  // Ensure we're not sending empty messages
        socket.emit("chat_message", {
            "username": username,
            "message": messageToSend,
            "model": model,  // Pass model as a separate attribute
            "room_name": room_name,
            "enable_thinking": enableThinking  // false => ask model to skip chain-of-thought
        });
        chatDraft.clear();
        messageTextarea.value = "";
        // Reset textarea height after sending
        messageTextarea.style.height = 'auto';
        keepNewestInView();
    }
}

// Our Stop button shows while a reply streams in this room (the server
// counts them) & ends them all; what they wrote so far stays.
// When the last reply streaming here ends after a wait (15s or more), or
// while this tab is in the background, a soft chime says it is done.
let streamingSince = null;
const CHIME_AFTER_MS = 15000;
socket.on("replies_streaming", (data) => {
    const count = data && data.count;
    document.getElementById("stop-button").hidden = !count;
    if (count && streamingSince === null) streamingSince = Date.now();
    if (!count && streamingSince !== null) {
        const took = Date.now() - streamingSince;
        streamingSince = null;
        if (doneChime && (took >= CHIME_AFTER_MS || document.hidden)) playDoneChime();
    }
});

function stopReplies() {
    socket.emit("stop_replies", {"room_name": room_name});
}

// Function to handle deleting a message
function deleteMessage(messageId, room_name) {
    socket.emit("delete_message", {"message_id": messageId, "room_name": room_name});
}

// Undo a delete: the message returns for everyone in the room.
function restoreMessage(messageId, room_name) {
    socket.emit("restore_message", {"message_id": messageId, "room_name": room_name});
}

// Event listener for form submission to send a message
document.getElementById("message-form").addEventListener("submit", (e) => {
    e.preventDefault();
    sendMessage();
});

// Event listener for the Enter key press in the textarea to send a message
document.getElementById("message").addEventListener("keydown", function(e) {
    if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        sendMessage();
    }
});

// Auto-grow textarea as user types
document.getElementById("message").addEventListener("input", function() {
    const chat = document.getElementById("chat");
    const scrolledTo = chat.scrollTop;
    // Reset height to auto to get the correct scrollHeight
    this.style.height = 'auto';
    // Set height to scrollHeight to fit content
    this.style.height = this.scrollHeight + 'px';
    // A taller draft never covers our newest message; someone reading
    // further up stays where they were.
    if (!keepNewestInView(scrolledTo)) chat.scrollTop = scrolledTo;
});

// Socket event for updating the room title
socket.on("update_room_title", (data) => {
    document.title = data.title; // Update the window's title
    document.getElementById("chat-title").textContent = data.title;
    if (!CHAT_CONFIG.isPrivate) rememberRecentChat(room_name, data.title);
    // Every row showing this room, a guest's (no id, from this browser's
    // history) as much as a signed-in one's (chat-actions.js).
    if (data.title) showRoomTitle({name: room_name}, data.title);
});

// A new or retitled room, live in our sidebar (_app_sidebar.html). Rows
// come from chatListItem() in utils.js, the same markup the server renders.
socket.on('update_room_list', function(updatedRoom) {
    // A room sits in one list (yours, else public); rename every row of
    // it all the same, found by its name: a guest's rows (this browser's
    // history) may carry no id.
    const rows = document.querySelectorAll(`#app-sidebar li[data-room-name="${CSS.escape(updatedRoom.name)}"]`);
    if (rows.length) {
        if (updatedRoom.title) {
            showRoomTitle(updatedRoom, updatedRoom.title);
            retitleRecentChat(updatedRoom.name, updatedRoom.title);
        }
    } else if (updatedRoom.is_new) {
        const list = document.getElementById(updatedRoom.is_private ? 'your-chats' : 'public-chats');
        if (!list) return;
        // A solo room's news reaches only its owner, who may do anything
        // with it; a public one offers what anyone may do.
        const room = {
            ...updatedRoom,
            canRetitle: updatedRoom.is_private || updatedRoom.anyone_may_retitle === true,
            canRemove: Boolean(updatedRoom.is_private),
        };
        list.prepend(chatListItem(room, updatedRoom.is_private ? '🔒' : '', 'solo'));
        if (updatedRoom.is_private) document.getElementById('your-chats-empty').hidden = true;
    }
});

// Helper function to enable and wire download button
function enableDownloadButton(messageId, playButton, audioUrl, voice) {
    const messageWrapper = playButton.closest('.message-wrapper');
    if (!messageWrapper) return;

    const downloadButton = messageWrapper.querySelector('.tts-download-button');
    // Use == instead of === because dataset values are strings, messageId might be number
    if (downloadButton && downloadButton.dataset.messageId == messageId) {
        downloadButton.style.display = "inline-block";
        downloadButton.onclick = () => {
            const link = document.createElement('a');
            link.href = audioUrl;
            link.download = `tts-${messageId}-${voice}.${AUDIO_FORMAT.format}`;
            link.click();
        };
    }
}
