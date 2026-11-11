// Room background & activities.
// Part of chat.html's script, split by concern; files load in order &
// share globals (see templates/chat.html). Config comes from CHAT_CONFIG.

// Socket event for setting the chat background
socket.on("set_background", (data) => {
    // Use setTimeout to ensure background updates don't get blocked by TTS
    setTimeout(() => {
        const chat = document.getElementById("chat");
        chat.style.backgroundImage = `url('data:image/png;base64,${data.image_data}')`;
        chat.style.backgroundRepeat = "no-repeat";
        chat.style.backgroundPosition = "right center";
        chat.style.backgroundSize = "auto"; // Ensures the image is not stretched
        console.log("Background image updated");
    }, 0);
});

// Activity management functions
function refreshActivityList() {
    fetch('/api/activities')
        .then(response => response.json())
        .then(data => {
            const activitySelect = document.getElementById('activity-select');
            // Keep the placeholder option at index 0
            while (activitySelect.options.length > 1) {
                activitySelect.remove(1);
            }
            data.activities.forEach(activity => {
                const option = document.createElement('option');
                option.value = activity;
                option.textContent = activity;
                activitySelect.appendChild(option);
            });
        })
        .catch(error => {
            console.error('Error fetching activities:', error);
            alert('Failed to fetch activities');
        });
}

function loadSelectedActivity() {
    const selectedActivity = document.getElementById('activity-select').value;
    if (!selectedActivity) {
        alert('Please choose an activity');
        return;
    }
    closeDrawer();
    socket.emit("chat_message", {
        "username": username,
        "message": `/activity ${selectedActivity}`,
        "model": document.getElementById("model-select").value,
        "room_name": room_name
    });
}

function cancelActivity() {
    if (confirm('Cancel the running activity? Its progress in this room is deleted.')) {
        socket.emit("chat_message", {
            "username": username,
            "message": "/activity cancel",
            "model": document.getElementById("model-select").value,
            "room_name": room_name
        });
    }
}

// Socket event for activity status updates
socket.on("activity_status", (data) => {
    document.getElementById('current-activity-info').hidden = !data.active;
    document.getElementById('activity-list-section').hidden = !!data.active;
    if (data.active) {
        document.getElementById('current-activity-name').textContent = data.activity_name || 'Unknown';
    }
});

// A multiple-choice question's choices as buttons under it (ids & texts
// only; our server holds the answer & grades). One pick sends its id; a
// pick-every-one-that-fits question toggles ids, then Send sends them.
// The question already lists them, so typing an id works too.
function clearActivityChoices() {
    document.querySelectorAll(".activity-choices").forEach((row) => row.remove());
}

function sendActivityAnswer(text) {
    clearActivityChoices();
    const textarea = document.getElementById("message");
    textarea.value = text;
    sendMessage();
}

socket.on("activity_choices", (data) => {
    const messages = document.querySelectorAll('#chat [id^="message-"]');
    const question = messages[messages.length - 1];
    if (!question || !(data.choices || []).length) return;
    clearActivityChoices();
    const row = document.createElement("div");
    row.className = "followups activity-choices";
    row.setAttribute("role", "group");
    row.setAttribute("aria-label", data.multiple ? "Pick every one that fits" : "Pick one");
    const picked = new Set();
    data.choices.forEach((choice) => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "followup-chip";
        button.textContent = `${choice.id}: ${choice.text}`;
        if (data.multiple) {
            button.setAttribute("aria-pressed", "false");
            button.onclick = () => {
                const on = !picked.has(choice.id);
                if (on) picked.add(choice.id); else picked.delete(choice.id);
                button.setAttribute("aria-pressed", String(on));
                send.disabled = !picked.size;
            };
        } else {
            button.onclick = () => sendActivityAnswer(choice.id);
        }
        row.appendChild(button);
    });
    const send = document.createElement("button");
    if (data.multiple) {
        send.type = "button";
        send.className = "btn activity-choices-send";
        send.textContent = "Send";
        send.disabled = true;
        send.onclick = () => sendActivityAnswer(
            data.choices.map((c) => c.id).filter((id) => picked.has(id)).join(", "));
        row.appendChild(send);
    }
    question.appendChild(row);
    if (!userHasScrolledUp) {
        document.getElementById("chat").scrollTop = document.getElementById("chat").scrollHeight;
    }
});

// A later message (an answer typed instead, someone else's turn) moves our
// activity past these choices.
socket.on("chat_message", (data) => {
    if (!data || data.username !== "System") clearActivityChoices();
});

// Load activities on page load
document.addEventListener('DOMContentLoaded', () => {
    refreshActivityList();
    
    // Request current activity status
    socket.emit("get_activity_status", {"room_name": room_name});
});
