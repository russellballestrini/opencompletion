# OpenCompletion style guide

One look on every page & every screen. `static/css/style.css` implements
this guide, `/styleguide` renders every component live (flip dark mode in
our header to check both themes), and `make test-ui` enforces the rules
marked **(tested)** in real headless Chromium at 320, 375, 768 & 1280px.

## Pages & templates

| Template | What it is |
|---|---|
| `layout.html` | Our one shell for every page: `_head.html`, `utils.js`, `_app_sidebar.html` on the left, a page on the right (`.app-main` > `<main class="page">`, a ☰ `.page-topbar` on phones). Pages fill `title`, `content`, `scripts` & optional `page_class` (`page-narrow` for forms); set `active_nav` (`home`, `search`, `browse`, `profile`, `auth`) to mark the current page. |
| `_app_sidebar.html` | Every link we have: our name on top; New chat, Search, Rooms; Your chats & Public chats (scroll on their own); you at the bottom (`.sidebar-user`: settings & theme), Sign in for guests. Data comes from the `sidebar` context processor (`routes.pages.sidebar_chats`). |
| `home.html` | Our home (`/`), extends `layout.html` & replaces `main`: Solo / Chatroom switch (`.mode-btn`, `aria-pressed`; Solo is a room only its owner opens, `is_private` underneath) & one `.composer`, whose `.composer-bar` holds `_composer_tools.html` (attach & mic, `static/js/composer.js`) beside the model picker & Send; chat adds a Stop button (`.composer-stop`) while a reply streams. Behaviour in `static/js/home.js`. |
| `base.html` | Chat, extends `layout.html`: vendored chat libraries, `.chat-shell` adds room controls (`.utility-belt`) as a third column, sign-in dialog. `chat.html` reuses home's `.composer`. |
| `_head.html` | Meta, Open Graph, theme applied before first paint, our one stylesheet. |
| `_auth_steps.html` + `static/js/auth.js` | Email-code sign in, shared by `/auth` & chat's sign-in dialog. `data-after` on `#auth-flow` decides what success does. |
| `_room_card.html` | One room in a grid (browse & search). |

Rules:

- Every page extends `layout.html` (chat via `base.html`) & links `style.css` once. **(tested)**
- Templates hold no `<style>` blocks & no raw colours in `style=""`. **(tested)**
- Shared behaviour lives in `static/js/`: `site.js` (theme, sign out), `auth.js` (sign in), `utils.js` (`slugify`), `chat/*.js` (chat, in load order). Static scripts hold no template syntax & parse under `node --check`. **(tested)**
- Third-party browser libraries are vendored in `static/vendor/`, never loaded from a CDN.

## Tokens

All colour comes from custom properties on `:root`, redefined under
`[data-theme="dark"]`. Every `var(--x)` must be defined. **(tested)**

| Role | Tokens |
|---|---|
| Surfaces | `--bg-primary` (page), `--bg-secondary` (cards, panels), `--bg-tertiary` (inputs, secondary buttons), `--bg-hover` |
| Text | `--text-primary`, `--text-secondary` (hints, meta), `--text-success`, `--text-error`, `--text-warning` |
| Action | `--button-primary` (the one accent), `--button-success`, `--button-danger` |
| Lines | `--border-color`, `--focus-ring` |
| Space | `--space-1` … `--space-12` on a 4px base |
| Radius | `--radius-sm` 4, `--radius-md` 8 (controls), `--radius-lg` 12 (cards) |

Type is the system UI stack at 14px / 1.5; headings 32/24/18. Form fields
are 16px on phones so iOS never zooms into them.

## Components

- **Buttons** `.btn` (primary), `.btn-secondary`, `.btn-danger`,
  `.btn-block`, `.icon-btn`. One primary action per view. Two-state
  controls are `.btn-toggle`: JavaScript sets `data-on` & words naming what
  a tap does ("Enable thinking" / "Disable thinking"); CSS colours the
  state green, JavaScript never sets colours.
- **Cards** `.card` group a task; `.page-title` + `.page-subtitle` open a page.
- **Forms** `.form-group` > `label` + `.form-input` + `.form-hint`. A label
  sits above every field; placeholders show format, never the label.
  `.choice-group` for radios. `.alert-success` / `.alert-error` report
  results; `.error-message` sits under a field.
- **Rooms** `.room-grid` of `.room-card`s, each in a `.room-card-wrap`;
  `.badge-private` marks private rooms.
- **Chat actions** a `.chat-actions-btn` (⋯, `data-chat-actions`) on each
  sidebar chat & room card opens `.chat-actions-menu` (`chat-actions.js`):
  what it offers comes from `_room_actions_data.html`'s data attributes.
  It shows on hover or focus, always on touch screens.
- **Follow-ups** `.followups` of `.followup-chip`s under our newest reply;
  `.scroll-bottom` (↓) floats above the composer once you scroll up.
- **Tool lines** `.message-tools` of `.message-tool`s inside a reply
  (`8-tools.js`): what a tool did & whose network it ran on ("Searched
  the web from your browser: …"), its pages in `.message-tool-sources`; a
  code tool's `.message-tool-detail` folds open to its code & output.
- **Tabs** `.tabs` > `.room-tab` with `role="tab"` & `aria-selected`.
- **Empty states** `.empty-state` say what is missing & offer the next step.

## Every screen

- No horizontal scrolling at 320px or wider. Long names wrap
  (`overflow-wrap: anywhere`). **(tested)**
- Tap targets are at least 40px tall on phones, 44px for buttons & fields. **(tested)**
- Our sidebar is a drawer on phones on every page, opened by ☰ & closed
  by ×, the backdrop or Escape (`openDrawer`/`closeDrawer` in `site.js`).
  You stay pinned at its bottom however many chats it lists. **(tested)**
- Buttons inside a conversation (`#chat button`: message actions, code
  Copy & Run) share one quiet style; actions brighten on hover or focus.
- Breakpoints: 1024px narrows chat's sidebars; 768px is phone. On a phone,
  chat's rooms sidebar & room controls become drawers opened from its top
  bar (☰ & ⚙): our real panels, never copies, closed by ×, the backdrop or
  Escape. **(tested)**
- Only chat locks page scrolling (`body.chat-page`); every other page
  scrolls. **(tested)**
- Hover is never the only way in: anything on hover also works on tap or
  focus.
- Focus is always visible (3px `--focus-ring`); motion stops under
  `prefers-reduced-motion`.

## Honest copy

- Say "machine learning", never "AI". **(tested)**
- Say what really happened: sign in says whether the code was emailed or
  only written to the server log; `/help` lists only commands that exist.
  **(tested)**
- Solo means only you: search, sidebars, live room updates & its files
  never show a solo chat to anyone but its owner. **(tested)**
- We call an only-you chat **Solo**, never "Private": elsewhere "Private"
  means an unsaved, temporary chat, & ours are kept.
- Hints state the real rule ("3-50 letters, numbers, _ or -"), confirms
  say what happens ("Archive: leaves every list & search, nothing is
  deleted"), errors say what failed & what to do next.
- Prefer "our" for shared things & "a" for one of many; plain words, no
  hype, emoji only where they carry meaning.
