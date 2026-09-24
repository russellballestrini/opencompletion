// Joining our room: model & voice pickers, users, sending, room list.
// Part of chat.html's script, split by concern; files load in order &
// share globals (see templates/chat.html). Config comes from CHAT_CONFIG.

// Function to sanitize the username
function sanitizeUsername(username) {
    // Split the username on commas and take the first part.
    // The backend denormalizes the user list in the room table via csv.
    return username.split(',')[0].trim();
}

// Function to copy message content to clipboard
function copyMessageContent(content) {
    navigator.clipboard.writeText(content).then(() => {
        // Optional: show a temporary success message
        const button = event.currentTarget;
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

document.addEventListener('DOMContentLoaded', (event) => {
    const chatContainer = document.getElementById("chat");
    const modelSelectDesktop = document.getElementById("model-select");
    const voiceSelectDesktop = document.getElementById("voice-select");
    
    // Initialize auto-play TTS button state from localStorage
    updateAutoPlayTTSDisplay();

    // Initialize show-thinking button state from localStorage
    updateShowThinkingDisplay();

    // Check for vision model availability (enables image hover descriptions)
    initVisionCapability();

    // Function to populate the model dropdown
    function populateModelDropdown(models) {
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

    // Function to populate the voice dropdown
    function populateVoiceDropdown(voicesData) {
        // Clear existing options
        voiceSelectDesktop.innerHTML = '';

        // Group voices by model
        const voicesByModel = {};
        voicesData.data.forEach(modelData => {
            const modelId = modelData.id;
            voicesByModel[modelId] = modelData.voices || [];
        });

        // Create optgroups for each model
        Object.entries(voicesByModel).forEach(([modelId, voices]) => {
            if (voices.length > 0) {
                const optgroup = document.createElement('optgroup');
                optgroup.label = modelId;

                voices.forEach(voice => {
                    const option = document.createElement('option');
                    option.value = `${modelId}:${voice}`;
                    option.textContent = `${modelId} - ${voice}`;
                    optgroup.appendChild(option);
                });

                voiceSelectDesktop.appendChild(optgroup);
            }
        });

        // Set initial value from localStorage or first available option (not URL)
        const initialVoice = localStorage.getItem('selectedVoice') || voiceSelectDesktop.options[0]?.value;
        if (initialVoice) {
            voiceSelectDesktop.value = initialVoice;
        }
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
        // Fetch from backend and update cache
        fetch('/models')
            .then(response => response.json())
            .then(data => {
                const models = data.models;
                populateModelDropdown(models);
                // Store in localStorage with expiration
                localStorage.setItem(cacheKey, JSON.stringify(models));
                localStorage.setItem(cacheExpirationKey, Date.now() + cacheDuration);
            })
            .catch(error => console.error("Error fetching models:", error));
    }

    // Fetch and populate voices with caching
    const voicesCacheKey = 'voicesList';
    const voicesCacheExpirationKey = 'voicesListExpiration';

    const cachedVoices = localStorage.getItem(voicesCacheKey);
    const cachedVoicesExpiration = localStorage.getItem(voicesCacheExpirationKey);

    if (cachedVoices && cachedVoicesExpiration && Date.now() < parseInt(cachedVoicesExpiration)) {
        // Use cached voices data
        const voicesData = JSON.parse(cachedVoices);
        populateVoiceDropdown(voicesData);
    } else {
        // Fetch voices from API
        fetch(VOICES_API_URL, {
            method: 'GET',
            headers: {
                'Authorization': `Bearer ${API_KEY}`
            }
        })
            .then(response => response.json())
            .then(voicesData => {
                populateVoiceDropdown(voicesData);
                // Store in localStorage with expiration
                localStorage.setItem(voicesCacheKey, JSON.stringify(voicesData));
                localStorage.setItem(voicesCacheExpirationKey, Date.now() + cacheDuration);
            })
            .catch(error => {
                console.error("Error fetching voices:", error);
                // Leave dropdown empty if API fails
                voiceSelectDesktop.innerHTML = '';
                    });
    }

    chatContainer.addEventListener('scroll', () => {
        const distanceFromBottom = chatContainer.scrollHeight - chatContainer.scrollTop - chatContainer.clientHeight;
        userHasScrolledUp = distanceFromBottom > 5;
    });

    // NOTE: Model and voice restoration happens in populateModelDropdown() and
    // populateVoiceDropdown() AFTER the async fetch completes. Don't set values
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
    const chat = document.getElementById("chat");
    chat.textContent = "This room is private. Sign in as its owner to open it.";
});

// Function to update the active and inactive user lists in the DOM
function updateUserLists(activeUsers, inactiveUsers) {
    const activeUserListElement = document.getElementById("active-users");
    const inactiveUserListElement = document.getElementById("inactive-users");

    activeUserListElement.innerHTML = ''; // Clear the current list
    inactiveUserListElement.innerHTML = ''; // Clear the current list

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
socket.on("active_users", (data) => {
    updateUserLists(data.active_users, data.inactive_users);
});

// Function to handle sending the message
function sendMessage() {
    const messageTextarea = document.getElementById("message");
    const message = messageTextarea.value;
    const model = document.getElementById("model-select").value;
    let messageToSend = message.trim();

    if (messageToSend !== "") {  // Ensure we're not sending empty messages
        socket.emit("chat_message", {
            "username": username,
            "message": messageToSend,
            "model": model,  // Pass model as a separate attribute
            "room_name": room_name,
            "enable_thinking": showThinking  // false => ask model to skip chain-of-thought
        });
        messageTextarea.value = "";
        // Reset textarea height after sending
        messageTextarea.style.height = 'auto';
    }
}

// Function to handle deleting a message
function deleteMessage(messageId, room_name) {
    socket.emit("delete_message", {"message_id": messageId, "room_name": room_name});
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
    // Reset height to auto to get the correct scrollHeight
    this.style.height = 'auto';
    // Set height to scrollHeight to fit content
    this.style.height = this.scrollHeight + 'px';
});

// Socket event for updating the room title
socket.on("update_room_title", (data) => {
    document.title = data.title; // Update the window's title
});

// Socket event to update the room title in the sidebar or add new rooms.
// Each item is <li data-room-id><a><b>name</b><br>title<br>N users</a></li>,
// the same markup base.html renders, built with DOM methods (no innerHTML).
function fillRoomLink(link, room, userCount) {
    link.textContent = '';
    const nameElement = document.createElement('b');
    nameElement.textContent = room.name;
    link.appendChild(nameElement);
    if (room.title) {
        link.appendChild(document.createElement('br'));
        link.appendChild(document.createTextNode(room.title));
    }
    if (userCount) {
        link.appendChild(document.createElement('br'));
        link.appendChild(document.createTextNode(`${userCount} users`));
    }
}

socket.on('update_room_list', function(updatedRoom) {
    const roomListItem = document.querySelector(`#rooms-list li[data-room-id="${updatedRoom.id}"]`);

    if (roomListItem) {
        const link = roomListItem.querySelector('a');
        const userCountText = link.textContent.match(/(\d+)\s*users/);
        fillRoomLink(link, updatedRoom, userCountText ? userCountText[1] : null);
    } else if (updatedRoom.is_new) {
        const targetList = updatedRoom.is_private
            ? document.querySelector('#private-rooms-section .rooms-list')
            : document.querySelector('#public-rooms-section .rooms-list');
        if (!targetList) return;

        const newLi = document.createElement('li');
        newLi.setAttribute('data-room-id', updatedRoom.id);
        newLi.classList.add(updatedRoom.is_private ? 'private-room' : 'public-room');
        const newLink = document.createElement('a');
        newLink.href = `/chat/${encodeURIComponent(updatedRoom.name)}`;
        fillRoomLink(newLink, updatedRoom, null);
        newLi.appendChild(newLink);
        targetList.prepend(newLi);
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
