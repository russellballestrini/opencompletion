// Shared by every page (layout.html & chat's base.html): theme & sign out.

function currentTheme() {
    return document.documentElement.getAttribute('data-theme') === 'dark' ? 'dark' : 'light';
}

// Label every theme control by the mode a click switches to, & tint the
// browser's own bar (installed app, phone) with our page background.
function syncThemeControls() {
    const theme = currentTheme();
    const themeColor = document.querySelector('meta[name="theme-color"]');
    const background = getComputedStyle(document.documentElement).getPropertyValue('--bg-primary').trim();
    if (themeColor && background) themeColor.setAttribute('content', background);
    document.querySelectorAll('[data-theme-toggle]').forEach(button => {
        const label = theme === 'dark' ? 'Light mode' : 'Dark mode';
        const icon = button.querySelector('.theme-icon');
        const text = button.querySelector('.theme-label');
        if (icon) icon.textContent = theme === 'dark' ? '☀' : '☾';
        if (text) text.textContent = label; else button.textContent = label;
        button.title = label;
    });
    document.querySelectorAll('[data-theme-choice]').forEach(button => {
        button.setAttribute('aria-pressed', String(button.dataset.themeChoice === theme));
    });
}

function setTheme(theme) {
    document.documentElement.setAttribute('data-theme', theme);
    try { localStorage.setItem('theme', theme); } catch (e) {}
    syncThemeControls();
    document.dispatchEvent(new CustomEvent('themechange', {detail: {theme}}));
}

function toggleTheme() {
    setTheme(currentTheme() === 'dark' ? 'light' : 'dark');
}

// Phones: app sidebars & panels open as drawers (chat & our home).
function openDrawer(which) {
    document.body.dataset.drawer = which;
}

function closeDrawer() {
    delete document.body.dataset.drawer;
}

document.addEventListener('keydown', event => {
    if (event.key === 'Escape') closeDrawer();
});

async function signOut() {
    try {
        const response = await fetch('/auth/logout', {method: 'POST'});
        if (!response.ok) throw new Error(response.statusText);
        forgetRecentChats();
        window.location.href = '/';
    } catch (error) {
        alert('Sign out failed. Please try again.');
    }
}

syncThemeControls();
