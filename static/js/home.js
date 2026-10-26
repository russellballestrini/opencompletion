// Our home page: an empty chat. The first message makes a room, private
// (only you, needs sign in) or a chatroom (public, multiplayer), then opens
// it; chat's 3-room.js sends that message once the room is joined.
// Config comes from HOME_CONFIG in home.html.

const MODE_KEY = 'newChatMode';
const MODE_HINTS = {
    private: 'Private: only you can open this chat.',
    chatroom: 'Chatroom: anyone with the link can read & post, & it is listed under Public chats.',
};
const GUEST_PRIVATE_HINT = 'Sign in to keep chats private. As a guest you start a public chatroom.';

const composer = document.getElementById('composer');
const input = document.getElementById('composer-input');
const modelSelect = document.getElementById('composer-model');
const sendButton = document.getElementById('composer-send');
const hint = document.getElementById('mode-hint');
const errorEl = document.getElementById('composer-error');
const modeButtons = [...document.querySelectorAll('.mode-btn')];

function storedMode() {
    try { return localStorage.getItem(MODE_KEY); } catch (e) { return null; }
}

// Signed in, chats start private unless you chose chatroom; a guest can
// only start a chatroom (private rooms need an owner).
let mode = HOME_CONFIG.signedIn ? (storedMode() === 'chatroom' ? 'chatroom' : 'private') : 'chatroom';

function showMode(note) {
    modeButtons.forEach(button => {
        button.setAttribute('aria-pressed', String(button.dataset.mode === mode));
    });
    hint.textContent = note || MODE_HINTS[mode];
}

modeButtons.forEach(button => {
    button.addEventListener('click', () => {
        if (button.dataset.mode === 'private' && !HOME_CONFIG.signedIn) {
            showMode(GUEST_PRIVATE_HINT);
            return;
        }
        mode = button.dataset.mode;
        try { localStorage.setItem(MODE_KEY, mode); } catch (e) {}
        showMode();
        input.focus();
    });
});

// Models: the same list & saved choice chat uses (localStorage selectedModel).
function fillModels(models) {
    models.forEach(id => {
        const option = document.createElement('option');
        option.value = id;
        option.textContent = id;
        modelSelect.appendChild(option);
    });
    let saved = null;
    try { saved = localStorage.getItem('selectedModel'); } catch (e) {}
    const values = [...modelSelect.options].map(o => o.value);
    // A first visit gets a model that answers, not a silent room.
    modelSelect.value = values.includes(saved) ? saved : (models[0] || 'None');
}

fetch('/models')
    .then(response => response.json())
    .then(data => fillModels(data.models || []))
    .catch(() => {});

modelSelect.addEventListener('change', () => {
    try { localStorage.setItem('selectedModel', modelSelect.value); } catch (e) {}
});

// A readable, unique room name: the first words of the message plus a
// random tail, within our server's rule [a-z0-9][a-z0-9_-]{0,63}.
function newRoomName(message) {
    const words = slugify(message.split(/\s+/).slice(0, 6).join(' ')).replace(/[-_]+$/, '').slice(0, 40);
    const tail = Math.random().toString(36).slice(2, 8);
    return (words ? `${words}-` : 'chat-') + tail;
}

async function createRoom(message) {
    for (let attempt = 0; attempt < 3; attempt++) {
        const name = newRoomName(message);
        const response = await fetch('/api/rooms/create', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({name, is_private: mode === 'private'}),
        });
        const data = await response.json();
        if (response.ok) return data.room.name;
        if (data.error !== 'Room name already exists') throw new Error(data.error || 'Could not start a chat');
    }
    throw new Error('Could not find a free room name. Please try again.');
}

async function startChat() {
    // Enter can arrive again while a room is being made: one chat per send.
    if (sendButton.disabled) return;
    const message = input.value.trim();
    errorEl.textContent = '';
    if (!message) {
        input.focus();
        return;
    }
    sendButton.disabled = true;
    try {
        const room = await createRoom(message);
        // Without this handoff the new chat would open without our message.
        try {
            sessionStorage.setItem('pendingMessage', JSON.stringify({room, message, model: modelSelect.value}));
        } catch (e) {
            errorEl.textContent = 'This browser would not keep your message (storage blocked or full), so the chat was not opened. Allow site storage & try again.';
            sendButton.disabled = false;
            return;
        }
        try { localStorage.setItem('selectedModel', modelSelect.value); } catch (e) {}
        if (mode !== 'private') rememberRecentChat(room);
        // tests/functional/test_home.py records the address instead of leaving.
        const open = window.startChatNavigate || (url => { window.location.href = url; });
        open(`/chat/${encodeURIComponent(room)}`);
    } catch (error) {
        errorEl.textContent = error instanceof TypeError
            ? 'Network error: your chat was not started. Please try again.'
            : error.message;
        sendButton.disabled = false;
    }
}

composer.addEventListener('submit', event => {
    event.preventDefault();
    startChat();
});

// Enter sends, Shift+Enter makes a new line, our box grows as you type.
input.addEventListener('keydown', event => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
        event.preventDefault();
        startChat();
    }
});

input.addEventListener('input', () => {
    input.style.height = 'auto';
    input.style.height = `${Math.min(input.scrollHeight, 240)}px`;
});

showMode();
