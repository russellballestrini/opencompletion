// Our chat actions: a ⋯ button on every chat in our sidebar & on every room
// card (browse, search) opens one menu: rename, refresh title (a model
// titles it afresh from its conversation), pin, archive & delete. What it
// offers comes from the row's data-* attributes (_room_actions_data.html);
// our server checks again. Pins live in this browser (localStorage), shown
// in our sidebar's Pinned section.

const PINNED_KEY = 'pinnedChats';

function pinnedChats() {
    try {
        const pins = JSON.parse(localStorage.getItem(PINNED_KEY) || '[]');
        return Array.isArray(pins) ? pins.filter(pin => pin && pin.name) : [];
    } catch (e) {
        return [];
    }
}

function savePinnedChats(pins) {
    try { localStorage.setItem(PINNED_KEY, JSON.stringify(pins)); } catch (e) {}
}

function isPinned(name) {
    return pinnedChats().some(pin => pin.name === name);
}

function togglePin(room) {
    const pins = pinnedChats();
    const at = pins.findIndex(pin => pin.name === room.name);
    if (at >= 0) pins.splice(at, 1);
    else pins.unshift({id: room.id || null, name: room.name, title: room.title || ''});
    savePinnedChats(pins);
    renderPinnedChats();
}

// Our sidebar's Pinned section: a copy of each pinned chat, newest pin first.
function renderPinnedChats() {
    const list = document.getElementById('pinned-chats');
    const section = document.getElementById('pinned-chats-section');
    if (!list || !section) return;
    list.replaceChildren();
    const current = decodeURIComponent((location.pathname.match(/^\/chat\/([^/]+)/) || [])[1] || '');
    pinnedChats().forEach(pin => {
        // The freshest title & permissions are on the chat's own row, if listed.
        const row = document.querySelector(`#your-chats li[data-room-name="${CSS.escape(pin.name)}"], #public-chats li[data-room-name="${CSS.escape(pin.name)}"]`);
        const item = row ? row.cloneNode(true) : chatListItem(pin);
        if (!row) item.dataset.roomName = pin.name;
        if (row) item.querySelector('.chat-list-name').textContent = row.querySelector('.chat-list-name').textContent;
        const link = item.querySelector('a');
        if (pin.name === current) link.setAttribute('aria-current', 'page');
        else link.removeAttribute('aria-current');
        list.appendChild(item);
    });
    section.hidden = !list.children.length;
}

// Where a row's room is described: an <li> in our sidebar or a card wrap.
function chatRow(button) {
    return button.closest('[data-room-name]');
}

function rowRoom(row) {
    const title = row.querySelector('.chat-list-name, .room-card-title');
    return {
        id: row.dataset.roomId ? Number(row.dataset.roomId) : null,
        name: row.dataset.roomName,
        title: row.dataset.roomTitle || (title ? title.textContent : ''),
        canRetitle: row.dataset.canRetitle === 'true' && !!row.dataset.roomId,
        canRemove: row.dataset.canRemove === 'true' && !!row.dataset.roomId,
    };
}

// Every place on this page showing `room` takes its new title.
function showRoomTitle(room, title) {
    document.querySelectorAll(`[data-room-name="${CSS.escape(room.name)}"]`).forEach(row => {
        row.dataset.roomTitle = title;
        const name = row.querySelector('.chat-list-name');
        if (name) name.textContent = title;
        const link = row.querySelector('a');
        if (link && name) link.title = title;
        const card = row.querySelector('.room-card');
        if (card) {
            // An untitled room's card has no title line yet: add one.
            let cardTitle = card.querySelector('.room-card-title');
            if (!cardTitle) {
                cardTitle = document.createElement('p');
                cardTitle.className = 'room-card-title';
                card.querySelector('.room-card-header').after(cardTitle);
            }
            cardTitle.textContent = title;
        }
    });
    const pins = pinnedChats();
    const pin = pins.find(p => p.name === room.name);
    if (pin) { pin.title = title; savePinnedChats(pins); }
}

// A room archived or deleted leaves this page (& the open chat, for home).
function dropRoom(room) {
    const pins = pinnedChats().filter(pin => pin.name !== room.name);
    savePinnedChats(pins);
    if (location.pathname === `/chat/${encodeURIComponent(room.name)}`) {
        window.location.href = '/';
        return;
    }
    document.querySelectorAll(`[data-room-name="${CSS.escape(room.name)}"]`).forEach(row => row.remove());
    renderPinnedChats();
}

async function roomAction(url, method, body) {
    const response = await fetch(url, {
        method,
        headers: {'Content-Type': 'application/json'},
        body: body === undefined ? undefined : JSON.stringify(body),
    });
    let data = {};
    try { data = await response.json(); } catch (e) {}
    if (!response.ok) throw new Error(data.error || 'That did not work. Please try again.');
    return data;
}

const CHAT_ACTIONS = [
    {
        label: 'Rename',
        show: room => room.canRetitle,
        run: async room => {
            const title = prompt('Rename this chat', room.title || room.name);
            if (title === null || !title.trim()) return;
            const data = await roomAction(`/api/rooms/${room.id}/title`, 'POST', {title});
            showRoomTitle(room, data.title);
        },
    },
    {
        label: 'Refresh title',
        hint: 'A model titles this chat afresh from its conversation',
        show: room => room.canRetitle,
        run: async room => {
            const data = await roomAction(`/api/rooms/${room.id}/title`, 'POST', {generate: true});
            showRoomTitle(room, data.title);
        },
    },
    {
        label: room => isPinned(room.name) ? 'Unpin' : 'Pin',
        hint: 'Pinned chats stay atop our sidebar in this browser',
        show: () => true,
        run: async room => togglePin(room),
    },
    {
        label: 'Archive',
        show: room => room.canRemove,
        run: async room => {
            if (!confirm('Archive this chat? It leaves every list & search, but nothing is deleted.')) return;
            await roomAction(`/api/rooms/${room.id}/archive`, 'POST');
            dropRoom(room);
        },
    },
    {
        label: 'Delete',
        danger: true,
        show: room => room.canRemove,
        run: async room => {
            if (!confirm('Delete this chat & every message in it? This cannot be undone.')) return;
            await roomAction(`/api/rooms/${room.id}/delete`, 'DELETE');
            dropRoom(room);
        },
    },
];

let openMenu = null;

function closeChatActions() {
    if (!openMenu) return;
    openMenu.menu.remove();
    openMenu.button.setAttribute('aria-expanded', 'false');
    openMenu = null;
}

function openChatActions(button) {
    const row = chatRow(button);
    if (!row) return;
    const room = rowRoom(row);
    const menu = document.createElement('div');
    menu.className = 'chat-actions-menu';
    menu.setAttribute('role', 'menu');
    CHAT_ACTIONS.filter(action => action.show(room)).forEach(action => {
        const item = document.createElement('button');
        item.type = 'button';
        item.setAttribute('role', 'menuitem');
        item.textContent = typeof action.label === 'function' ? action.label(room) : action.label;
        if (action.hint) item.title = action.hint;
        if (action.danger) item.classList.add('danger');
        item.addEventListener('click', async () => {
            closeChatActions();
            button.disabled = true;
            try {
                await action.run(room);
            } catch (error) {
                alert(error instanceof TypeError ? 'Network error. Please try again.' : error.message);
            } finally {
                button.disabled = false;
            }
        });
        menu.appendChild(item);
    });
    document.body.appendChild(menu);
    // Below the button, kept inside our window (fixed: our sidebar scrolls).
    const box = button.getBoundingClientRect();
    const width = menu.offsetWidth;
    const height = menu.offsetHeight;
    menu.style.left = `${Math.max(8, Math.min(box.right - width, window.innerWidth - width - 8))}px`;
    menu.style.top = `${box.bottom + height + 8 > window.innerHeight ? Math.max(8, box.top - height - 4) : box.bottom + 4}px`;
    button.setAttribute('aria-expanded', 'true');
    openMenu = {menu, button};
    menu.querySelector('button').focus();
}

document.addEventListener('click', event => {
    const button = event.target.closest('[data-chat-actions]');
    if (button) {
        event.preventDefault();
        event.stopPropagation();
        const same = openMenu && openMenu.button === button;
        closeChatActions();
        if (!same) openChatActions(button);
        return;
    }
    if (openMenu && !openMenu.menu.contains(event.target)) closeChatActions();
});

document.addEventListener('keydown', event => {
    if (event.key === 'Escape') closeChatActions();
});
window.addEventListener('resize', closeChatActions);
document.addEventListener('scroll', closeChatActions, true);

renderPinnedChats();
