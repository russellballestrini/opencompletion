// Text to speech: streaming playback, sentence glow, auto-play queue & toggles.
// Part of chat.html's script, split by concern; files load in order &
// share globals (see templates/chat.html). Config comes from CHAT_CONFIG.

// Streaming TTS function - plays audio as chunks arrive using MediaSource API
// Returns a promise that resolves with { audio, blob, blobUrl, streamed }
async function fetchTTSStreaming(cleanText, model, voice) {
    const mseMime = AUDIO_FORMAT.mseMime;
    const canStream = mseMime && window.MediaSource && MediaSource.isTypeSupported(mseMime);
    const requestFormat = AUDIO_FORMAT.format;

    console.log(`TTS streaming: canStream=${canStream}, format=${requestFormat}, mseMime=${mseMime}`);

    const response = await fetch(TTS_API_URL, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            'Authorization': `Bearer ${API_KEY}`
        },
        body: JSON.stringify({
            model: model,
            voice: voice,
            input: cleanText,
            response_format: requestFormat
        })
    });

    if (!response.ok) {
        throw new Error(`HTTP error! status: ${response.status}`);
    }

    // If MSE isn't available, fall back to full buffered download
    if (!canStream) {
        console.warn(`No streaming support for ${requestFormat}, using full buffer`);
        const audioBlob = await response.blob();
        const audioUrl = URL.createObjectURL(audioBlob);
        const audio = new Audio(audioUrl);
        audio.playbackRate = 0.9;
        return { audio, blob: audioBlob, blobUrl: audioUrl, streamed: false };
    }

    // Use MediaSource API for true streaming
    const mediaSource = new MediaSource();
    const audioUrl = URL.createObjectURL(mediaSource);
    const audio = new Audio(audioUrl);
    audio.playbackRate = 0.9;
    console.log(`TTS streaming: MediaSource created, readyState=${mediaSource.readyState}`);

    // Collect all chunks for download later
    const chunks = [];

    // Create a promise that resolves when streaming is complete
    const streamingComplete = new Promise((resolve, reject) => {
        mediaSource.addEventListener('sourceopen', async () => {
            console.log("TTS streaming: sourceopen fired");
            let sourceBuffer;
            try {
                sourceBuffer = mediaSource.addSourceBuffer(mseMime);
                sourceBuffer.mode = 'sequence';
            } catch (e) {
                console.error("Failed to create SourceBuffer:", e);
                reject(e);
                return;
            }

            const reader = response.body.getReader();
            let totalBytes = 0;

            // Queue for appending buffers (SourceBuffer can only append one at a time)
            const appendQueue = [];
            let appending = false;

            function processQueue() {
                if (appending || appendQueue.length === 0) return;
                appending = true;
                const chunk = appendQueue.shift();
                try {
                    sourceBuffer.appendBuffer(chunk);
                } catch (e) {
                    console.error("appendBuffer error:", e);
                    appending = false;
                }
            }

            sourceBuffer.addEventListener('updateend', () => {
                appending = false;
                processQueue();
            });

            try {
                while (true) {
                    const { done: readerDone, value } = await reader.read();
                    if (readerDone) break;

                    chunks.push(value);
                    totalBytes += value.byteLength;

                    // Queue the chunk for appending (use slice to avoid shared ArrayBuffer issues)
                    appendQueue.push(value.slice().buffer);
                    processQueue();

                    // Auto-play once we have some data (~1KB)
                    if (totalBytes > 1024 && audio.paused) {
                        console.log("TTS streaming: starting playback at", totalBytes, "bytes");
                        audio.play().catch(e => console.warn("Auto-play blocked:", e.message));
                    }
                }

                // Wait for all queued appends to finish
                await new Promise((res) => {
                    const check = () => {
                        if (!appending && appendQueue.length === 0) {
                            res();
                        } else {
                            setTimeout(check, 50);
                        }
                    };
                    check();
                });

                if (mediaSource.readyState === 'open') {
                    mediaSource.endOfStream();
                }

                console.log(`TTS streaming complete: ${totalBytes} bytes`);

                // Build blob for download/caching
                const audioBlob = new Blob(chunks, { type: mseMime });
                resolve({ blob: audioBlob });

            } catch (error) {
                console.error("Streaming read error:", error);
                if (mediaSource.readyState === 'open') {
                    mediaSource.endOfStream('network');
                }
                reject(error);
            }
        });

        mediaSource.addEventListener('error', (e) => {
            console.error("MediaSource error:", e);
            reject(new Error("MediaSource error"));
        });
    });

    // Return immediately with audio element - streaming happens in background
    return {
        audio,
        blobUrl: audioUrl,
        streamed: true,
        streamingComplete // Promise that resolves with { blob } when done
    };
}

// ---- Per-sentence glow synced to TTS audio via exact server timestamps ----
// The speech service (tts-1-f5) returns each sentence's start/end in ms — it
// synthesizes one audio chunk per sentence, so the timing is exact, not guessed.
// We wrap the rendered message into sentence spans and light the active one by
// comparing audio.currentTime to those boundaries. No Web Audio / RMS heuristics.

function wrapSentencesForGlow(container) {
    if (!container) return;
    // Skip only if the previous wrap survives. innerHTML overwrites (streaming
    // chunks, message_updated, edit/save) wipe the spans but the dataset flag
    // lives on the container itself — without this spans-present check, manual
    // replay after a re-render finds the flag set, skips wrapping, and the
    // glow no-ops because querySelectorAll('.tts-sentence') comes back empty.
    if (container.dataset.glowWrapped === '1' && container.querySelector('.tts-sentence')) return;
    // chat_message (non-streaming) inlines the sender header into message-content
    // as "<p><strong>[name](link):</strong></p>" — message_chunk uses a separate
    // .message-header div. If we wrap the header as sentence 0, manual Play
    // highlights the sender name instead of the actual first sentence. Detect
    // the pattern (top-level <p> with one <strong> child, text ending in ":")
    // and exclude it from the walk.
    let headerParagraph = null;
    const first = container.firstElementChild;
    if (first && first.tagName === 'P' &&
        first.children.length === 1 &&
        first.firstElementChild &&
        first.firstElementChild.tagName === 'STRONG' &&
        first.textContent.trim().endsWith(':')) {
        headerParagraph = first;
    }
    const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT, {
        acceptNode(node) {
            if (headerParagraph && headerParagraph.contains(node)) return NodeFilter.FILTER_REJECT;
            return NodeFilter.FILTER_ACCEPT;
        }
    });
    const nodes = [];
    let t;
    while ((t = walker.nextNode())) { if (t.nodeValue && t.nodeValue.trim()) nodes.push(t); }
    if (!nodes.length) return;
    const full = nodes.map(function (x) { return x.nodeValue; }).join('');
    const parts = full.match(/[^.!?]+[.!?]*\s*/g) || [full];
    const ranges = [];
    let start = 0;
    for (let i = 0; i < parts.length; i++) {
        ranges.push([start, start + parts[i].length]);
        start += parts[i].length;
    }
    function sentAt(pos) {
        for (let s = 0; s < ranges.length; s++) { if (pos < ranges[s][1]) return s; }
        return ranges.length - 1;
    }
    let off = 0;
    nodes.forEach(function (node) {
        const text = node.nodeValue;
        const frag = document.createDocumentFragment();
        let i = 0;
        while (i < text.length) {
            const si = sentAt(off + i);
            let j = i + 1;
            while (j < text.length && sentAt(off + j) === si) j++;
            const span = document.createElement('span');
            span.className = 'tts-sentence';
            span.dataset.si = si;
            span.textContent = text.slice(i, j);
            frag.appendChild(span);
            i = j;
        }
        off += text.length;
        node.parentNode.replaceChild(frag, node);
    });
    container.dataset.glowWrapped = '1';
    container._glowSentenceCount = parts.length;
}

// Light the spoken sentence using exact server timing. `sentences` is the
// array from the speech service: [{index, text, start_ms, end_ms}, ...].
// Without it (non-F5 models), we no-op rather than guess.
function attachSentenceGlow(audio, playButton, sentences) {
    if (!audio || !playButton) return;
    // Allow an initially-empty array: under SSE it grows as sentences arrive,
    // and the tick reads its length live. Undefined (non-F5) still no-ops.
    if (!Array.isArray(sentences)) return;
    const wrapper = playButton.closest('.message-wrapper');
    const container = wrapper && wrapper.querySelector('.message-content');
    if (!container) return;
    wrapSentencesForGlow(container);
    const spans = container.querySelectorAll('.tts-sentence');
    const domCount = container._glowSentenceCount || 0;
    if (!spans.length || domCount <= 0) return;

    let active = -1;
    function setActive(idx) {
        if (idx === active) return;
        active = idx;
        spans.forEach(function (s) {
            s.classList.toggle('tts-reading', parseInt(s.dataset.si, 10) === idx);
        });
    }
    function clear() { spans.forEach(function (s) { s.classList.remove('tts-reading'); }); }

    // The rendered message and the synthesized text usually split into the same
    // sentence count; when they don't, map server index onto DOM spans by ratio.
    function domIndexFor(serverIdx) {
        const sc = sentences.length || 1;
        if (domCount === sc) return serverIdx;
        return Math.min(domCount - 1, Math.floor(serverIdx * domCount / sc));
    }

    function tick() {
        if (audio.paused || audio.ended) return;
        const ms = audio.currentTime * 1000;
        let si = 0;
        for (let i = 0; i < sentences.length; i++) {
            if (ms >= sentences[i].start_ms) si = i; else break;
        }
        setActive(domIndexFor(si));
        requestAnimationFrame(tick);
    }
    setActive(0);
    audio.addEventListener('play', function () { requestAnimationFrame(tick); });
    audio.addEventListener('ended', clear);
    audio.addEventListener('pause', function () { if (audio.ended) clear(); });
    if (!audio.paused) requestAnimationFrame(tick);
}

// Decode a base64 string to a Uint8Array.
function b64ToBytes(b64) {
    const bin = atob(b64);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    return bytes;
}

// Parse a fetch byte stream as Server-Sent Events, yielding {event, data}.
async function* sseEvents(reader) {
    const decoder = new TextDecoder();
    let buf = "";
    while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        let nl;
        while ((nl = buf.indexOf("\n\n")) >= 0) {
            const block = buf.slice(0, nl);
            buf = buf.slice(nl + 2);
            let ev = "message", data = "";
            block.split("\n").forEach((line) => {
                if (line.startsWith("event:")) ev = line.slice(6).trim();
                else if (line.startsWith("data:")) data += line.slice(5).trim();
            });
            yield { event: ev, data: data };
        }
    }
}

// Stream TTS over SSE (tts-1-f5 only). Each event carries one sentence's mp3
// plus exact timing. Audio feeds an MSE SourceBuffer (sequence mode) so playback
// starts after sentence 0; `sentences` grows live to drive the glow. Returns
// { audio, sentences, blobUrl, streamingComplete } — streamingComplete resolves
// with the full { blob } once every sentence has arrived.
async function fetchTTSStreamingSSE(cleanText, model, voice) {
    const mseMime = "audio/mpeg";
    const canStream = window.MediaSource && MediaSource.isTypeSupported(mseMime);
    const response = await fetch(TTS_API_URL, {
        method: "POST",
        headers: { "Content-Type": "application/json", "Authorization": `Bearer ${API_KEY}` },
        body: JSON.stringify({ model: model, voice: voice, input: cleanText, sse: true })
    });
    if (!response.ok) throw new Error(`TTS SSE request failed: ${response.status}`);

    const sentences = []; // grows as events arrive; shared with the glow
    const chunks = [];     // mp3 bytes per sentence, for the final blob
    const reader = response.body.getReader();

    // No MSE for mp3 (e.g. Firefox/Safari): collect all, then play one blob.
    if (!canStream) {
        for await (const e of sseEvents(reader)) {
            if (e.event !== "sentence") continue;
            const o = JSON.parse(e.data);
            sentences.push({ index: o.index, text: o.text, start_ms: o.start_ms, end_ms: o.end_ms });
            chunks.push(b64ToBytes(o.audio_b64));
        }
        const blob = new Blob(chunks, { type: mseMime });
        const blobUrl = URL.createObjectURL(blob);
        const audio = new Audio(blobUrl);
        audio.playbackRate = 0.9;
        return { audio, sentences, blobUrl, streamingComplete: Promise.resolve({ blob }) };
    }

    const mediaSource = new MediaSource();
    const blobUrl = URL.createObjectURL(mediaSource);
    const audio = new Audio(blobUrl);
    audio.playbackRate = 0.9;

    const streamingComplete = new Promise((resolve, reject) => {
        mediaSource.addEventListener("sourceopen", async () => {
            let sourceBuffer;
            try {
                sourceBuffer = mediaSource.addSourceBuffer(mseMime);
                sourceBuffer.mode = "sequence";
            } catch (e) { reject(e); return; }
            const appendQueue = [];
            let appending = false;
            function processQueue() {
                if (appending || appendQueue.length === 0) return;
                appending = true;
                try { sourceBuffer.appendBuffer(appendQueue.shift()); }
                catch (e) { appending = false; }
            }
            sourceBuffer.addEventListener("updateend", () => { appending = false; processQueue(); });
            try {
                for await (const e of sseEvents(reader)) {
                    if (e.event === "error") throw new Error("TTS SSE error");
                    if (e.event !== "sentence") continue;
                    const o = JSON.parse(e.data);
                    sentences.push({ index: o.index, text: o.text, start_ms: o.start_ms, end_ms: o.end_ms });
                    const bytes = b64ToBytes(o.audio_b64);
                    chunks.push(bytes);
                    appendQueue.push(bytes.slice().buffer);
                    processQueue();
                    if (audio.paused) audio.play().catch(() => {});
                }
                await new Promise((res) => {
                    const check = () => (!appending && appendQueue.length === 0) ? res() : setTimeout(check, 50);
                    check();
                });
                if (mediaSource.readyState === "open") mediaSource.endOfStream();
                resolve({ blob: new Blob(chunks, { type: mseMime }) });
            } catch (error) {
                if (mediaSource.readyState === "open") { try { mediaSource.endOfStream("network"); } catch (e) { /* ignore */ } }
                reject(error);
            }
        });
        mediaSource.addEventListener("error", () => reject(new Error("MediaSource error")));
    });

    return { audio, sentences, blobUrl, streamed: true, streamingComplete };
}

// Function to read text using TTS (for manual button clicks) - now with streaming
async function speakText(text, playButton, messageId) {
    const voiceSelectValue = document.getElementById("voice-select").value;

    // Parse model and voice from the dropdown value (format: "model:voice")
    const [model, voice] = voiceSelectValue.includes(':') ? voiceSelectValue.split(':') : ['tts-1', voiceSelectValue];
    const cacheKey = `${messageId}-${voiceSelectValue}`; // Unique cache key for each message and voice

    // Clean the text to include only alphanumeric characters, spaces, and key punctuation
    const cleanText = text.replace(/[^a-zA-Z0-9\s.,!?]/g, '');

    // Wire button + glow to THIS audio while it owns playback. While live, clicks
    // toggle pause/resume on this audio (via toggleAudioPlayback for cross-message
    // safety). When audio ends or errors, the original speakText handler is
    // restored so users can replay from cache.
    function takeOver(audio, sentences) {
        const originalClick = playButton.onclick;
        playButton.onclick = () => toggleAudioPlayback(audio, playButton);
        audio.addEventListener('ended', () => {
            playButton.textContent = "Play";
            playButton.onclick = originalClick;
        });
        audio.addEventListener('error', () => {
            playButton.textContent = "Play";
            playButton.onclick = originalClick;
        });
        // attachSentenceGlow no-ops when sentences is undefined (non-F5 models).
        attachSentenceGlow(audio, playButton, sentences);
    }

    try {
        // Check if the audio is already cached
        if (audioCache[cacheKey]) {
            const cachedData = audioCache[cacheKey];
            // Create new audio from cached blob for replay
            const audioUrl = URL.createObjectURL(cachedData.blob);
            const audio = new Audio(audioUrl);
            audio.playbackRate = 0.9;
            enableDownloadButton(messageId, playButton, audioUrl, voice);
            takeOver(audio, cachedData.sentences);
            toggleAudioPlayback(audio, playButton);
            return;
        }

        // Set button to streaming state
        playButton.textContent = "Loading";
        playButton.classList.add("tts-loading");
        playButton.disabled = true;

        // F5: SSE gives per-sentence timing so the glow lights live during read.
        if (model === 'tts-1-f5') {
            const result = await fetchTTSStreamingSSE(cleanText, model, voice);
            const audio = result.audio;
            playButton.classList.remove("tts-loading");
            playButton.disabled = false;
            takeOver(audio, result.sentences);
            toggleAudioPlayback(audio, playButton);
            result.streamingComplete.then(({ blob }) => {
                audioCache[cacheKey] = { blob, sentences: result.sentences };
                enableDownloadButton(messageId, playButton, URL.createObjectURL(blob), voice);
            }).catch(e => console.error("TTS SSE completion error:", e));
            return;
        }

        // Other models: streaming playback, no glow (no per-sentence timing).
        const result = await fetchTTSStreaming(cleanText, model, voice);
        const audio = result.audio;
        playButton.classList.remove("tts-loading");
        playButton.disabled = false;
        takeOver(audio, undefined);
        toggleAudioPlayback(audio, playButton);

        // Handle streaming completion for caching and download button
        if (result.streamed && result.streamingComplete) {
            result.streamingComplete.then(({ blob }) => {
                audioCache[cacheKey] = { blob };
                const downloadUrl = URL.createObjectURL(blob);
                enableDownloadButton(messageId, playButton, downloadUrl, voice);
            }).catch(e => console.error("Streaming completion error:", e));
        } else {
            audioCache[cacheKey] = { blob: result.blob };
            enableDownloadButton(messageId, playButton, result.blobUrl, voice);
        }
    } catch (error) {
        console.error('Error in TTS:', error);
        playButton.classList.remove("tts-loading");
        playButton.textContent = "Play"; // Reset button text on error
        playButton.disabled = false;
    }
}

// Function to read text using TTS (for queued auto-play) - now with streaming
async function speakTextQueued(text, playButton, messageId) {
    return new Promise(async (resolve, reject) => {
        const voiceSelectValue = document.getElementById("voice-select").value;

        // Parse model and voice from the dropdown value (format: "model:voice")
        const [model, voice] = voiceSelectValue.includes(':') ? voiceSelectValue.split(':') : ['tts-1', voiceSelectValue];
        const cacheKey = `${messageId}-${voiceSelectValue}`;
        const cleanText = text.replace(/[^a-zA-Z0-9\s.,!?]/g, '');

        function bindLifecycle(audio) {
            currentQueuedAudio = audio;
            // Save the message-level click handler so we can restore it
            // once this queued audio ends — it's how replay-from-cache works.
            const originalClick = playButton.onclick;
            // Route clicks through toggleAudioPlayback so we still pause any
            // other audio that owns currentAudio (cross-message safety).
            const toggleThisAudio = () => toggleAudioPlayback(audio, playButton);
            audio.onplay = () => {
                playButton.classList.remove("tts-queued", "tts-loading");
                playButton.disabled = false;
                playButton.textContent = "Pause";
                currentAudio = audio;
                currentAudio.playButton = playButton;
                // Click should pause/resume THIS audio while it owns playback.
                // Without this, the original onclick re-runs speakText and
                // restarts from the cached blob — sounds like a double-play.
                playButton.onclick = toggleThisAudio;
            };
            audio.onpause = () => {
                if (!audio.ended) playButton.textContent = "Play";
            };
            audio.onended = () => {
                console.log("TTS finished for:", messageId);
                playButton.classList.remove("tts-queued", "tts-loading");
                playButton.disabled = false;
                playButton.textContent = "Play";
                playButton.onclick = originalClick;
                currentQueuedAudio = null;
                resolve();
            };
            audio.onerror = () => {
                console.error("TTS audio error for:", messageId);
                playButton.classList.remove("tts-queued", "tts-loading");
                playButton.disabled = false;
                playButton.textContent = "Play";
                playButton.onclick = originalClick;
                currentQueuedAudio = null;
                reject(new Error("Audio playback failed"));
            };
        }

        // Check if audio is cached
        if (audioCache[cacheKey]) {
            const cachedData = audioCache[cacheKey];
            // Create new audio from cached blob for replay
            const audioUrl = URL.createObjectURL(cachedData.blob);
            const audio = new Audio(audioUrl);
            audio.playbackRate = 0.9;
            enableDownloadButton(messageId, playButton, audioUrl, voice);
            attachSentenceGlow(audio, playButton, cachedData.sentences);
            bindLifecycle(audio);
            audio.play().catch(reject);
            return;
        }

        try {
            // F5 streams over SSE: audio starts after sentence 0 (gapless via MSE)
            // and the glow tracks exact per-sentence timing as events arrive.
            if (model === 'tts-1-f5') {
                const result = await fetchTTSStreamingSSE(cleanText, model, voice);
                const audio = result.audio;
                attachSentenceGlow(audio, playButton, result.sentences);
                bindLifecycle(audio);
                // Cache the full clip + final sentence timing once streaming finishes.
                result.streamingComplete.then(({ blob }) => {
                    audioCache[cacheKey] = { blob, sentences: result.sentences };
                    enableDownloadButton(messageId, playButton, URL.createObjectURL(blob), voice);
                }).catch(e => console.error("TTS SSE completion error:", e));
                if (audio.paused) audio.play().catch(reject);
                return;
            }

            // Other models: streaming playback, no glow (no server timing).
            const result = await fetchTTSStreaming(cleanText, model, voice);
            const audio = result.audio;
            bindLifecycle(audio);

            // Handle streaming completion for caching
            if (result.streamed && result.streamingComplete) {
                result.streamingComplete.then(({ blob }) => {
                    // Cache the blob for replay
                    audioCache[cacheKey] = { blob };
                    // Create downloadable URL from blob
                    const downloadUrl = URL.createObjectURL(blob);
                    enableDownloadButton(messageId, playButton, downloadUrl, voice);
                }).catch(e => console.error("Streaming completion error:", e));
            } else {
                // Non-streamed fallback - cache immediately
                audioCache[cacheKey] = { blob: result.blob };
                enableDownloadButton(messageId, playButton, result.blobUrl, voice);
            }

            // Audio should auto-play from streaming, but ensure it starts
            if (audio.paused) {
                audio.play().catch(reject);
            }
        } catch (error) {
            playButton.classList.remove("tts-queued", "tts-loading");
            playButton.disabled = false;
            playButton.textContent = "Play";
            reject(error);
        }
    });
}

// Function to add TTS to queue
function queueTTS(text, playButton, messageId) {
    ttsQueue.push({ text, playButton, messageId });
    console.log("Added to TTS queue:", messageId, "Queue length:", ttsQueue.length);
    // Immediately show "Queued" so users see TTS is incoming even before fetch starts.
    playButton.classList.remove("tts-loading");
    playButton.classList.add("tts-queued");
    playButton.disabled = true;
    playButton.textContent = "Queued";
    processNextTTS();
}

// Function to process the next TTS in queue
function processNextTTS() {
    if (isPlayingTTS || ttsQueue.length === 0) {
        return;
    }
    
    isPlayingTTS = true;
    const { text, playButton, messageId } = ttsQueue.shift();
    currentQueuedMessageId = messageId;
    console.log("Processing TTS from queue:", messageId);

    // Swap "Queued" indicator for active "Loading" spinner while we fetch audio.
    playButton.classList.remove("tts-queued");
    playButton.classList.add("tts-loading");
    playButton.disabled = true;
    playButton.textContent = "Loading";

    // Use non-blocking async processing
    speakTextQueued(text, playButton, messageId)
        .then(() => {
            console.log("TTS completed successfully for:", messageId);
        })
        .catch((error) => {
            console.error("TTS error:", error);
        })
        .finally(() => {
            isPlayingTTS = false;
            currentQueuedMessageId = null;
            // Schedule next item with minimal delay to prevent blocking
            setTimeout(processNextTTS, 10);
        });
}

// Toggle buttons carry their state in aria-pressed; CSS colours it.
function updateShowThinkingDisplay() {
    document.getElementById("show-thinking-btn").setAttribute("aria-pressed", String(showThinking));
}

// Toggle thinking mode. On by default; persisted in localStorage.
function toggleShowThinking() {
    showThinking = !showThinking;
    localStorage.setItem('showThinking', showThinking.toString());
    updateShowThinkingDisplay();
}

function updateAutoPlayTTSDisplay() {
    document.getElementById("auto-play-tts-btn").setAttribute("aria-pressed", String(autoPlayTTS));
    if (!autoPlayTTS) {
        // Clear queue when turning off
        ttsQueue = [];
        isPlayingTTS = false;
    }
}

// Function to toggle auto-play TTS
function toggleAutoPlayTTS() {
    autoPlayTTS = !autoPlayTTS;
    console.log("Auto-play TTS toggled to:", autoPlayTTS);
    
    // Save to localStorage
    localStorage.setItem('autoPlayTTS', autoPlayTTS.toString());
    
    // If turning off, clear the queue and stop current audio
    if (!autoPlayTTS) {
        console.log("Clearing TTS queue, had", ttsQueue.length, "items");
        // Reset any buttons that were showing the Queued/Loading indicator.
        ttsQueue.forEach((item) => {
            if (item.playButton) {
                item.playButton.classList.remove("tts-queued", "tts-loading");
                item.playButton.disabled = false;
                item.playButton.textContent = "Play";
            }
        });
        ttsQueue = [];
        isPlayingTTS = false;
        
        // Stop any currently playing audio
        if (currentAudio) {
            currentAudio.pause();
            currentAudio.currentTime = 0;
            if (currentAudio.playButton) {
                currentAudio.playButton.textContent = "Play";
            }
            currentAudio = null;
        }
        
        // Stop any currently playing queued TTS audio
        if (currentQueuedAudio) {
            currentQueuedAudio.pause();
            currentQueuedAudio.currentTime = 0;
            currentQueuedAudio = null;
        }
        currentQueuedMessageId = null;
    }

    updateAutoPlayTTSDisplay();
}

// Function to toggle audio playback
function toggleAudioPlayback(audio, playButton) {
    if (currentAudio && currentAudio !== audio) {
        currentAudio.pause();
        currentAudio.currentTime = 0;
        currentAudio.playButton.textContent = "Play";
    }

    if (audio.paused) {
        audio.play();
        playButton.textContent = "Pause";
    } else {
        audio.pause();
        playButton.textContent = "Play";
    }

    currentAudio = audio;
    currentAudio.playButton = playButton;

    // A listener, not onended: queued audio's lifecycle owns onended &
    // resolves our auto-play queue from it.
    if (!audio._toggleEndedBound) {
        audio._toggleEndedBound = true;
        audio.addEventListener("ended", () => { playButton.textContent = "Play"; });
    }
}
