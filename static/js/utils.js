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
// Chats this browser opened, newest first: a guest's "Your chats" on our
// home page (signed-in people get theirs from the server). Names & titles
// only, never message text.
const RECENT_CHATS_KEY = 'recentChats';
const RECENT_CHATS_MAX = 30;

function recentChats() {
    try {
        const list = JSON.parse(localStorage.getItem(RECENT_CHATS_KEY) || '[]');
        return Array.isArray(list) ? list.filter(c => c && typeof c.name === 'string') : [];
    } catch (e) {
        return [];
    }
}

function rememberRecentChat(name, title) {
    const previous = recentChats().find(c => c.name === name);
    const entry = {name, title: title || (previous && previous.title) || null};
    const list = [entry, ...recentChats().filter(c => c.name !== name)].slice(0, RECENT_CHATS_MAX);
    try { localStorage.setItem(RECENT_CHATS_KEY, JSON.stringify(list)); } catch (e) {}
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
    return item;
}

// A guest's "Your chats" come from this browser's history; `current`
// names the open chat, if any.
function fillGuestChats(current) {
    const list = document.getElementById('your-chats');
    if (!list || list.dataset.signedIn === 'true') return;
    const chats = recentChats();
    chats.forEach(chat => {
        const item = chatListItem(chat);
        if (chat.name === current) item.querySelector('a').setAttribute('aria-current', 'page');
        list.appendChild(item);
    });
    document.getElementById('your-chats-empty').hidden = chats.length > 0;
}

// Our home page hands its first message to the chat page it opens: kept in
// sessionStorage (this tab only) & sent once that room's page has joined.
const PENDING_MESSAGE_KEY = 'pendingMessage';

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
