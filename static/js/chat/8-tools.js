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
    sandbox_share_file: ["Sharing", "Shared"],
    delegate: ["uncloseai-cli is working", "uncloseai-cli worked"],
};

// 18234 -> "18.2k": token counts read at a glance.
function shortCount(n) {
    if (!Number.isFinite(n)) return "";
    if (n >= 1e6) return `${(n / 1e6).toFixed(n >= 1e8 ? 0 : 1)}M`;
    return n >= 1000 ? `${(n / 1000).toFixed(n >= 100000 ? 0 : 1)}k` : String(n);
}

// A changed file as a person reads it: under its home (uncloseai-cli works
// from /root, so "ago/x.py" & "/root/ago/x.py" are one file, shown once).
function homePath(path) {
    const p = String(path);
    if (p.startsWith("/root/")) return "~/" + p.slice(6);
    return p.startsWith("/") ? p : "~/" + p;
}

// The newest pytest tally in a log: "76 passed · 2 failed", or "".
function testTally(text) {
    const lines = String(text || "").split("\n").filter((l) => /\d+ (passed|failed)\b/.test(l));
    if (!lines.length) return "";
    const last = lines[lines.length - 1];
    const counts = [...last.matchAll(/(\d+) (passed|failed|errors?)\b/g)].map((m) => `${m[1]} ${m[2]}`);
    return counts.join(" · ");
}

// 241 -> "4m 1s": how long a job has run.
function shortDuration(s) {
    s = Math.round(s);
    if (s < 60) return `${s}s`;
    const m = Math.floor(s / 60);
    return m < 60 ? `${m}m ${s % 60}s` : `${Math.floor(m / 60)}h ${m % 60}m`;
}

// A running agent's job in full, folded: files it changed, its plan, the
// model's health (calls, retries, empty replies, last error), tokens, time.
function toolJob(vitals) {
    const rows = [];
    const files = vitals.files || {};
    const plan = vitals.plan || {};
    const llm = vitals.llm || {};
    const tokens = vitals.tokens || {};
    const time = vitals.time || {};
    if (plan.total) rows.push(["plan", `${plan.done || 0}/${plan.total} done${plan.current ? " · now: " + plan.current : ""}`]);
    if (files.count !== undefined && files.count !== null) {
        const listed = [...new Set((files.changed || []).map(homePath))];
        rows.push([`files changed (${listed.length || files.count})`, listed.join("\n") || "none"]);
    }
    if (llm.calls !== undefined && llm.calls !== null) {
        let health = `${llm.calls} calls · ${llm.retries || 0} retries · ${llm.empty || 0} empty`;
        // uncloseai-cli rides these out (a thinking-only turn is re-sent
        // with thinking off): the last one, not a run that failed.
        if (llm.last_error) {
            health += `\nlast hiccup: ${llm.last_error}`;
            if (/thinking-only/.test(llm.last_error)) health += " (re-sent with thinking off)";
        }
        rows.push(["model", health]);
    }
    if (tokens.input || tokens.output) rows.push(["tokens", `${shortCount(tokens.input || 0)} in · ${shortCount(tokens.output || 0)} out`]);
    if (Number.isFinite(time.elapsed_s)) {
        rows.push(["time", shortDuration(time.elapsed_s) + (Number.isFinite(time.budget_s) ? ` of ${shortDuration(time.budget_s)}` : "")]);
    }
    if (!rows.length) return null;
    const block = document.createElement("details");
    block.className = "message-tool-detail message-tool-job";
    const summary = document.createElement("summary");
    summary.textContent = "job";
    block.appendChild(summary);
    const list = document.createElement("dl");
    for (const [name, value] of rows) {
        const dt = document.createElement("dt");
        dt.textContent = name;
        const dd = document.createElement("dd");
        dd.textContent = value;
        list.append(dt, dd);
    }
    block.appendChild(list);
    return block;
}

// A small rounded label: a tool offered, or one of a long call's vitals.
function toolChip(text, extra) {
    const chip = document.createElement("span");
    chip.className = "message-tool-chip" + (extra ? " " + extra : "");
    chip.textContent = text;
    return chip;
}

// uncloseai-cli's "no turn limit" is a huge number: shown, it is noise.
const TURN_LIMIT_SHOWN = 1000;

// A running agent's vitals (uncloseai-cli unclose-vitals/1, shaped by
// tools/loop.py) as a row of chips that wraps: live while it runs, a
// summary after. Each chip has a key, so a redraw can tell which changed.
// `extra`: the card's own facts (the log's test tally, its heartbeat).
function toolVitals(vitals, running, extra = {}) {
    const parts = [];
    const put = (key, text, className = "", pct = null) => parts.push({key, text, className, pct});
    const ctx = vitals.context || {};
    const tools = vitals.tools || {};
    const plan = vitals.plan || {};
    const llm = vitals.llm || {};
    if (running) {
        const limit = vitals.max_turns && vitals.max_turns < TURN_LIMIT_SHOWN ? "/" + vitals.max_turns : "";
        if (vitals.turn) put("turn", `turn ${vitals.turn}${limit}`);
        if (ctx.used) {
            const pct = ctx.window ? Math.min(100, Math.round((100 * ctx.used) / ctx.window)) : null;
            put("context", `context ${shortCount(ctx.used)}${ctx.window ? " / " + shortCount(ctx.window) : ""}`, "is-meter", pct);
        }
        if (tools.total) put("tools", `${tools.total} tool call${tools.total === 1 ? "" : "s"}`);
        // What it spends its calls on: its three busiest tools.
        const mix = Object.entries(tools.by_name || {}).sort((a, b) => b[1] - a[1]).slice(0, 3);
        if (mix.length > 1) put("mix", mix.map(([name, n]) => `${name} ${n}`).join(" · "), "is-quiet");
        if (extra.tests) put("tests", "tests " + extra.tests, /failed|error/.test(extra.tests) ? "is-warn" : "is-good");
        const act = vitals.activity || {};
        if (act.label) put("activity", act.label + (Number.isFinite(act.elapsed_s) ? ` ${Math.round(act.elapsed_s)}s` : ""));
        if (llm.retries) put("retries", `${llm.retries} retr${llm.retries === 1 ? "y" : "ies"}`, "is-warn");
        if (llm.empty) put("empty", `${llm.empty} empty repl${llm.empty === 1 ? "y" : "ies"}`, "is-warn");
        const pulse = extra.pulse || {};
        if (Number.isFinite(pulse.moved_s)) put("moved", pulse.moved_s < 20 ? "moving" : `moved ${shortDuration(pulse.moved_s)} ago`, pulse.moved_s >= 300 ? "is-warn" : "is-quiet");
        if (Number.isFinite(pulse.review_in_s)) put("review", `review in ${shortDuration(pulse.review_in_s)}`, "is-quiet");
    } else {
        if (plan.total) put("steps", `${plan.done || 0}/${plan.total} steps`);
        if (vitals.turn) put("turn", `${vitals.turn} turn${vitals.turn === 1 ? "" : "s"}`);
        if (ctx.peak) put("peak", `peak context ${shortCount(ctx.peak)}`);
        if (tools.total) put("tools", `${tools.total} tool call${tools.total === 1 ? "" : "s"}`);
        if (extra.tests) put("tests", "tests " + extra.tests, /failed|error/.test(extra.tests) ? "is-warn" : "is-good");
    }
    if (!parts.length) return null;
    const row = document.createElement("div");
    row.className = "message-tool-vitals";
    for (const part of parts) {
        const chip = toolChip(part.text, part.className);
        chip.dataset.key = part.key;
        if (Number.isFinite(part.pct)) chip.style.setProperty("--pct", part.pct + "%");
        row.appendChild(chip);
    }
    return row;
}

// A running agent's plan as a progress bar: "step 4 of 9 · what it does".
function toolPlan(vitals, fromPct) {
    const plan = (vitals || {}).plan || {};
    if (!plan.total) return null;
    const done = Math.min(plan.done || 0, plan.total);
    const pct = Math.round((100 * done) / plan.total);
    const box = document.createElement("div");
    box.className = "tool-card-plan";
    box.dataset.pct = String(pct);
    const bar = document.createElement("div");
    bar.className = "tool-card-bar";
    // Drawn at its last width, then moved: the fill glides to the new step.
    const from = Number(fromPct);
    bar.style.setProperty("--pct", (Number.isFinite(from) ? from : pct) + "%");
    if (Number.isFinite(from) && from !== pct) {
        requestAnimationFrame(() => requestAnimationFrame(() => bar.style.setProperty("--pct", pct + "%")));
    } else {
        bar.style.setProperty("--pct", pct + "%");
    }
    bar.setAttribute("role", "progressbar");
    bar.setAttribute("aria-valuemin", "0");
    bar.setAttribute("aria-valuemax", String(plan.total));
    bar.setAttribute("aria-valuenow", String(done));
    const text = document.createElement("div");
    text.className = "tool-card-step";
    const step = Math.min(done + 1, plan.total);
    text.textContent = done >= plan.total
        ? `all ${plan.total} steps done`
        : `step ${step} of ${plan.total}${plan.current ? " · " + plan.current : ""}`;
    box.append(bar, text);
    return box;
}

// A delegate is one card: what never changes (who, what, the task it was
// handed) on top; under it what each 15s poll updates, read top-down as
// where it is (plan), how it runs (vitals), the detail (job & log folds),
// & last the sentence that sums it up, where chat's scroll & the eye
// land (fox, 2026-10-31).
function drawDelegateCard(line, data, verbs, open) {
    const running = data.status === "running";
    // What the last tick showed, so this one moves only what changed: a new
    // summary slides in, a changed number flashes, the plan bar glides.
    const before = {
        summary: line.dataset.summary,
        pct: line.dataset.planPct,
        chips: JSON.parse(line.dataset.chips || "{}"),
    };
    const add = (className, child) => {
        if (!child) return null;
        child.classList.add(className);
        line.appendChild(child);
        return child;
    };
    const div = (text) => {
        const el = document.createElement("div");
        if (text !== undefined) el.textContent = text;
        return el;
    };
    const where = toolWhere(data);
    // A chat's delegates share one session: say when this one carries on.
    const resumed = data.session && data.session.resumed ? " · continuing its session" : "";
    const head = add("tool-card-head", div(`${verbs[running ? 0 : 1]}${where ? " · " + where : ""}${resumed}`));
    head.classList.add("message-tool-head");
    if (data.status === "error") {
        const why = document.createElement("span");
        why.className = "message-tool-error";
        why.textContent = ` (failed: ${data.error})`;
        head.appendChild(why);
    }
    const elapsed = ((data.vitals || {}).time || {}).elapsed_s;
    if (Number.isFinite(elapsed)) add("tool-card-clock", div(shortDuration(elapsed)));
    add("tool-card-title", div(data.label)).classList.add("message-tool-label");
    // The task never changes: it sits with the title.
    if (data.detail) {
        const fold = toolDetail(data.detail, running, data.tool);
        // Open while the sandbox & uncloseai-cli start, closed once its
        // summaries tell how it goes, like the log; a reader's own choice
        // wins after that (fox, 2026-10-31).
        fold.open = line.dataset.taskTouched ? open.detail : running && !data.summary;
        fold.querySelector("summary").addEventListener("click", () => { line.dataset.taskTouched = "1"; });
        add("tool-card-task", fold);
    }
    // Its overseer's latest review (every few minutes): continue, steered
    // with a note, or stopped & why.
    add("tool-card-oversight", toolOversight(data.oversight));
    if (running) {
        const plan = add("tool-card-plan-row", toolPlan(data.vitals, before.pct));
        if (plan) line.dataset.planPct = plan.dataset.pct;
    }
    const tests = testTally(data.progress || (data.detail || {}).log || "");
    if (data.vitals) {
        const row = add("tool-card-vitals", toolVitals(data.vitals, running, {tests, pulse: data.pulse}));
        const now = {};
        for (const chip of row ? row.children : []) {
            now[chip.dataset.key] = chip.textContent;
            const was = before.chips[chip.dataset.key];
            if (was !== undefined && was !== chip.textContent) chip.classList.add("is-changed");
        }
        line.dataset.chips = JSON.stringify(now);
    }
    const folds = div();
    folds.className = "tool-card-folds";
    const job = data.vitals ? toolJob(data.vitals) : null;
    if (job) {
        job.open = open.job;
        folds.appendChild(job);
    }
    if (data.progress && running) folds.appendChild(toolLog(data.progress, open.log));
    if (folds.children.length) line.appendChild(folds);
    if (data.summary && running) {
        const said = add("message-tool-summary", div(data.summary));
        if (data.summary !== before.summary) said.classList.add("is-new");
        line.dataset.summary = data.summary;
    }
    const sources = toolSources(data.sources);
    if (sources) add("tool-card-sources", sources);
}

// The overseer's latest call on a delegate (tools/oversight.py).
function toolOversight(review) {
    if (!review || !review.decision) return null;
    const box = document.createElement("div");
    box.className = "is-" + review.decision;
    const said = {continue: "continue", steer: "steered", stop: "stopped"}[review.decision] || review.decision;
    let text = `Overseer${review.at ? " · " + review.at : ""}: ${said}`;
    if (review.note) text += ` — ${review.note}`;
    if (review.undelivered) {
        const why = typeof review.undelivered === "string" ? review.undelivered : "this sandbox's uncloseai-cli takes no notes yet";
        text += ` (not delivered: ${why})`;
    }
    box.textContent = text;
    return box;
}

// A delegate's newest session lines, folded; it grows as the run goes.
function toolLog(progress, open) {
    const log = document.createElement("details");
    log.className = "message-tool-log";
    const summary = document.createElement("summary");
    const count = progress.split("\n").length;
    summary.textContent = `log · ${count} line${count === 1 ? "" : "s"}`;
    log.appendChild(summary);
    const body = document.createElement("pre");
    body.className = "message-tool-progress";
    body.textContent = progress;
    log.appendChild(body);
    log.open = open;
    return log;
}

// Pages a call found, or files it made (our own /attachments/ links).
function toolSources(found) {
    const sources = (found || []).filter(
        (s) => /^https?:\/\//.test(s.url) || /^\/attachments\/[0-9a-f]{32}\//.test(s.url));
    if (!sources.length) return null;
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
    return list;
}

// One tool's line, drawn from a tool_activity event or a saved trail entry.
function drawToolLine(line, data) {
    if (data.tool === "offered") {
        // The tools a reply could call stay in its trail (downloads show
        // them) but are not drawn: a list of tools it never used is noise
        // (fox, 2026-10-31).
        line.replaceChildren();
        line.hidden = true;
        line.classList.add("is-offered");
        return;
    }
    const verbs = TOOL_VERBS[data.tool] || [`Running ${data.tool}`, `Ran ${data.tool}`];
    // Every report redraws the line; each fold keeps the state the reader
    // left it in (they snapped shut on each delegate update, 2026-10-31).
    // First drawn, a delegate shows its task & job open and its log
    // closed: the summaries tell how the work goes, the log is a click
    // away (fox, 2026-10-31).
    const delegating = data.tool === "delegate";
    const kept = (selector, first) => {
        const fold = line.querySelector(selector);
        return fold ? fold.open : first;
    };
    const wasOpen = kept(".message-tool-detail:not(.message-tool-job)", delegating);
    const jobWasOpen = kept(".message-tool-job", delegating);
    const logWasOpen = kept(".message-tool-log", false);
    line.replaceChildren();
    line.classList.toggle("is-running", data.status === "running");
    line.classList.toggle("is-error", data.status === "error");
    line.classList.toggle("is-card", delegating);
    if (delegating) {
        drawDelegateCard(line, data, verbs, {detail: wasOpen, job: jobWasOpen, log: logWasOpen});
        return;
    }
    const where = toolWhere(data);
    const verb = verbs[data.status === "running" ? 0 : 1];
    const head = document.createElement("div");
    head.className = "message-tool-head";
    // What & where in quiet text, what it was about in full colour.
    head.append(`${verb}${where ? " " + where : ""}: `);
    const about = document.createElement("span");
    about.className = "message-tool-label";
    about.textContent = data.label;
    head.appendChild(about);
    if (data.status === "running") head.append("…");
    line.appendChild(head);
    if (data.status === "error") {
        const why = document.createElement("span");
        why.className = "message-tool-error";
        why.textContent = ` (failed: ${data.error})`;
        head.appendChild(why);
    }
    if (data.detail && data.detail.input) line.appendChild(toolPreview(data.detail.input));
    if (data.detail) {
        const fold = toolDetail(data.detail, data.status === "running", data.tool);
        fold.open = wasOpen;
        line.appendChild(fold);
    }
    const sources = toolSources(data.sources);
    if (sources) line.appendChild(sources);
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
// A copy button for a block of output (the delegation log).
function copyButton(text) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "copy-button message-tool-copy";
    button.textContent = "Copy";
    button.addEventListener("click", async () => {
        try {
            await navigator.clipboard.writeText(text);
            button.textContent = "Copied";
        } catch (e) {
            button.textContent = "Copy failed";
        }
        setTimeout(() => { button.textContent = "Copy"; }, 1500);
    });
    return button;
}

function toolDetail(detail, running, tool) {
    const block = document.createElement("details");
    block.className = "message-tool-detail";
    const summary = document.createElement("summary");
    // While it runs there is only the code (or task) to open.
    summary.textContent = tool === "delegate"
        ? (running ? "task" : "task & output")
        : (running ? "code" : "code & output");
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
    const language = String(detail.language || "").toLowerCase();
    // A delegate's task is prose: it wraps; code keeps its lines.
    const prose = tool === "delegate" || !language;
    const input = pre(detail.input || "", "message-tool-input" + (prose ? " is-prose" : ""));
    if (window.hljs && language && hljs.getLanguage(language)) {
        input.className = "language-" + language;
        hljs.highlightElement(input);
    }
    if (detail.log) {
        // A delegate's log, as it ran (tools/loop.py merge_log).
        const title = document.createElement("div");
        title.className = "message-tool-log-title";
        title.textContent = "delegation log";
        if (!running) title.appendChild(copyButton(detail.log));
        block.appendChild(title);
        pre(detail.log, "message-tool-output is-log");
    }
    if (detail.stdout) pre(detail.stdout, "message-tool-output");
    if (detail.answer) pre(detail.answer, "message-tool-output");
    if (detail.stderr) {
        // A delegate's stderr is uncloseai-cli's own commentary (spinner,
        // "slow call" timings): notes, red only when the run failed.
        const failed = detail.exit_code !== undefined && detail.exit_code !== null && detail.exit_code !== 0;
        if (tool === "delegate" && !failed) {
            const title = document.createElement("div");
            title.className = "message-tool-log-title";
            title.textContent = "uncloseai-cli notes";
            block.appendChild(title);
            pre(detail.stderr, "message-tool-output is-notes");
        } else {
            pre(detail.stderr, "message-tool-output is-stderr");
        }
    }
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
        // The key live events find a running line by: a page opened (or
        // refreshed) mid-run keeps updating the card it drew, never starts
        // a second one below it (fox, 2026-10-31).
        line.dataset.toolKey = toolKey(entry);
        drawToolLine(line, entry);
        lines.appendChild(line);
    }
    addText(content.slice(pos));
}

// Which line an event belongs to: its tool & what it was about.
function toolKey(data) {
    return `${data.tool}\u0000${data.label || ""}`;
}

socket.on("tool_activity", (data) => {
    const wrapperId = "message-" + data.id;
    let messageWrapper = document.getElementById(wrapperId);
    if (!messageWrapper) {
        messageWrapper = document.createElement("div");
        messageWrapper.className = "message-wrapper is-model";
        messageWrapper.id = wrapperId;
        document.getElementById("chat").appendChild(messageWrapper);
    }
    const lines = toolLines(messageWrapper, data);
    let line = lines.lastElementChild;
    // A running line is updated in place by its own later events: its end,
    // & a long call's progress (also "running"), which used to start a
    // second line for the same delegate (2026-10-31).
    const key = toolKey(data);
    const same = line && line.classList.contains("is-running") &&
        (data.status !== "running" || line.dataset.toolKey === key);
    if (!same) {
        line = document.createElement("li");
        line.className = "message-tool";
        lines.appendChild(line);
    }
    line.dataset.toolKey = key;
    drawToolLine(line, data);
    if (!userHasScrolledUp) {
        document.getElementById("chat").scrollTop = document.getElementById("chat").scrollHeight;
    }
});
