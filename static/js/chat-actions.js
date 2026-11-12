// Our chat actions: a ⋯ button on every chat in our sidebar & on every room
// card (browse, search) opens one menu: rename, refresh title (a model
// titles it afresh from its conversation), archive & delete. What it
// offers comes from the row's data-* attributes (_room_actions_data.html);
// our server checks again.

// Where a row's room is described: an <li> in our sidebar or a card wrap.
function chatRow(button) {
    return button.closest('[data-room-name]');
}

function rowRoom(row) {
    const title = row.querySelector('.chat-list-name');
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
            // A card's heading is its title; its name (the slug) sits under
            // it, added the first time an untitled room gets a title.
            card.querySelector('.room-card-name').textContent = title;
            if (!card.querySelector('.room-card-slug')) {
                const slug = document.createElement('p');
                slug.className = 'room-card-slug';
                slug.textContent = room.name;
                card.querySelector('.room-card-header').after(slug);
            }
        }
    });
}

// Archiving or deleting a chat takes our server a second or three, so it
// shows at once: every row & card for that chat dims, strikes through &
// spins (.room-removing, inert so nothing in it is clicked twice), & a
// polite status says it for a screen reader. When the server agrees the
// rows fold away (or, for the open chat, home opens); when it does not,
// they come back as they were.
const LEAVE_MS = 260;  // style.css .room-leaving's transition, & a little

function roomRows(room) {
    return [...document.querySelectorAll(`[data-room-name="${CSS.escape(room.name)}"]`)];
}

// The chat this page shows (chat.html's CHAT_CONFIG), if it is `room`.
function isOpenRoom(room) {
    return (window.CHAT_CONFIG && CHAT_CONFIG.roomName === room.name)
        || location.pathname === `/chat/${encodeURIComponent(room.name)}`;
}

function announceRoomStatus(text) {
    let region = document.getElementById('room-action-status');
    if (!region) {
        region = document.createElement('p');
        region.id = 'room-action-status';
        region.className = 'visually-hidden';
        region.setAttribute('role', 'status');
        region.setAttribute('aria-live', 'polite');
        document.body.appendChild(region);
    }
    region.textContent = text;
}

// `doing` says what is happening ("Deleting"). Returns the undo for a failure.
function markRoomRemoving(room, doing) {
    const rows = roomRows(room);
    rows.forEach(row => {
        row.classList.add('room-removing');
        row.setAttribute('aria-busy', 'true');
        row.inert = true;
    });
    const leavingPage = isOpenRoom(room);
    if (leavingPage) document.body.classList.add('room-leaving-page');
    announceRoomStatus(`${doing} “${room.title || room.name}”…`);
    return () => {
        rows.forEach(row => {
            row.classList.remove('room-removing');
            row.removeAttribute('aria-busy');
            row.inert = false;
        });
        if (leavingPage) document.body.classList.remove('room-leaving-page');
        announceRoomStatus('');
    };
}

// Our server agreed (`done`: "Deleted"): the rows fold away, or home opens
// in place of the chat that is gone (its rows stay marked until it does).
function roomRemoved(room, done) {
    announceRoomStatus(`${done} “${room.title || room.name}”.`);
    if (isOpenRoom(room)) {
        // tests/functional record the address instead of leaving.
        (window.roomLeftNavigate || (url => { window.location.href = url; }))('/');
        return;
    }
    const still = matchMedia('(prefers-reduced-motion: reduce)').matches;
    roomRows(room).forEach(row => {
        if (still) {
            row.remove();
            return;
        }
        row.style.height = `${row.offsetHeight}px`;
        void row.offsetHeight;  // lay that height out, so the fold animates from it
        row.classList.add('room-leaving');
        setTimeout(() => row.remove(), LEAVE_MS);
    });
}

// Archive & Delete both: mark at once, ask our server, finish or undo.
async function removeRoom(room, url, method, doing, done) {
    const undo = markRoomRemoving(room, doing);
    try {
        await roomAction(url, method);
    } catch (error) {
        undo();
        throw error;
    }
    roomRemoved(room, done);
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
            const data = await roomAction(`/api/rooms/${room.id}/title`, 'POST', {title, name: room.name});
            showRoomTitle(room, data.title);
        },
    },
    {
        label: 'Refresh title',
        hint: 'A model titles this chat afresh from its conversation',
        show: room => room.canRetitle,
        run: async room => {
            const data = await roomAction(`/api/rooms/${room.id}/title`, 'POST', {generate: true, name: room.name});
            showRoomTitle(room, data.title);
        },
    },
    {
        label: 'Archive',
        show: room => room.canRemove,
        run: async room => {
            if (!confirm('Archive this chat? It leaves every list & search, but nothing is deleted.')) return;
            await removeRoom(room, `/api/rooms/${room.id}/archive`, 'POST', 'Archiving', 'Archived');
        },
    },
    {
        label: 'Delete',
        danger: true,
        show: room => room.canRemove,
        run: async room => {
            if (!confirm('Delete this chat & every message in it? This cannot be undone.')) return;
            await removeRoom(room, `/api/rooms/${room.id}/delete`, 'DELETE', 'Deleting', 'Deleted');
        },
    },
];

let openMenu = null;

// Closing hands keyboard focus back to the ⋯ that opened the menu (only
// for a keyboard: a pointer's focus stays where the pointer left it).
function closeChatActions(restoreFocus = false) {
    if (!openMenu) return;
    const {menu, button} = openMenu;
    menu.remove();
    button.setAttribute('aria-expanded', 'false');
    openMenu = null;
    if (restoreFocus && button.isConnected) button.focus();
}

function openChatActions(button) {
    const row = chatRow(button);
    if (!row) return;
    const room = rowRoom(row);
    const offered = CHAT_ACTIONS.filter(action => action.show(room));
    // Nothing to offer: never an empty menu, & no ⋯ promising one.
    if (!offered.length) {
        button.hidden = true;
        return;
    }
    const menu = document.createElement('div');
    menu.className = 'chat-actions-menu';
    menu.setAttribute('role', 'menu');
    offered.forEach(action => {
        const item = document.createElement('button');
        item.type = 'button';
        item.setAttribute('role', 'menuitem');
        item.textContent = typeof action.label === 'function' ? action.label(room) : action.label;
        if (action.hint) item.title = action.hint;
        if (action.danger) item.classList.add('danger');
        item.addEventListener('click', async event => {
            const byKeyboard = event.detail === 0;
            closeChatActions(byKeyboard);
            button.disabled = true;
            try {
                await action.run(room);
            } catch (error) {
                alert(error instanceof TypeError ? 'Network error. Please try again.' : error.message);
            } finally {
                button.disabled = false;
                // Disabling blurred it; give a keyboard its place back.
                if (byKeyboard && button.isConnected) button.focus();
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
    if (event.key === 'Escape') closeChatActions(true);
});
window.addEventListener('resize', closeChatActions);
document.addEventListener('scroll', closeChatActions, true);

