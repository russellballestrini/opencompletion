// Our composer's tools, shared by home & chat (_composer_tools.html):
// attach files (pick, drop or paste; uploaded on send, each within our
// limit) & speak instead of typing (recorded here, transcribed by our
// speech service, never a browser vendor's).
//
// const tools = composerTools(form, textarea);
// tools.hasFiles(); await tools.uploadAll(roomName) -> markdown to send.

function formatSize(bytes) {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

// How a sent file reads in a message: images show, other files link.
// Names are markdown-escaped so a file can't inject a link or markup.
function attachmentMarkdown(file) {
    const name = String(file.filename).replace(/([\\`*_{}\[\]()#+\-.!<>|~])/g, '\\$1');
    // Spaces & parentheses in a name would end a markdown link early.
    const url = encodeURI(file.url).replace(/\(/g, '%28').replace(/\)/g, '%29');
    return file.is_image
        ? `![${name}](${url})`
        : `[📎 ${name} · ${formatSize(file.size)}](${url})`;
}

function composerTools(form, textarea) {
    const maxBytes = Number(form.dataset.maxUpload) || 7 * 1024 * 1024;
    const fileInput = form.querySelector('.composer-file-input');
    const attachButton = form.querySelector('[data-attach]');
    const micButton = form.querySelector('[data-mic]');
    const queued = [];

    const chips = document.createElement('ul');
    chips.className = 'composer-files';
    chips.hidden = true;
    textarea.before(chips);
    const status = document.createElement('p');
    status.className = 'composer-status';
    status.setAttribute('role', 'status');
    form.after(status);

    function say(text) {
        status.textContent = text || '';
    }

    function render() {
        chips.textContent = '';
        queued.forEach((file, index) => {
            const chip = document.createElement('li');
            chip.className = 'composer-file';
            const label = document.createElement('span');
            label.textContent = `${file.name} · ${formatSize(file.size)}`;
            const remove = document.createElement('button');
            remove.type = 'button';
            remove.textContent = '×';
            remove.setAttribute('aria-label', `Remove ${file.name}`);
            remove.onclick = () => { queued.splice(index, 1); render(); };
            chip.append(label, remove);
            chips.appendChild(chip);
        });
        chips.hidden = queued.length === 0;
    }

    function add(files) {
        const refused = [];
        [...files].forEach(file => {
            if (file.size > maxBytes) refused.push(file.name);
            else if (file.size > 0) queued.push(file);
        });
        render();
        say(refused.length
            ? `Not attached (over ${formatSize(maxBytes)} each): ${refused.join(', ')}`
            : '');
    }

    attachButton.addEventListener('click', () => fileInput.click());
    fileInput.addEventListener('change', () => { add(fileInput.files); fileInput.value = ''; });
    form.addEventListener('dragover', event => { event.preventDefault(); form.classList.add('composer-drop'); });
    form.addEventListener('dragleave', () => form.classList.remove('composer-drop'));
    form.addEventListener('drop', event => {
        event.preventDefault();
        form.classList.remove('composer-drop');
        add(event.dataTransfer.files);
    });
    textarea.addEventListener('paste', event => {
        const files = event.clipboardData && event.clipboardData.files;
        if (files && files.length) {
            event.preventDefault();
            add(files);
        }
    });

    // Upload every queued file to `roomName`; resolves to the markdown that
    // links them, or rejects with a message (nothing is sent then).
    async function uploadAll(roomName) {
        const links = [];
        for (const file of queued) {
            say(`Uploading ${file.name}…`);
            const body = new FormData();
            body.append('file', file, file.name);
            let response;
            try {
                response = await fetch(`/api/rooms/${encodeURIComponent(roomName)}/attachments`, {method: 'POST', body});
            } catch (error) {
                say('');
                throw new Error(`Network error: ${file.name} was not uploaded, so nothing was sent.`);
            }
            const data = await response.json().catch(() => ({}));
            if (!response.ok) {
                say('');
                throw new Error(`${file.name}: ${data.error || 'upload failed'} Nothing was sent.`);
            }
            links.push(attachmentMarkdown(data));
        }
        queued.length = 0;
        render();
        say('');
        return links.join('\n\n');
    }

    // Voice: record until tapped again, then our server transcribes it.
    let recorder = null;
    const canRecord = !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia && window.MediaRecorder);
    micButton.hidden = !canRecord;

    function insertAtCursor(text) {
        const start = textarea.selectionStart ?? textarea.value.length;
        const end = textarea.selectionEnd ?? start;
        const before = textarea.value.slice(0, start);
        const spacer = before && !/\s$/.test(before) ? ' ' : '';
        textarea.value = before + spacer + text + textarea.value.slice(end);
        textarea.dispatchEvent(new Event('input'));
        textarea.focus();
    }

    async function transcribe(blob) {
        say('Transcribing…');
        const body = new FormData();
        const type = blob.type || 'audio/webm';
        const ext = type.includes('ogg') ? 'ogg' : type.includes('mp4') ? 'mp4' : 'webm';
        body.append('file', blob, `speech.${ext}`);
        try {
            const response = await fetch('/speech/transcribe', {method: 'POST', body});
            const data = await response.json().catch(() => ({}));
            if (!response.ok) throw new Error(data.error || 'Voice input failed.');
            if (data.text) insertAtCursor(data.text);
            say(data.text ? '' : 'No speech was heard.');
        } catch (error) {
            say(error instanceof TypeError ? 'Network error: nothing was transcribed.' : error.message);
        }
    }

    micButton.addEventListener('click', async () => {
        if (recorder) {
            recorder.stop();
            return;
        }
        let stream;
        try {
            stream = await navigator.mediaDevices.getUserMedia({audio: true});
        } catch (error) {
            say('Microphone blocked: allow it for this site to speak instead of typing.');
            return;
        }
        const parts = [];
        recorder = new MediaRecorder(stream);
        recorder.ondataavailable = event => { if (event.data.size) parts.push(event.data); };
        recorder.onstop = () => {
            stream.getTracks().forEach(track => track.stop());
            micButton.setAttribute('aria-pressed', 'false');
            const blob = new Blob(parts, {type: recorder.mimeType});
            recorder = null;
            if (blob.size > maxBytes) say('That recording is too long; try a shorter one.');
            else if (blob.size) transcribe(blob);
            else say('');
        };
        recorder.start();
        micButton.setAttribute('aria-pressed', 'true');
        say('Listening… tap the mic again to stop.');
    });

    return {
        hasFiles: () => queued.length > 0,
        firstFileName: () => (queued[0] ? queued[0].name.replace(/\.[^.]+$/, '') : ''),
        uploadAll,
        say,
    };
}
