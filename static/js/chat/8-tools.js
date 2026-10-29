// Tools a model calls that run here, in the asker's own browser, so a
// guest's search goes out from their network, never ours (tools/browser.py
// asks; docs/TOOLS.md has the whole picture). Also draws each tool's
// line inside a reply ("Searched the web from your browser: …"), live &
// from a reply's saved trail in history.
//
// The server sends `tool_request` only to the socket that sent the message
// & takes a `tool_result` only from it. Our search is a port of
// tools/keyless_search.py: the same vendors' public free tiers, keyless,
// round robin from a random start, failing over to the next vendor. Four
// of its five answer a browser (CORS); Keenable needs a header its CORS
// rules refuse, so it stays on the server's ring only.

const KEYLESS_TIMEOUT_MS = 20000;
const KEYLESS_RING = ["exa", "parallel", "tavily", "firecrawl"];
let keylessCursor = Math.floor(Math.random() * KEYLESS_RING.length);
// Parallel's free tier wants a session id for its rate limit: a random
// one per page, derived from nothing.
const KEYLESS_SESSION = Array.from(crypto.getRandomValues(new Uint8Array(16)),
    (b) => b.toString(16).padStart(2, "0")).join("");

async function keylessPost(url, payload, headers) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), KEYLESS_TIMEOUT_MS);
    try {
        const response = await fetch(url, {
            method: "POST",
            headers: Object.assign({"Content-Type": "application/json"}, headers || {}),
            body: JSON.stringify(payload),
            credentials: "omit",
            referrerPolicy: "no-referrer",
            signal: controller.signal,
        });
        const body = await response.text();
        if (!response.ok) throw new Error(`HTTP ${response.status}: ${body.slice(0, 200)}`);
        return body;
    } finally {
        clearTimeout(timer);
    }
}

// The text of a JSON-RPC tools/call answer, plain JSON or an SSE stream.
function toolCallText(body) {
    const payloads = body.trim().startsWith("{") ? [body] :
        body.split("\n").filter((line) => line.startsWith("data:"))
            .map((line) => line.slice(5).trim());
    for (const payload of payloads) {
        let message;
        try { message = JSON.parse(payload); } catch (e) { continue; }
        if (message.error) throw new Error(message.error.message || "tool call failed");
        const result = message.result;
        if (!result) continue;
        const text = (result.content || []).filter((part) => part.type === "text")
            .map((part) => part.text).join("\n");
        if (result.isError) throw new Error(text || "tool call failed");
        if (text) return text;
    }
    throw new Error("unrecognized response shape");
}

function jsonRpc(url, tool, args) {
    return keylessPost(url, {
        jsonrpc: "2.0", id: 1, method: "tools/call",
        params: {name: tool, arguments: args},
    }, {"Accept": "application/json, text/event-stream"}).then(toolCallText);
}

const KEYLESS_VENDORS = {
    async exa(query, limit) {
        const text = await jsonRpc("https://mcp.exa.ai/mcp", "web_search_exa",
            {query: query, numResults: limit});
        const results = [];
        for (const block of text.split("\n---\n")) {
            let title = "", url = "", inHighlights = false;
            const highlights = [];
            for (const raw of block.split("\n")) {
                const line = raw.trim();
                if (line.startsWith("Title:")) { title = line.slice(6).trim(); inHighlights = false; }
                else if (line.startsWith("URL:")) { url = line.slice(4).trim(); inHighlights = false; }
                else if (line.startsWith("Highlights:")) inHighlights = true;
                else if (line.startsWith("Published:") || line.startsWith("Author:")) inHighlights = false;
                else if (inHighlights && line) highlights.push(line);
            }
            if (url) results.push({url: url, title: title, description: highlights.join(" ")});
            if (results.length >= limit) break;
        }
        return results;
    },
    async parallel(query, limit) {
        const text = await jsonRpc("https://search.parallel.ai/mcp", "web_search",
            {objective: query, search_queries: [query], session_id: KEYLESS_SESSION});
        return (JSON.parse(text).results || []).slice(0, limit).map((r) => ({
            url: r.url || "", title: r.title || "", description: (r.excerpts || []).join(" "),
        }));
    },
    async tavily(query, limit) {
        const body = await keylessPost("https://api.tavily.com/search",
            {query: query, max_results: limit}, {"X-Tavily-Access-Mode": "keyless"});
        return (JSON.parse(body).results || []).slice(0, limit).map((r) => ({
            url: r.url || "", title: r.title || "", description: r.content || "",
        }));
    },
    async firecrawl(query, limit) {
        const body = await keylessPost("https://api.firecrawl.dev/v2/search",
            {query: query, limit: limit});
        return (((JSON.parse(body).data || {}).web) || []).slice(0, limit).map((r) => ({
            url: r.url || "", title: r.title || "", description: r.description || "",
        }));
    },
};

// Every vendor in turn from our cursor until one answers. Unlike the
// server's ring, any failure moves on: a browser can't read the status of
// a response CORS blocked, so it can't tell a throttle from an outage.
async function keylessSearch(query, limit) {
    const start = keylessCursor;
    keylessCursor = (keylessCursor + 1) % KEYLESS_RING.length;
    const errors = [];
    for (let i = 0; i < KEYLESS_RING.length; i++) {
        const name = KEYLESS_RING[(start + i) % KEYLESS_RING.length];
        try {
            const results = await KEYLESS_VENDORS[name](query, limit);
            return {success: true, served_by: name, results: results};
        } catch (e) {
            errors.push(`${name}: ${e.message || e}`);
        }
    }
    return {success: false, error: errors.join("; ")};
}

const BROWSER_TOOLS = {
    web_search: (args) => keylessSearch(String(args.query || ""),
        Math.max(1, Math.min(10, Number(args.limit) || 5))),
};

socket.on("tool_request", async (data) => {
    const tool = BROWSER_TOOLS[data.tool];
    let result;
    try {
        result = tool ? await tool(data.arguments || {}) : {error: `${data.tool} does not run in a browser`};
    } catch (e) {
        result = {error: String(e.message || e)};
    }
    socket.emit("tool_result", {request_id: data.request_id, result: result});
});

// The tool list a new line joins: the last thing in its reply's flow if
// that is a tool list, else a new one after the text so far.
function toolLines(messageWrapper, data) {
    const content = replyFlow(messageWrapper);
    replyHeader(messageWrapper, data.asker, data.model_name);
    const last = content.lastElementChild;
    if (last && last.classList.contains("message-tools")) return last;
    const lines = document.createElement("ul");
    lines.className = "message-tools";
    content.appendChild(lines);
    return lines;
}

function toolWhere(data) {
    if (data.where === "server") return "from our server";
    if (data.where === "unsandbox") return "on unsandbox";
    if (data.where === "sandbox") return "in this chat's sandbox";
    if (data.where === "browser") {
        return data.asker === username ? "from your browser" : `from ${data.asker}'s browser`;
    }
    return "";
}

const TOOL_VERBS = {
    web_search: ["Searching the web", "Searched the web"],
    web_fetch: ["Reading", "Read"],
    run_code: ["Running", "Ran"],
    sandbox_shell: ["Running", "Ran"],
    sandbox_write_file: ["Writing", "Wrote"],
    delegate: ["uncloseai-cli is working", "uncloseai-cli worked"],
};

// One tool's line, drawn from a tool_activity event or a saved trail entry.
function drawToolLine(line, data) {
    if (data.tool === "offered") {
        // The reply's first line: the tools it may call (app.chat_gpt).
        line.replaceChildren();
        line.classList.add("is-offered");
        line.textContent = "Tools: " + (data.names || []).join(", ");
        return;
    }
    const verbs = TOOL_VERBS[data.tool] || [`Running ${data.tool}`, `Ran ${data.tool}`];
    line.replaceChildren();
    line.classList.toggle("is-running", data.status === "running");
    line.classList.toggle("is-error", data.status === "error");
    const where = toolWhere(data);
    const verb = verbs[data.status === "running" ? 0 : 1];
    const head = document.createElement("span");
    head.className = "message-tool-head";
    head.textContent = `${verb}${where ? " " + where : ""}: ${data.label}` +
        (data.status === "running" ? "…" : "");
    line.appendChild(head);
    if (data.status === "error") {
        const why = document.createElement("span");
        why.className = "message-tool-error";
        why.textContent = ` (failed: ${data.error})`;
        line.appendChild(why);
    }
    if (data.detail && data.detail.input) line.appendChild(toolPreview(data.detail.input));
    if (data.detail && data.status !== "running") line.appendChild(toolDetail(data.detail));
    // Pages it found, or files it made (our own /attachments/ links).
    const sources = (data.sources || []).filter(
        (s) => /^https?:\/\//.test(s.url) || /^\/attachments\/\d+\//.test(s.url));
    if (sources.length) {
        const list = document.createElement("ol");
        list.className = "message-tool-sources";
        for (const source of sources) {
            const item = document.createElement("li");
            const link = document.createElement("a");
            link.href = source.url;
            link.target = "_blank";
            link.rel = "noopener noreferrer";
            link.textContent = source.title || source.url;
            item.appendChild(link);
            list.appendChild(item);
        }
        line.appendChild(list);
    }
}

// The first lines of a code tool's code or command, always in view.
const PREVIEW_LINES = 4;
function toolPreview(input) {
    const lines = String(input).replace(/\s+$/, "").split("\n");
    const preview = document.createElement("pre");
    preview.className = "message-tool-preview";
    const shown = lines.slice(0, PREVIEW_LINES).join("\n");
    preview.textContent = lines.length > PREVIEW_LINES
        ? `${shown}\n… ${lines.length - PREVIEW_LINES} more lines` : shown;
    return preview;
}

// A code tool's code (or command) & what came back, folded under its line.
function toolDetail(detail) {
    const block = document.createElement("details");
    block.className = "message-tool-detail";
    const summary = document.createElement("summary");
    summary.textContent = "code & output";
    block.appendChild(summary);
    const pre = (text, className) => {
        const box = document.createElement("pre");
        box.className = className;
        const code = document.createElement("code");
        code.textContent = text;
        box.appendChild(code);
        block.appendChild(box);
        return code;
    };
    const input = pre(detail.input || "", "message-tool-input");
    const language = String(detail.language || "").toLowerCase();
    if (window.hljs && language && hljs.getLanguage(language)) {
        input.className = "language-" + language;
        hljs.highlightElement(input);
    }
    if (detail.stdout) pre(detail.stdout, "message-tool-output");
    if (detail.answer) pre(detail.answer, "message-tool-output");
    if (detail.stderr) pre(detail.stderr, "message-tool-output is-stderr");
    if (detail.exit_code !== undefined && detail.exit_code !== null) {
        const exit = document.createElement("div");
        exit.className = "message-tool-exit";
        exit.textContent = `exit code ${detail.exit_code}`;
        block.appendChild(exit);
    }
    return block;
}

// A saved reply (Message.tool_trail) in the order it happened: its text
// cut at each tool line's `at`, the lines between. Trails saved before
// lines carried `at` put every line first.
function renderFlow(container, content, trail, label) {
    container.replaceChildren();
    const addText = (text) => {
        if (!text.trim()) return;
        const part = document.createElement("div");
        part.className = "message-part";
        part.innerHTML = renderMarkdown(text);
        container.appendChild(part);
    };
    if (label) addText(label);
    let pos = 0;
    let lines = null;
    for (const entry of trail) {
        const at = Number.isInteger(entry.at) ? Math.min(Math.max(entry.at, pos), content.length) : pos;
        if (at > pos) {
            addText(content.slice(pos, at));
            pos = at;
            lines = null;
        }
        if (!lines) {
            lines = document.createElement("ul");
            lines.className = "message-tools";
            container.appendChild(lines);
        }
        const line = document.createElement("li");
        line.className = "message-tool";
        drawToolLine(line, entry);
        lines.appendChild(line);
    }
    addText(content.slice(pos));
}

socket.on("tool_activity", (data) => {
    const wrapperId = "message-" + data.id;
    let messageWrapper = document.getElementById(wrapperId);
    if (!messageWrapper) {
        messageWrapper = document.createElement("div");
        messageWrapper.className = "message-wrapper";
        messageWrapper.id = wrapperId;
        document.getElementById("chat").appendChild(messageWrapper);
    }
    const lines = toolLines(messageWrapper, data);
    let line = lines.lastElementChild;
    if (data.status === "running" || !line || !line.classList.contains("is-running")) {
        line = document.createElement("li");
        line.className = "message-tool";
        lines.appendChild(line);
    }
    drawToolLine(line, data);
    if (!userHasScrolledUp) {
        document.getElementById("chat").scrollTop = document.getElementById("chat").scrollHeight;
    }
});
