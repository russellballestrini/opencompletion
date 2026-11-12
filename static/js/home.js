// Our home page: an empty chat. The first message makes a room, private
// (only you, needs sign in) or a chatroom (public, multiplayer), then opens
// it; chat's 3-room.js sends that message once the room is joined.
// Config comes from HOME_CONFIG in home.html.

const MODE_KEY = 'newChatMode';
// Each hint is [lead, rest]: on phones the rest starts its own line
// (.phone-line), so a hint breaks where it reads well, not mid-phrase.
const MODE_HINTS = {
    private: ['Solo: only you can open this chat.'],
    chatroom: ['Chatroom: anyone with the link can read & post,', '& it is listed under Public rooms.'],
};

const composer = document.getElementById('composer');
const input = document.getElementById('composer-input');
const modelSelect = document.getElementById('composer-model');
const sendButton = document.getElementById('composer-send');
const hint = document.getElementById('mode-hint');
const errorEl = document.getElementById('composer-error');
const statusEl = document.getElementById('composer-status');
// True from Send until the new chat's page takes over (showPending).
let pending = false;
const modeButtons = [...document.querySelectorAll('.mode-btn')];
// Attach & speak (static/js/composer.js), in our message box.
const homeTools = composerTools(composer, input);
// Home's unsent words, kept apart from every room's.
const homeDraft = keepDraft(input, 'home');

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
    // A model list landing mid-send must not unlock a second send.
    if (pending) {
        modelSelect.disabled = true;
        return;
    }
    modeButtons.forEach(button => {
        button.setAttribute('aria-pressed', String(button.dataset.mode === mode));
    });
    const locked = mode === 'private' && !HOME_CONFIG.signedIn;
    input.disabled = sendButton.disabled = locked;
    modelSelect.disabled = locked || homeModels.loading();
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
// Until our list arrives the picker says it is loading (utils.js
// modelsLoading) & Send waits for it.
const homeModels = modelsLoading(modelSelect);

function fillModels(models) {
    homeModels.done();
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
    // No list (our server did not answer): people only, & it says so.
    .catch(() => homeModels.done())
    .finally(() => showMode());

modelSelect.addEventListener('change', () => {
    try { localStorage.setItem('selectedModel', modelSelect.value); } catch (e) {}
});

// Voices: the same list & saved choice chat uses (utils.js loadVoices).
const voiceSelect = document.getElementById('composer-voice');
document.addEventListener('DOMContentLoaded', () => loadVoices(voiceSelect));
voiceSelect.addEventListener('change', () => {
    try { localStorage.setItem('selectedVoice', voiceSelect.value); } catch (e) {}
});

// Making a room & opening it takes a second or three: from the click
// until the chat page takes over, our box lifts into a pending card that
// keeps your words in view, Send spins & everything that could start a
// second chat is locked. A failure gives the box back, words intact.
function showPending(on) {
    pending = on;
    composer.classList.toggle('composer-pending', on);
    composer.setAttribute('aria-busy', String(on));
    sendButton.setAttribute('aria-busy', String(on));
    sendButton.setAttribute('aria-label', on ? 'Creating your chat' : 'Send');
    input.readOnly = on;  // readable, not greyed: still your words
    statusEl.hidden = !on;
    statusEl.textContent = on ? 'Creating your chat…' : '';
    if (on) {
        sendButton.disabled = modelSelect.disabled = true;
        composer.querySelectorAll('.composer-tool').forEach(tool => { tool.disabled = true; });
        modeButtons.forEach(button => { button.disabled = true; });
    } else {
        modeButtons.forEach(button => { button.disabled = false; });
        showMode();
    }
}

function failPending(message) {
    showPending(false);
    errorEl.textContent = message;
    input.focus();
}

// Back from the new chat (a page kept in memory): our box is ours again.
window.addEventListener('pageshow', event => {
    if (event.persisted && pending) showPending(false);
});

async function startChat() {
    // Enter can arrive again while a room is being made: one chat per send.
    if (pending || sendButton.disabled) return;
    let message = input.value.trim();
    errorEl.textContent = '';
    if (!message && !homeTools.hasFiles()) {
        input.focus();
        return;
    }
    // Seen at once, before any answer: no click that seems to do nothing.
    showPending(true);
    // A Send faster than our model list waits for it; a list that never
    // comes sends with the model picked before, never silently with none.
    let model = modelSelect.value;
    if (homeModels.loading()) {
        model = await homeModels.wait() ? modelSelect.value : savedModel();
        if (!model) {
            failPending(MODELS_NOT_LOADED);
            return;
        }
    }
    try {
        const made = await createChatRoom(message || homeTools.firstFileName(), mode === 'private');
        const room = made.name;
        // Files upload into the new chat & go as links below the text.
        if (homeTools.hasFiles()) {
            const links = await homeTools.uploadAll(room);
            message = [message, links].filter(Boolean).join('\n\n');
        }
        // Without this handoff the new chat would open without our message.
        if (!keepPendingMessage(room, message, model)) {
            failPending('This browser would not keep your message (storage blocked or full), so the chat was not opened. Allow site storage & try again.');
            return;
        }
        try { localStorage.setItem('selectedModel', model); } catch (e) {}
        if (mode !== 'private') rememberRecentChat(room, null, {id: made.id, anyoneMayRetitle: made.anyone_may_retitle});
        homeDraft.clear();  // sent: it lives in its chat now
        // tests/functional/test_home.py records the address instead of leaving.
        // Our pending card stays up until the chat page replaces this one.
        statusEl.textContent = 'Opening your chat…';
        const open = window.startChatNavigate || (url => { window.location.href = url; });
        open(`/chat/${encodeURIComponent(room)}`);
    } catch (error) {
        failPending(error instanceof TypeError
            ? 'Network error: your chat was not started. Please try again.'
            : error.message);
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
