// Our Activities gallery (templates/activities.html): filter its cards by
// kind & by the words typed, & Play: make a new public chatroom, hand chat
// the `/activity <path>` command the way home hands over a first message
// (utils.js keepPendingMessage; chat's 3-room.js sends it once the room is
// joined, exactly what the chooser in a room's controls sends), then open it.

(function () {
    const grid = document.getElementById('activity-grid');
    const cards = [...grid.querySelectorAll('.activity-card')];
    const options = [...document.querySelectorAll('.activities-toolbar .segmented-option')];
    const text = document.getElementById('activities-filter');
    const empty = document.getElementById('activities-empty');
    const emptyTitle = document.getElementById('activities-empty-title');
    const errorEl = document.getElementById('activities-error');
    const kindWords = {game: 'games', lesson: 'lessons', tool: 'tools'};
    let kind = (options.find(o => o.getAttribute('aria-checked') === 'true') || options[0]).dataset.kind;

    function apply() {
        const typed = text.value.trim();
        const words = typed.toLowerCase();
        let shown = 0;
        for (const card of cards) {
            const ofKind = kind === 'all' || card.dataset.kind === kind;
            const about = [card.dataset.title, card.dataset.description, card.dataset.name].join(' ').toLowerCase();
            card.hidden = !(ofKind && (!words || about.includes(words)));
            if (!card.hidden) shown++;
        }
        empty.hidden = shown > 0;
        const what = kind === 'all' ? 'activities' : kindWords[kind];
        emptyTitle.textContent = words ? `No ${what} match “${typed}”` : `No ${what} here`;
    }

    function pick(option) {
        kind = option.dataset.kind;
        options.forEach(o => o.setAttribute('aria-checked', String(o === option)));
        // A link or a reload opens on the same kind.
        try {
            const url = new URL(location.href);
            if (kind === 'all') url.searchParams.delete('kind');
            else url.searchParams.set('kind', kind);
            history.replaceState(null, '', url);
        } catch (e) {}
        apply();
    }

    options.forEach(option => option.addEventListener('click', () => pick(option)));
    text.addEventListener('input', apply);
    document.getElementById('activities-clear').addEventListener('click', () => {
        text.value = '';
        pick(options[0]);
        text.focus();
    });

    async function play(card, button) {
        // A second tap while its room is being made must not make two.
        if (button.disabled) return;
        button.disabled = true;
        errorEl.textContent = '';
        try {
            const room = await createChatRoom(card.dataset.title, false);
            // Without this handoff the new room would open with nothing running.
            if (!keepPendingMessage(room.name, `/activity ${card.dataset.path}`, 'None')) {
                errorEl.textContent = 'This browser would not keep what to start (storage blocked or full), so its room was left empty & not opened. Allow site storage & try again.';
                button.disabled = false;
                return;
            }
            // Our lists then show the activity's title, not the room's
            // slug; a room that keeps its slug still plays.
            if (room.id) {
                await fetch(`/api/rooms/${room.id}/title`, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({title: card.dataset.title}),
                }).catch(() => {});
            }
            rememberRecentChat(room.name, card.dataset.title, {id: room.id, anyoneMayRetitle: room.anyone_may_retitle});
            // tests/functional/test_activities_ui.py records the address instead of leaving.
            const open = window.startChatNavigate || (url => { window.location.href = url; });
            open(`/chat/${encodeURIComponent(room.name)}`);
        } catch (error) {
            errorEl.textContent = error instanceof TypeError
                ? 'Network error: no room was started. Please try again.'
                : error.message;
            button.disabled = false;
        }
    }

    grid.addEventListener('click', event => {
        const button = event.target.closest('[data-play]');
        if (button) play(button.closest('.activity-card'), button);
    });

    // Back from a room (this page restored from cache): Play works again.
    window.addEventListener('pageshow', () => {
        grid.querySelectorAll('[data-play]').forEach(button => { button.disabled = false; });
    });

    apply();
})();
