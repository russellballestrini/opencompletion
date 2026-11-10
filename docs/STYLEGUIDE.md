# OpenCompletion style guide

One look on every page & every screen. `static/css/style.css` implements
this guide, `/styleguide` renders every component live (flip dark mode in
our header to check both themes), and `make test-ui` enforces the rules
marked **(tested)** in real headless Chromium at 320, 375, 768 & 1280px.

## Pages & templates

| Template | What it is |
|---|---|
| `layout.html` | Our one shell for every page: `_head.html`, `utils.js`, `_app_sidebar.html` on the left, a page on the right (`.app-main` > `<main class="page">`, a ☰ `.page-topbar` on phones). Pages fill `title`, `content`, `scripts` & optional `page_class` (`page-narrow` for forms); set `active_nav` (`home`, `browse`, `activities`, `verify`, `profile`, `auth`) to mark the current page. |
| `_app_sidebar.html` | Every link we have: our name on top; New chat, Rooms (search lives there), Activities, Verify a transcript; Your rooms (the newest 5, then Show all) & Public rooms (scroll on their own; a room under Your rooms is never repeated under Public rooms); you at the bottom (`.sidebar-user`: settings & theme), Sign in for guests. Data comes from the `sidebar` context processor (`routes.pages.sidebar_chats`). |
| `home.html` | Our home (`/`), extends `layout.html` & replaces `main`: Solo / Chatroom switch (`.mode-btn`, `aria-pressed`; Solo is a room only its owner opens, `is_private` underneath) & one `.composer`, whose `.composer-bar` holds `_composer_tools.html` (attach & mic, `static/js/composer.js`) beside the model picker & Send; chat adds a Stop button (`.composer-stop`) while a reply streams. Behaviour in `static/js/home.js`. |
| `base.html` | Chat, extends `layout.html`: vendored chat libraries, `.chat-shell` adds room controls (`.utility-belt`) as a third column, sign-in dialog. `chat.html` reuses home's `.composer`. |
| `_head.html` | Meta, Open Graph, theme applied before first paint, our one stylesheet. |
| `_auth_steps.html` + `static/js/auth.js` | Email-code sign in, shared by `/auth` & chat's sign-in dialog. `data-after` on `#auth-flow` decides what success does. |
| `_room_card.html` | One room in a grid (browse & search). |
| `activities.html` + `_activity_card.html` + `static/js/activities.js` | Our Activities gallery (`/activities`): a `.room-grid` of activity cards, a kind filter, a text filter & Play. |
| `verify.html` + `static/js/verify.js` | Verify a transcript (`/verify`): a room's JSON download checked in the reader's browser, never uploaded. |

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

`hidden` always hides (`[hidden] { display: none !important }` in
style.css): show & hide with the attribute, never a display rule.

## Components

- **Buttons** `.btn` (primary), `.btn-secondary`, `.btn-danger`,
  `.btn-block`, `.icon-btn`. One primary action per view. Two-state
  controls are `.btn-toggle`: JavaScript sets `data-on` & words naming what
  a tap does ("Enable thinking" / "Disable thinking"); CSS colours the
  state green, JavaScript never sets colours.
- **Composer pickers** each select in a `.composer-bar` sits in a
  `.composer-pick` label: an icon saying which it is (voice, model), its
  name for screen readers, then the select, which shows a voice's name
  alone (its group names the speech model). At 480px or narrower our bar
  takes two rows: the pickers (the model gets what the voice leaves), then
  speak & attach on our left, Stop & Send on our right. **(tested)**
  While our model list loads its picker says "Loading models…", disabled
  (`modelsLoading` in `utils.js`), never "People only", & Send waits for
  the list: 4s at most, then it sends with the model picked before or
  says the list has not loaded. **(tested)**
- **Cards** `.card` group a task; `.page-title` + `.page-subtitle` open a page.
- **Forms** `.form-group` > `label` + `.form-input` + `.form-hint`. A label
  sits above every field; placeholders show format, never the label.
  `.choice-group` for radios. `.alert-success` / `.alert-error` report
  results; `.error-message` sits under a field.
- **Rooms** `.room-grid` of `.room-card`s, each in a `.room-card-wrap`:
  its title leads (`.room-card-name`), its name sits under it
  (`.room-card-slug`), `.badge-private` (Solo) or `.badge-public` &
  "active 2h ago" (`ago` filter). Our Rooms page filters one list with a
  `.segmented` radio group (Yours · Solo · Public, `?show=`; a guest's Yours lists & counts the public rooms their browser opened, as our sidebar's Your rooms does, & says so) & a text box that filters by title or name as you type & searches messages on Enter (`?q=`, `search_messages`; old `/search` links redirect there). A search shows two headed lists, no room in both: rooms whose messages mention the words (`.rooms-found`), then the other rooms named or titled like them (`#rooms-list-heading` + `#room-grid`), each saying so when empty.
- **Activities** a `.room-grid` of `.activity-card`s (a `.room-card` that
  is an `<article>`, not a link): title, its file's name
  (`.room-card-slug`), one line (`.activity-card-description`), then
  `.activity-card-foot` pinned to its bottom: a kind `.badge` (Game,
  Lesson, Tool) & one **Play** `.btn`. A `.segmented` group filters by kind
  (All · Games · Lessons · Tools, each with its count, `?kind=`; a kind
  with none is left out) & a text box by title, description or file name
  as you type; no match is an `.empty-state` that clears both. A file we
  could not read says so & has no Play.
- **Chat actions** a `.chat-actions-btn` (⋯, `data-chat-actions`) on each
  sidebar chat & room card opens `.chat-actions-menu` (`chat-actions.js`):
  what it offers comes from `_room_actions_data.html`'s data attributes.
  It shows on hover or focus, always on touch screens. A room that offers
  its viewer nothing carries no ⋯ (never an empty menu). A guest's own
  rows (`chatListItem` in `utils.js`, from this browser's history) offer
  Rename & Refresh title for a room anyone may retitle, by the id our
  server gave when the room was recorded. A new title reaches every row
  of its room, found by name (`showRoomTitle`).
- **Follow-ups** `.followups` of `.followup-chip`s under our newest reply,
  after its own actions: outlined pills in our text colour (their rules
  name `#chat` to outrank chat's quiet buttons), 40px tall on phones.
  **(tested)** `.scroll-bottom` (↓) floats above the composer once you
  scroll up.
- **Tool lines** `.message-tools` of `.message-tool`s inside a reply
  (`8-tools.js`), each a two-column grid: a status dot (`::before`:
  running pulses in our accent, done green, `.is-error` red) & the line.
  `.message-tool-head` says what & where in quiet text with
  `.message-tool-label` (what it was about) in full colour. A delegate is
  one card (`.is-card`, `drawDelegateCard`): what never changes on top
  (head: verb · where, `.tool-card-clock`; `.tool-card-title`;
  `.tool-card-task`, the task it was handed, a fold that reads as prose),
  then what each 15s poll updates, read top-down: `.tool-card-plan` (bar
  + step), `.tool-card-vitals`, `.tool-card-folds` (job & log pills; an
  opened panel takes the full width below them), & `.message-tool-summary`
  last, where chat's scroll & the eye land; its Play (`.tool-card-say`)
  reads the summary, & with Read replies aloud on each new live summary
  is read (an older unplayed one of the same card is dropped). Reorder
  by editing the areas only. Short facts are `.message-tool-chip`s
  (vitals; `.is-meter` fills to `--pct`,
  `.is-warn` for retries & empty replies) that wrap, never scroll.
  Prose (a delegate's task) & output wrap; code keeps its lines inside
  its box. Pages & files found are `.message-tool-sources`. A reply that
  used tools reads in order: its `.message-content` holds `.message-part`s
  (text) & `.message-tools` lists as they came, live & from history (each
  trail entry keeps `at`, its offset in the text).
- **Messages** `.message-wrapper` carries who it is from
  (`markSender` in `5-messages.js`): `.is-person` words sit in a bubble,
  `.is-mine` on the right in our accent's tint; `.is-model` replies are
  plain text across the pane, so who said what reads at a glance. A
  message being edited (`.message-edit`) opens its bubble to the whole
  column. One that was edited & resent carries `.message-versions`, a
  quiet `‹ 2/2 ›` pager; an earlier version opens under it as
  `.earlier-version`, a dashed frame saying which version it is & that it
  is read only (a note that stays in view as it scrolls), so it never
  reads as part of our chat; meanwhile the current words step back
  (`.is-viewing-earlier`). Its one action, **Continue from here**
  (`.earlier-version-actions`, outlined, at its end), starts a new chat
  from that version; its title says solo or a public chatroom. Message actions rest in `--text-secondary` at
  full opacity (never dimmed below 4.5:1). A table in a message is ruled,
  keeps its words whole & scrolls inside itself when wide, never the
  page. **(tested)** A single newline is a line break (marked's `breaks`):
  verse & addresses keep their lines. **(tested)**
- **Deleted line** `.deleted-line` (`deletedLine` in `5-messages.js`)
  takes a deleted message's place for everyone in the room: one quiet,
  dashed, centred line, "A message from ada was deleted by bob." & a
  `.deleted-line-restore` button, **Restore** (underlined, 40px tall on
  phones). It says who, never what: the words are off our page until
  someone restores them, & then the message returns where it was. A line
  nobody can act on is never shown (past its Restore window, or after an
  Edit & resend replaced that part of our chat). **(tested)**
- **Missing pages** `not_found.html` (`app.missing_page`): a wrong
  address, a name no room may have & someone's solo chat answer as an
  `.empty-state` in our shell, saying what is missing & offering New chat
  & Rooms. Our API keeps its short replies.
  Math is KaTeX's `.katex` (inline) & `.katex-display` (its own line,
  scrolling sideways when wider than the column), typeset from `$…$` &
  `$$…$$` by `renderMarkdown`.
- **Done chime** a soft two-note chime (Web Audio, no file) when the
  last reply streaming in a room ends after 15s or more, or while the tab
  is in the background; `#done-chime-btn` mutes it (localStorage
  `doneChime`, on by default).
- **Verify** (`/verify`, `verify.js`) a dashed `.verify-drop` card with
  one Choose button (a file dropped anywhere on its page counts); one
  `.verify-verdict` alert (`.alert-success` every message verified,
  `.alert-error` some failed or the file is junk, plain when it carries no
  proofs); `.verify-facts` (the file's room, chain & root); a
  `.verify-list` of `.verify-item`s, `.is-verified` / `.is-failed` /
  `.is-unchained`, each saying its state in a `.verify-badge` & which
  check failed in `.verify-item-why`, never by colour alone. Its copy
  says what is not proved (the root is only this file's own).
- **Who is here** room controls list Active Users & Inactive Users
  (`#user-lists`); a list & its heading are `hidden` while it is empty.
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
- A composer that grows (a long draft, attached files, a phone's
  keyboard) never covers our newest message: at the bottom (or within
  40px of it) chat stays there (`keepNewestInView` in `3-room.js`);
  scrolled up, nothing moves. **(tested)**
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
