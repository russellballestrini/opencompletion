/**
 * Utility functions for the OpenCompletion application
 */

/**
 * Convert a string to a URL-friendly slug
 * @param {string} str - The string to slugify
 * @returns {string} - The slugified string
 */
function slugify(str) {
    // Our server's rule for a new room name: [a-z0-9][a-z0-9_-]{0,63}
    return str.toLowerCase().replace(/\s+/g, '-').replace(/[^a-z0-9_-]+/g, '')
        .replace(/^[-_]+/, '').slice(0, 64);
}
// Public chats this browser opened, newest first: a guest's "Your chats"
// (signed-in people get theirs from the server). Names & titles only, never
// message text, and never a private room: whoever uses this browser after
// its owner signs out must not read one. Entries carry public: true; older
// unmarked ones (which may be private) are never shown.
const RECENT_CHATS_KEY = 'recentChats';
const RECENT_CHATS_MAX = 30;

function recentChats() {
    try {
        const list = JSON.parse(localStorage.getItem(RECENT_CHATS_KEY) || '[]');
        return Array.isArray(list)
            ? list.filter(c => c && typeof c.name === 'string' && c.public === true)
            : [];
    } catch (e) {
        return [];
    }
}

function rememberRecentChat(name, title) {
    const previous = recentChats().find(c => c.name === name);
    const entry = {name, title: title || (previous && previous.title) || null, public: true};
    const list = [entry, ...recentChats().filter(c => c.name !== name)].slice(0, RECENT_CHATS_MAX);
    try { localStorage.setItem(RECENT_CHATS_KEY, JSON.stringify(list)); } catch (e) {}
}

function forgetRecentChats() {
    try { localStorage.removeItem(RECENT_CHATS_KEY); } catch (e) {}
}

function forgetRecentChat(name) {
    const list = recentChats().filter(c => c.name !== name);
    try { localStorage.setItem(RECENT_CHATS_KEY, JSON.stringify(list)); } catch (e) {}
}

// One row of our sidebar's chat lists, the same markup _app_sidebar.html
// renders, built with DOM methods (a title is never HTML).
function chatListItem(room, tag, tagLabel) {
    const item = document.createElement('li');
    if (room.id) item.dataset.roomId = room.id;
    item.dataset.roomName = room.name;
    item.dataset.roomTitle = room.title || '';
    const link = document.createElement('a');
    link.href = `/chat/${encodeURIComponent(room.name)}`;
    link.title = room.title || room.name;
    const name = document.createElement('span');
    name.className = 'chat-list-name';
    name.textContent = room.title || room.name;
    link.appendChild(name);
    if (tag) {
        const badge = document.createElement('span');
        badge.className = 'chat-list-tag';
        badge.setAttribute('aria-label', tagLabel || tag);
        badge.textContent = tag;
        link.appendChild(badge);
    }
    item.appendChild(link);
    // Our chat actions menu (chat-actions.js): with no permissions known
    // here, it offers what anyone may do; our server decides the rest.
    const actions = document.createElement('button');
    actions.type = 'button';
    actions.className = 'chat-actions-btn';
    actions.dataset.chatActions = '';
    actions.setAttribute('aria-label', 'Chat actions');
    actions.title = 'Chat actions';
    actions.textContent = '⋯';
    item.appendChild(actions);
    return item;
}

// A guest's "Your chats" come from this browser's history; `current`
// names the open chat, if any. A room listed here leaves Public rooms, so
// no room shows twice (signed in, our server does the same).
function fillGuestChats(current) {
    const list = document.getElementById('your-chats');
    if (!list || list.dataset.signedIn === 'true') return;
    const chats = recentChats();
    const publicRows = [...document.querySelectorAll('#public-chats li[data-room-name]')];
    chats.forEach(chat => {
        const item = chatListItem(chat);
        if (chat.name === current) item.querySelector('a').setAttribute('aria-current', 'page');
        list.appendChild(item);
        publicRows.filter(row => row.dataset.roomName === chat.name).forEach(row => row.remove());
    });
    document.getElementById('your-chats-empty').hidden = chats.length > 0;
}

// "Your chats" shows the newest few, then "Show all (N)" opens the rest in
// place (fox, 2026-11-01); the open chat always shows. Signed in, the
// opened list ends with a link to every room of yours on our Rooms page.
const YOUR_CHATS_SHOWN = 5;

function collapseYourChats() {
    const list = document.getElementById('your-chats');
    if (!list || list.dataset.collapsed) return;
    const rows = [...list.children];
    const hidden = rows.slice(YOUR_CHATS_SHOWN).filter(row => !row.querySelector('[aria-current="page"]'));
    if (!hidden.length) return;
    list.dataset.collapsed = 'true';
    hidden.forEach(row => { row.hidden = true; });
    const more = document.createElement('button');
    more.type = 'button';
    more.className = 'sidebar-more sidebar-more-toggle';
    more.setAttribute('aria-expanded', 'false');
    more.setAttribute('aria-controls', 'your-chats');
    more.textContent = `Show all (${rows.length})`;
    let all = null;
    if (list.dataset.signedIn === 'true') {
        all = document.createElement('a');
        all.className = 'sidebar-more';
        all.href = '/browse?show=mine';
        all.textContent = 'All your rooms →';
        all.hidden = true;
    }
    more.addEventListener('click', () => {
        const open = more.getAttribute('aria-expanded') !== 'true';
        hidden.forEach(row => { row.hidden = !open; });
        more.setAttribute('aria-expanded', String(open));
        more.textContent = open ? 'Show fewer' : `Show all (${rows.length})`;
        if (all) all.hidden = !open;
    });
    list.after(more);
    if (all) more.after(all);
}

// A readable, unique room name: the first words of what the room is for
// plus a random tail, within our server's rule [a-z0-9][a-z0-9_-]{0,63}.
function newRoomName(words) {
    const lead = slugify(words.split(/\s+/).slice(0, 6).join(' ')).replace(/[-_]+$/, '').slice(0, 40);
    const tail = Math.random().toString(36).slice(2, 8);
    return (lead ? `${lead}-` : 'chat-') + tail;
}

// Make a new room named after `words` (home's first message, an
// activity's title): solo (only its owner, needs sign in) or a public
// chatroom. Resolves to our server's {id, name, is_private}.
async function createChatRoom(words, isPrivate) {
    for (let attempt = 0; attempt < 3; attempt++) {
        const name = newRoomName(words);
        const response = await fetch('/api/rooms/create', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({name, is_private: Boolean(isPrivate)}),
        });
        const data = await response.json();
        if (response.ok) return data.room;
        if (data.error !== 'Room name already exists') throw new Error(data.error || 'Could not start a chat');
    }
    throw new Error('Could not find a free room name. Please try again.');
}

// Our home page hands its first message to the chat page it opens (our
// Activities page its `/activity <path>` command the same way): kept in
// sessionStorage (this tab only) & sent once that room's page has joined.
const PENDING_MESSAGE_KEY = 'pendingMessage';

// False when this browser would not keep it (storage blocked or full).
function keepPendingMessage(room, message, model) {
    try {
        sessionStorage.setItem(PENDING_MESSAGE_KEY, JSON.stringify({room, message, model}));
        return true;
    } catch (e) {
        return false;
    }
}

function takePendingMessage(roomName) {
    let pending = null;
    try {
        pending = JSON.parse(sessionStorage.getItem(PENDING_MESSAGE_KEY) || 'null');
        if (pending && pending.room === roomName) {
            sessionStorage.removeItem(PENDING_MESSAGE_KEY);
            return pending;
        }
    } catch (e) {}
    return null;
}


// Voices for a voice picker (chat & home): our speech service's list
// (/speech/voices, a minute's cache), the voice picked before (localStorage
// selectedVoice), else our default, foxhop. Option values are "model:voice".
// The picker shows only when there are voices (fox, 2026-11-02: home had
// none). Play & Read replies aloud speak in it.
const DEFAULT_VOICE = 'foxhop';
const VOICES_CACHE_MS = 60 * 1000;

function defaultVoice(values) {
    const voiceOf = value => value.split(':').pop().toLowerCase();
    return values.find(v => voiceOf(v) === DEFAULT_VOICE)
        || values.find(v => voiceOf(v).includes(DEFAULT_VOICE))
        || values[0];
}

function fillVoiceSelect(select, voicesData) {
    select.innerHTML = '';
    ((voicesData && voicesData.data) || []).forEach(modelData => {
        const voices = modelData.voices || [];
        if (!voices.length) return;
        const optgroup = document.createElement('optgroup');
        optgroup.label = modelData.id;
        voices.forEach(voice => {
            const option = document.createElement('option');
            option.value = `${modelData.id}:${voice}`;
            // Its group already names the speech model: the closed picker
            // shows the voice alone, which fits a phone ("tts-…" said nothing).
            option.textContent = voice;
            optgroup.appendChild(option);
        });
        select.appendChild(optgroup);
    });
    const values = Array.from(select.options).map(o => o.value);
    let saved = null;
    try { saved = localStorage.getItem('selectedVoice'); } catch (e) {}
    const initial = values.includes(saved) ? saved : defaultVoice(values);
    if (initial) select.value = initial;
    select.hidden = values.length === 0;
}

function loadVoices(select) {
    if (!select) return;
    try {
        const cached = localStorage.getItem('voicesList');
        const until = parseInt(localStorage.getItem('voicesListExpiration'));
        if (cached && until && Date.now() < until) {
            fillVoiceSelect(select, JSON.parse(cached));
            return;
        }
    } catch (e) {}
    fetch('/speech/voices')
        .then(response => response.json())
        .then(voicesData => {
            fillVoiceSelect(select, voicesData);
            try {
                localStorage.setItem('voicesList', JSON.stringify(voicesData));
                localStorage.setItem('voicesListExpiration', Date.now() + VOICES_CACHE_MS);
            } catch (e) {}
        })
        .catch(() => {
            // No voices (speech down): the picker stays out of sight
            select.innerHTML = '';
            select.hidden = true;
        });
}
