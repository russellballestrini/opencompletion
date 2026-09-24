// Shared by every page (layout.html & chat's base.html): theme & sign out.

function currentTheme() {
    return document.documentElement.getAttribute('data-theme') === 'dark' ? 'dark' : 'light';
}

// Label every theme control by the mode a click switches to.
function syncThemeControls() {
    const theme = currentTheme();
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

// Phones: our header's links fold behind a menu button.
function toggleSiteNav(button) {
    const open = button.getAttribute('aria-expanded') !== 'true';
    button.setAttribute('aria-expanded', String(open));
    button.closest('.site-header').classList.toggle('nav-open', open);
}

async function signOut() {
    try {
        const response = await fetch('/auth/logout', {method: 'POST'});
        if (!response.ok) throw new Error(response.statusText);
        window.location.href = '/';
    } catch (error) {
        alert('Sign out failed. Please try again.');
    }
}

syncThemeControls();
