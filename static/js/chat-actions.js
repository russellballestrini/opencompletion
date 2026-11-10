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

// A room archived or deleted leaves this page (& the open chat, for home).
function dropRoom(room) {
    if (location.pathname === `/chat/${encodeURIComponent(room.name)}`) {
        window.location.href = '/';
        return;
    }
    document.querySelectorAll(`[data-room-name="${CSS.escape(room.name)}"]`).forEach(row => row.remove());
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

