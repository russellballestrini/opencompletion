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

// Load activities on page load
document.addEventListener('DOMContentLoaded', () => {
    refreshActivityList();
    
    // Request current activity status
    socket.emit("get_activity_status", {"room_name": room_name});
});
