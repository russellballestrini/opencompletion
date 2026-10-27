// Our home page: an empty chat. The first message makes a room, private
// (only you, needs sign in) or a chatroom (public, multiplayer), then opens
// it; chat's 3-room.js sends that message once the room is joined.
// Config comes from HOME_CONFIG in home.html.

const MODE_KEY = 'newChatMode';
// Each hint is [lead, rest]: on phones the rest starts its own line
// (.phone-line), so a hint breaks where it reads well, not mid-phrase.
const MODE_HINTS = {
    private: ['Solo: only you can open this chat.'],
    chatroom: ['Chatroom: anyone with the link can read & post,', '& it is listed under Public chats.'],
};

const composer = document.getElementById('composer');
const input = document.getElementById('composer-input');
const modelSelect = document.getElementById('composer-model');
const sendButton = document.getElementById('composer-send');
const hint = document.getElementById('mode-hint');
const errorEl = document.getElementById('composer-error');
const modeButtons = [...document.querySelectorAll('.mode-btn')];
// Attach & speak (static/js/composer.js), in our message box.
const homeTools = composerTools(composer, input);

function storedMode() {
    try { return localStorage.getItem(MODE_KEY); } catch (e) { return null; }
}

// Signed in, chats start private unless you chose chatroom; a guest
// starts in a chatroom (private rooms need an owner).
let mode = HOME_CONFIG.signedIn ? (storedMode() === 'chatroom' ? 'chatroom' : 'private') : 'chatroom';

function hintLine(lead, rest) {
    hint.textContent = lead;
    if (!rest) return;
    const line = document.createElement('span');
    line.className = 'phone-line';
    line.append(' ', ...[].concat(rest));
    hint.appendChild(line);
}

// A guest may pick Solo to see what it is, but can't start one: our
// message box, model & Send lock until they sign in or pick Chatroom.
function showMode() {
    modeButtons.forEach(button => {
        button.setAttribute('aria-pressed', String(button.dataset.mode === mode));
    });
    const locked = mode === 'private' && !HOME_CONFIG.signedIn;
    input.disabled = modelSelect.disabled = sendButton.disabled = locked;
    composer.querySelectorAll('.composer-tool').forEach(tool => { tool.disabled = locked; });
    composer.classList.toggle('composer-locked', locked);
    if (locked) {
        const signIn = document.createElement('a');
        signIn.href = '/auth?next=/';
        signIn.textContent = 'Sign in';
        hintLine('Solo chats need an account.', [signIn, ' or pick Chatroom.']);
    } else {
        hintLine(...MODE_HINTS[mode]);
    }
}

modeButtons.forEach(button => {
    button.addEventListener('click', () => {
        mode = button.dataset.mode;
        // Only an account's choice sticks: a guest always starts in a chatroom.
        if (HOME_CONFIG.signedIn) {
            try { localStorage.setItem(MODE_KEY, mode); } catch (e) {}
        }
        showMode();
        if (!input.disabled) input.focus();
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
    let message = input.value.trim();
    errorEl.textContent = '';
    if (!message && !homeTools.hasFiles()) {
        input.focus();
        return;
    }
    sendButton.disabled = true;
    try {
        const room = await createRoom(message || homeTools.firstFileName());
        // Files upload into the new chat & go as links below the text.
        if (homeTools.hasFiles()) {
            const links = await homeTools.uploadAll(room);
            message = [message, links].filter(Boolean).join('\n\n');
        }
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
