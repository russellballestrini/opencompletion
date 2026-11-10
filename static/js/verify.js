// Verify a transcript, in this browser: a JSON download of a room
// (opencompletion-transcript/1) carries, per message, its chain event line
// & a merkle-v1 proof under the transcript's root. This file is
// chain.verify_message (chain.py) & the rules it uses from provenance.py,
// ported byte for byte; nothing is uploaded & nothing is fetched.
//
// The rules (unfirehose-chain-v1, jsonl-bytes-v1, merkle-v1):
//   words    sha256(UTF-8 of a message's content), hex
//   event    a line ends ,"hash":"<64 hex>"} ; its preimage is the line's
//            exact bytes without that tail, closed with } ; the event's
//            hash is sha256(preimage). No canonical JSON anywhere.
//   leaf     sha256(0x00 || the event hash's 32 bytes)
//   node     sha256(0x03 || left || right); a proof step with is_left
//            puts its sibling on the LEFT of the running hash
//
// Pure functions run under Node too (tests/integration/
// test_verify_transcript.py feeds them a real download); the page part
// runs only where there is a document.

const VERIFY_SCHEMA = 'opencompletion-transcript/1';
const VERIFY_HASH_KEY = ',"hash":"';
const VERIFY_TAIL_LEN = VERIFY_HASH_KEY.length + 64 + 2; // ,"hash":"<64 hex>"}
const VERIFY_HEX64 = /^[0-9a-f]{64}$/;

// Why a message did not verify: chain.verify_message's own words.
const VERIFY_WHY = {
    verified: 'verified',
    unchained: 'not chained',
    line: 'the event line does not hash to its event',
    other: 'the event line is about another message',
    words: 'the words do not match the digest the chain recorded',
    leaf: 'the proof is for another event',
    path: 'the proof does not lead to its root',
    root: 'the proof leads to another root',
    malformed: 'the chain block is malformed',
};

function verifySubtle() {
    const subtle = globalThis.crypto && globalThis.crypto.subtle;
    if (!subtle) {
        throw new Error('This browser offers no SHA-256 here (crypto.subtle needs https or localhost).');
    }
    return subtle;
}

async function sha256Bytes(bytes) {
    return new Uint8Array(await verifySubtle().digest('SHA-256', bytes));
}

function toHex(bytes) {
    let out = '';
    for (const byte of bytes) out += byte.toString(16).padStart(2, '0');
    return out;
}

// bytes.fromhex, strict: pairs of hex digits or an error.
function fromHex(hex) {
    if (typeof hex !== 'string' || hex.length % 2 || !/^[0-9a-fA-F]*$/.test(hex)) {
        throw new Error('not hex');
    }
    const out = new Uint8Array(hex.length / 2);
    for (let i = 0; i < out.length; i++) out[i] = parseInt(hex.substr(i * 2, 2), 16);
    return out;
}

function concatBytes(...parts) {
    const out = new Uint8Array(parts.reduce((sum, part) => sum + part.length, 0));
    let at = 0;
    for (const part of parts) {
        out.set(part, at);
        at += part.length;
    }
    return out;
}

function utf8(text) {
    return new TextEncoder().encode(text);
}

// chain.sha256_text: sha256 hex of the words as UTF-8, null for no words.
async function sha256Text(text) {
    if (text === null || text === undefined) return null;
    return toHex(await sha256Bytes(utf8(String(text))));
}

// provenance.split_chained_line: [preimage bytes, claimed hash] for a line
// in chain-v1 shape, else null. Shape only; hashing is the caller's job.
function splitChainedLine(line) {
    let end = line.length;
    while (end && (line[end - 1] === 0x0a || line[end - 1] === 0x0d)) end--;
    if (end < VERIFY_TAIL_LEN + 1) return null;
    const ascii = (from, to) => String.fromCharCode(...line.subarray(from, to));
    if (ascii(end - 2, end) !== '"}') return null;
    const tail = end - VERIFY_TAIL_LEN;
    if (ascii(tail, tail + VERIFY_HASH_KEY.length) !== VERIFY_HASH_KEY) return null;
    const digest = ascii(tail + VERIFY_HASH_KEY.length, end - 2);
    if (!VERIFY_HEX64.test(digest)) return null;
    return [concatBytes(line.subarray(0, tail), utf8('}')), digest];
}

async function hashLeaf(content) {
    return sha256Bytes(concatBytes(Uint8Array.of(0x00), content));
}

async function hashCombine(left, right) {
    if (left.length !== 32 || right.length !== 32) throw new Error('hash inputs must be 32 bytes');
    return sha256Bytes(concatBytes(Uint8Array.of(0x03), left, right));
}

// provenance.verify_proof's walk: the root a proof's siblings lead to.
async function proofRoot(leaf, siblings) {
    let current = leaf;
    for (const step of siblings) {
        const sibling = fromHex(step.hash);
        current = step.is_left ? await hashCombine(sibling, current) : await hashCombine(current, sibling);
    }
    return current;
}

// chain.verify_message: {ok, why} for one transcript message & its `chain`
// block. `root` is the root the reader trusts (the transcript's own here).
// One check more than the Python: the line's bytes are hashed again, so a
// line rewritten under its old hash is caught too.
async function verifyMessage(content, block, root) {
    const no = (why) => ({ok: false, why});
    if (!block) return no(VERIFY_WHY.unchained);
    try {
        if (typeof block.line !== 'string' || typeof block.event !== 'string') return no(VERIFY_WHY.malformed);
        const split = splitChainedLine(utf8(block.line));
        if (split === null || split[1] !== block.event) return no(VERIFY_WHY.line);
        if (toHex(await sha256Bytes(split[0])) !== block.event) return no(VERIFY_WHY.line);
        const body = JSON.parse(new TextDecoder().decode(split[0]));
        let recorded = null;
        if (body.type === 'fork') { // a fork's event records every copy
            const copy = (body.copied || []).find((c) => c.messageId === block.messageId);
            recorded = copy && copy.contentSha256 !== undefined ? copy.contentSha256 : null;
        } else if (body.messageId === block.messageId && body.messageId !== undefined) {
            recorded = body.contentSha256 === undefined ? null : body.contentSha256;
        } else {
            return no(VERIFY_WHY.other);
        }
        const claimed = block.contentSha256 === undefined ? null : block.contentSha256;
        if (recorded !== claimed || (await sha256Text(content)) !== recorded) return no(VERIFY_WHY.words);
        const proof = block.proof;
        if (!proof || !Array.isArray(proof.siblings)) return no(VERIFY_WHY.malformed);
        const leaf = fromHex(proof.leaf);
        if (toHex(leaf) !== toHex(await hashLeaf(fromHex(block.event)))) return no(VERIFY_WHY.leaf);
        const proofsRoot = toHex(fromHex(proof.root));
        if (toHex(await proofRoot(leaf, proof.siblings)) !== proofsRoot) return no(VERIFY_WHY.path);
        if (root !== null && root !== undefined && proofsRoot !== root) return no(VERIFY_WHY.root);
        return {ok: true, why: VERIFY_WHY.verified};
    } catch (error) {
        if (error && /SHA-256/.test(error.message)) throw error;
        return no(VERIFY_WHY.malformed);
    }
}

// A dropped file's text as a transcript, or an Error whose message says
// what was wrong & what to do.
function parseTranscript(text) {
    const trimmed = String(text).replace(/^﻿/, '').trim();
    if (!trimmed) throw new Error('This file is empty. Pick a JSON transcript downloaded from a room.');
    let doc;
    try {
        doc = JSON.parse(trimmed);
    } catch (error) {
        if (/^# Chat History for /.test(trimmed) || /^#{1,3} /m.test(trimmed)) {
            throw new Error('This is a Markdown transcript: it shows each digest but carries no proofs. Download the JSON one from the same room (Download history: JSON) & drop that.');
        }
        throw new Error('This file is not JSON. Pick the JSON transcript a room offers under Download history.');
    }
    if (doc === null || typeof doc !== 'object' || Array.isArray(doc)) {
        throw new Error('This JSON is not a transcript: it holds no room & messages. Pick the JSON download of a room.');
    }
    if (doc.$schema !== VERIFY_SCHEMA) {
        const named = typeof doc.$schema === 'string' ? '"' + doc.$schema.slice(0, 60) + '"' : 'no $schema';
        throw new Error('This JSON is not a transcript we know: it names ' + named + ', & we check "' + VERIFY_SCHEMA + '". Pick the JSON download of a room.');
    }
    if (!Array.isArray(doc.messages)) {
        throw new Error('This transcript lists no messages, so there is nothing to check. Download it again from its room.');
    }
    return doc;
}

// Every message of a parsed transcript checked against its root:
// {verdict, total, verified, failed, unchained, root, chain, lines, room,
// results: [{id, username, role, content, state, why}]}. verdict is
// "verified" (every message), "failed" (some did not verify), "partial"
// (none failed, some carry no proof) or "unproven" (no proofs at all).
async function verifyTranscript(doc) {
    const chained = doc.chain && typeof doc.chain === 'object' ? doc.chain : null;
    const root = chained && typeof chained.root === 'string' ? chained.root : null;
    const results = [];
    for (const message of doc.messages) {
        const entry = message && typeof message === 'object' ? message : {};
        let state;
        let why;
        if (!entry.chain) {
            state = 'unchained';
            why = VERIFY_WHY.unchained;
        } else if (root === null) {
            state = 'failed';
            why = 'the transcript names no root to check against';
        } else {
            const checked = await verifyMessage(entry.content, entry.chain, root);
            state = checked.ok ? 'verified' : 'failed';
            why = checked.why;
        }
        results.push({
            id: entry.id,
            username: entry.username,
            role: entry.role,
            content: entry.content,
            state,
            why,
        });
    }
    const count = (state) => results.filter((r) => r.state === state).length;
    const failed = count('failed');
    const verified = count('verified');
    const unchained = count('unchained');
    let verdict = 'verified';
    if (failed) verdict = 'failed';
    else if (!verified) verdict = 'unproven';
    else if (unchained) verdict = 'partial';
    return {
        verdict,
        total: results.length,
        verified,
        failed,
        unchained,
        root,
        chain: chained && chained.chain,
        uptoSeq: chained && chained.uptoSeq,
        lines: chained && chained.lines,
        room: doc.room && typeof doc.room === 'object' ? doc.room : null,
        results,
    };
}

async function verifyTranscriptText(text) {
    return verifyTranscript(parseTranscript(text));
}

function verdictText(report) {
    const messages = (n) => n + (n === 1 ? ' message' : ' messages');
    if (report.verdict === 'verified') {
        return 'Every message verified: ' + messages(report.total) + ' match the root in this file.';
    }
    if (report.verdict === 'failed') {
        return messages(report.failed) + ' of ' + report.total + ' failed. This file was changed after it was downloaded, or it is damaged.';
    }
    if (report.verdict === 'partial') {
        return messages(report.verified) + ' verified; ' + report.unchained + ' carry no proof (written before our rooms kept chains), so nothing here speaks for them.';
    }
    return 'This transcript carries no proofs, so there is nothing to check: its room kept no chain when these messages were written.';
}

if (typeof module !== 'undefined' && module.exports) {
    module.exports = {
        VERIFY_WHY,
        fromHex,
        hashCombine,
        hashLeaf,
        parseTranscript,
        proofRoot,
        sha256Text,
        splitChainedLine,
        toHex,
        verdictText,
        verifyMessage,
        verifyTranscript,
        verifyTranscriptText,
    };
}

// ---------------------------------------------------------------------
// Our page (templates/verify.html)
// ---------------------------------------------------------------------

if (typeof document !== 'undefined') {
    document.addEventListener('DOMContentLoaded', () => {
        const drop = document.getElementById('verify-drop');
        if (!drop) return;
        const input = document.getElementById('verify-file');
        const verdict = document.getElementById('verify-verdict');
        const facts = document.getElementById('verify-facts');
        const list = document.getElementById('verify-results');
        const STATE_WORDS = {verified: 'Verified', failed: 'Failed', unchained: 'No proof'};

        const element = (tag, className, text) => {
            const node = document.createElement(tag);
            if (className) node.className = className;
            if (text !== undefined) node.textContent = text;
            return node;
        };

        const say = (text, kind) => {
            verdict.textContent = text;
            verdict.className = 'alert verify-verdict' + (kind ? ' alert-' + kind : '');
            verdict.dataset.verdict = kind || 'none';
        };

        const clear = () => {
            facts.hidden = true;
            facts.replaceChildren();
            list.hidden = true;
            list.replaceChildren();
        };

        const fact = (name, value) => {
            if (value === null || value === undefined || value === '') return;
            facts.append(element('dt', '', name), element('dd', '', String(value)));
        };

        const show = (report, fileName) => {
            const kind = {verified: 'success', failed: 'error'}[report.verdict] || '';
            say(verdictText(report), kind);
            verdict.dataset.verdict = report.verdict;
            fact('File', fileName);
            if (report.room) fact('Room', report.room.title || report.room.name);
            fact('Chain', report.chain);
            fact('Root in this file', report.root);
            if (report.uptoSeq !== null && report.uptoSeq !== undefined) {
                fact('Covers', 'events 0 to ' + report.uptoSeq + ' of its chain');
            }
            fact('Our copy of its chain', report.lines);
            facts.hidden = !facts.children.length;
            report.results.forEach((result, index) => {
                const item = element('li', 'verify-item is-' + result.state);
                item.dataset.state = result.state;
                const head = element('div', 'verify-item-head');
                head.append(
                    element('span', 'verify-item-who', (index + 1) + '. ' + (result.username || 'someone')),
                    element('span', 'badge verify-badge', STATE_WORDS[result.state])
                );
                const words = String(result.content === null || result.content === undefined ? '' : result.content);
                item.append(head, element('p', 'verify-item-words', words.length > 160 ? words.slice(0, 160) + '…' : words));
                if (result.state !== 'verified') {
                    item.append(element('p', 'verify-item-why', result.state === 'failed' ? 'Failed: ' + result.why + '.' : 'Not chained: this message has no event or proof in this file.'));
                }
                list.append(item);
            });
            list.hidden = !report.results.length;
        };

        const check = async (file) => {
            if (!file) return;
            clear();
            say('Checking ' + file.name + ' in this browser…');
            try {
                show(await verifyTranscriptText(await file.text()), file.name);
            } catch (error) {
                clear();
                say(error && error.message ? error.message : 'This file could not be read. Try picking it again.', 'error');
            }
        };

        document.getElementById('verify-pick').addEventListener('click', () => input.click());
        input.addEventListener('change', () => {
            check(input.files[0]);
            input.value = '';
        });
        // A file dropped anywhere on our page is checked, never opened by
        // the browser in place of it.
        document.addEventListener('dragover', (event) => {
            event.preventDefault();
            drop.dataset.over = 'true';
        });
        document.addEventListener('dragleave', (event) => {
            if (!event.relatedTarget) delete drop.dataset.over;
        });
        document.addEventListener('drop', (event) => {
            event.preventDefault();
            delete drop.dataset.over;
            check(event.dataTransfer && event.dataTransfer.files[0]);
        });
    });
}
