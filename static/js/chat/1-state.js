// Room constants, our posting name & page-wide state.
// Part of chat.html's script, split by concern; files load in order &
// share globals (see templates/chat.html). Config comes from CHAT_CONFIG.

// Constants
const API_KEY = "dummy-api-key";
const TTS_API_URL = "https://speech.ai.unturf.com/v1/audio/speech";
const VOICES_API_URL = "https://speech.ai.unturf.com/v1/voices";
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
//   clear-on-login) no cache → we prompt fresh
const GUEST_USERNAME_KEY = 'guestUsername';
if (username) {
    localStorage.removeItem(GUEST_USERNAME_KEY);
} else {
    const cached = localStorage.getItem(GUEST_USERNAME_KEY);
    if (cached) {
        username = cached;
    } else {
        username = prompt("Enter your username:", "guest") || "guest";
        localStorage.setItem(GUEST_USERNAME_KEY, username);
    }
}

// What DOMPurify keeps in a message. No iframe & no style attribute: a
// sender could otherwise cover our page with a full-screen overlay or
// another site. Images & video (including data: images) still render.
const dompurify_config = {
  ADD_TAGS: ["img", "video"],
  FORBID_TAGS: ["form", "iframe", "style"],
  ALLOWED_ATTR: [
    "src", "width", "height", "alt", "class", "title", "controls",
  ]
};

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
let showThinking = localStorage.getItem('showThinking') !== 'false';

// Vision model state for auto alt-text
let visionAvailable = false;
let visionModel = null;
const imageDescriptionCache = new Map();  // Cache descriptions by image src hash
const imageBase64Cache = new Map();  // Cache fetched external images as base64

// CORS proxy for ethical external image fetching (respects robots.txt)
const CORS_PROXY_URL = 'https://cors-proxy.uncloseai.com/api/fetch';
