// Room constants, our posting name & page-wide state.
// Part of chat.html's script, split by concern; files load in order &
// share globals (see templates/chat.html). Config comes from CHAT_CONFIG.

// Constants
// Speech goes through our server (routes/speech.py), which holds any keys.
const TTS_API_URL = "/speech/audio";
// Code execution API (proxied through backend to keep API key secure)
const room_name = CHAT_CONFIG.roomName;

// TTS Streaming: Detect best audio format for this browser
// Chrome/Edge: mp3 works, MediaSource supports audio/mpeg
// Firefox: mp3 often broken on Linux; use webm+opus
function detectAudioFormat() {
    const isFirefox = navigator.userAgent.includes('Firefox');
    if (isFirefox) {
        console.log("Firefox detected, using webm+opus format for TTS");
        return { format: 'webm', mime: 'audio/webm', mseMime: 'audio/webm;codecs=opus' };
    }
    // Chromium-based browsers: mp3 works, MSE supports audio/mpeg
    return { format: 'mp3', mime: 'audio/mpeg', mseMime: 'audio/mpeg' };
}
const AUDIO_FORMAT = detectAudioFormat();

// Get username from server (authenticated user's display name or None)
let username = CHAT_CONFIG.username;

// Guest usernames stick in localStorage across page loads so you don't have
// to retype them. The cache is invalidated on any login/logout transition:
// - logging in lands on a page with a server-provided username → we clear it
// - logging out lands on a page with no server username and (after the prior
//   clear-on-login) no cache → we ask fresh
// A guest with no name yet is asked in our page (askGuestName in 3-room.js)
// before we join: until then `username` is null & needsGuestName says so.
// Storage that throws (a private window) asks each visit & never breaks chat.
const GUEST_USERNAME_KEY = 'guestUsername';
let needsGuestName = false;
if (username) {
    try { localStorage.removeItem(GUEST_USERNAME_KEY); } catch (e) {}
} else {
    let cached = null;
    try { cached = localStorage.getItem(GUEST_USERNAME_KEY); } catch (e) {}
    if (cached) username = cached;
    else needsGuestName = true;
}

// What a guest typed, as the name we join under: trimmed, 50 characters at
// most, "guest" when empty. Our server has the real rules (resolve_username).
function guestNameFrom(typed) {
    return String(typed || '').trim().slice(0, 50).trim() || 'guest';
}

// What DOMPurify keeps in a message. No iframe & no style attribute: a
// sender could otherwise cover our page with a full-screen overlay or
// another site. No class either, for the same reason: with it a message
// could wear any look in style.css (a `.modal` over our whole page, styled
// as our own sign-in). Only a code block's `language-…` class stays, which
// marked writes & highlight.js reads. No data-* or aria-*: our delegated
// click handlers trust data-room-name & friends. Images & video (including
// data: images) still render.
const dompurify_config = {
  ADD_TAGS: ["img", "video"],
  FORBID_TAGS: ["form", "iframe", "style"],
  ALLOW_DATA_ATTR: false,
  ALLOW_ARIA_ATTR: false,
  // href: links work (attachments, your name -> /profile); DOMPurify still
  // drops javascript: & other script URLs.
  ALLOWED_ATTR: [
    "href", "src", "width", "height", "alt", "title", "controls",
  ]
};
if (typeof DOMPurify !== "undefined") {
  DOMPurify.addHook("uponSanitizeAttribute", (node, data) => {
    if (data.attrName === "class" && node.nodeName === "CODE"
        && /^language-[\w+#.-]+$/.test(data.attrValue)) {
      data.forceKeepAttr = true;
    }
  });
}

// keeping track of scrolling to prevent autoscrolling.
let userHasScrolledUp = false;
let currentAudio = null; // To keep track of the currently playing audio
let currentQueuedAudio = null; // To keep track of currently playing queued TTS audio
let currentQueuedMessageId = null; // messageId of the queued audio currently playing (for cleanup on delete)
let audioCache = {}; // Cache to store audio blobs

// Auto-play TTS state
let autoPlayTTS = localStorage.getItem('autoPlayTTS') === 'true' || false;
let ttsQueue = [];
let isPlayingTTS = false;

// Thinking-mode state. Default ON. When OFF we send enable_thinking=false
// with each chat_message so the server asks the model (Qwen3-style) to skip
// chain-of-thought entirely — saving tokens, not just hiding output. As a
// fallback, any reasoning_content that still arrives is dropped client-side
// (see message_chunk handler). Toggle persists to localStorage.
let enableThinking = localStorage.getItem('enableThinking') !== 'false';
// A soft chime when replies finish after a wait (on by default).
let doneChime = localStorage.getItem('doneChime') !== 'false';

// Vision model state for auto alt-text
let visionAvailable = false;
let visionModel = null;
const imageDescriptionCache = new Map();  // Cache descriptions by image src hash
const imageBase64Cache = new Map();  // Cache fetched external images as base64

// CORS proxy for ethical external image fetching (respects robots.txt)
const CORS_PROXY_URL = 'https://cors-proxy.uncloseai.com/api/fetch';
