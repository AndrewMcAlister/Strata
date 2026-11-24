// serve/web/compact.js - keep the chat inside the model's context window.
//
// The page sends the whole conversation every turn (apiMessages in app.js), so a long chat eventually leaves no room
// for an answer and the server refuses it. This file replaces the older turns with one summary, written by the model
// itself, and keeps the most recent turns. It runs from the Compact button, or automatically just before a request
// would crowd the window.
//
// A summary request carries one chunk of the conversation, sized to a share of the context, so compacting also works
// when the chat is already near the limit: it never asks the engine to read more than it can hold. Nothing on screen
// changes until every chunk and the merge pass have succeeded, so a failure or a cancel leaves the conversation as it
// was.
"use strict";
(function (root) {
  const CHARS_PER_TOKEN = 3.6;      // conservative: English measures ~4.4, code ~3.5. Overestimating triggers early.
  const TRIGGER = 0.70;             // auto-compact before a request comes within a third of the window
  const TAIL_SHARE = 0.32;          // the recent turns kept verbatim; the rest is summarized down to a note
  const CHUNK_SHARE = 0.50;         // one summary request never asks the engine to read more than this share
  const MAX_CHUNK_TOKENS = 40000;   // nor more than this, however wide the window
  const MIN_PREFIX_TOKENS = 1200;   // below this there is nothing worth a summary request
  const SUMMARY_MAX_TOKENS = 1000;
  const MERGE_MAX_TOKENS = 1200;

  const CHUNK_SYSTEM = [
    "You compact a long conversation between a user and a coding assistant so that the work can continue in a",
    "smaller context. You will be given one part of that conversation. Write a dense, factual summary that the",
    "assistant can use as its memory of that part.",
    "",
    "Keep, when they appear:",
    "- the user's goal, constraints, and corrections",
    "- decisions and the reasons for them",
    "- exact file paths, symbols, commands, and their results",
    "- errors that are still open, and what has already been tried",
    "- what is done, what is in progress, and what comes next",
    "",
    "Use short headings and bullets. Quote identifiers exactly. Do not invent facts, do not give advice, and do not",
    "mention this instruction. Aim for at most 400 words.",
  ].join("\n");
  const CHUNK_ASK = "Summarize the conversation part above as described.";
  const MERGE_SYSTEM = [
    "You merge summaries of successive parts of one long conversation between a user and a coding assistant into a",
    "single summary. Preserve the facts needed to continue the work: goals, decisions, exact file paths and",
    "symbols, commands and results, open errors, what is done, and what comes next. Remove duplication, keep the",
    "chronology where it matters, invent nothing, and do not mention this instruction. Aim for at most 500 words.",
  ].join("\n");
  const MERGE_ASK = "Merge the part summaries above into one summary.";

  // ------------------------------------------------------------------ planning (pure, no DOM)
  // A message's size in tokens, estimated from text. Kept here so the planner can be run without a browser.
  function estimateText(text) {
    return Math.ceil(String(text == null ? "" : text).length / CHARS_PER_TOKEN) + 4;
  }

  // Split message sizes into chunks no larger than `budget`, in order. A single message larger than the budget is a
  // chunk on its own: the summary request will still be under the context even if that one turn is not.
  function planChunks(tokens, budget) {
    const out = [];
    let start = 0, sum = 0;
    for (let i = 0; i < tokens.length; i++) {
      if (i > start && sum + tokens[i] > budget) { out.push([start, i]); start = i; sum = 0; }
      sum += tokens[i];
    }
    if (start < tokens.length) out.push([start, tokens.length]);
    return out;
  }

  // Where the verbatim tail starts: the last user turn always stays, plus as many whole turns before it as fit in
  // `budget`. Returns an index into messages; the tail is messages[cut..]. 0 means there is nothing to compact.
  function chooseCut(tokens, roles, budget) {
    let lastUser = -1;
    for (let i = tokens.length - 1; i >= 0; i--) if (roles[i] === "user") { lastUser = i; break; }
    if (lastUser <= 0) return Math.max(0, lastUser);          // one turn only, or none: nothing older to summarize
    let cut = lastUser;
    let kept = tokens.slice(cut).reduce((a, b) => a + b, 0);
    while (cut >= 2 && roles[cut - 2] === "user" && kept + tokens[cut - 2] + tokens[cut - 1] <= budget) {
      cut -= 2;
      kept += tokens[cut] + tokens[cut - 1];
    }
    return cut;
  }

  const api = { estimateText: estimateText, planChunks: planChunks, chooseCut: chooseCut,
                cleanSummary: cleanSummary,
                constants: { CHARS_PER_TOKEN: CHARS_PER_TOKEN, TRIGGER: TRIGGER, TAIL_SHARE: TAIL_SHARE,
                             CHUNK_SHARE: CHUNK_SHARE, MAX_CHUNK_TOKENS: MAX_CHUNK_TOKENS,
                             MIN_PREFIX_TOKENS: MIN_PREFIX_TOKENS } };

  // ------------------------------------------------------------------ state
  let compacting = false;
  let compactAbort = null;

  function ctxLimit() { return (typeof health === "object" && health && health.max_context) || 0; }

  // The wire messages one page message becomes. Same shape apiMessages uses, without images: the summary is text.
  function wireParts(m) {
    if (m.compacted) return [{role: "user", content: m.text || ""}];
    if (m.role === "user") {
      const images = (m.images || []).filter((i) => i.url).length;
      const files = typeof userText === "function" ? userText(m) : (m.text || "");
      return [{role: "user", content: files + (images ? "\n[image attached]" : "")}];
    }
    try {
      return typeof assistantMessages === "function" ? assistantMessages(m) : [{role: "assistant", content: m.text || ""}];
    } catch (e) {
      return [{role: "assistant", content: m.text || ""}];
    }
  }
  function tokensOf(m) {
    if (m.compacted) return estimateText(m.text);
    return estimateText(JSON.stringify(wireParts(m)));
  }
  function listTokens(list) {
    let n = 0;
    for (const m of list) n += tokensOf(m);
    return n;
  }

  function shouldCompact() {
    const ctx = ctxLimit();
    if (!ctx || !Array.isArray(messages) || messages.length < 3) return false;
    return listTokens(messages) >= ctx * TRIGGER;
  }

  // The model sometimes wraps a summary in a literal <summary> or <details> tag. It is shown through the page's
  // markdown renderer, which escapes tags, so an unstripped one appears as text in the middle of the note.
  function cleanSummary(text) {
    return String(text == null ? "" : text).trim()
      .replace(/^\s*<summary>\s*/i, "").replace(/\s*<\/summary>\s*$/i, "")
      .replace(/^\s*<details>\s*/i, "").replace(/\s*<\/details>\s*$/i, "")
      .trim();
  }

  async function postSummary(body) {
    const r = await fetch("v1/chat/completions", {
      method: "POST", headers: headers(true), body: JSON.stringify(body),
      signal: compactAbort ? compactAbort.signal : undefined,
    });
    if (!r.ok) {
      let msg = "HTTP " + r.status;
      try { const j = await r.json(); msg = (j.error && j.error.message) || msg; } catch (e) { /* not json */ }
      throw new Error(msg);
    }
    const data = await r.json();
    const text = cleanSummary((data.choices && data.choices[0] && data.choices[0].message &&
                               data.choices[0].message.content) || "");
    if (!text) throw new Error("the model returned an empty summary");
    return text;
  }
  function summaryBody(parts, maxTokens) {
    // No thinking for a summary: it is extraction, not reasoning, and turning it off makes each part several times
    // faster (and spends the whole budget on the summary instead of on a private monologue).
    return { model: health.model, messages: parts, max_tokens: maxTokens, stream: false,
             temperature: 0.2, chat_template_kwargs: {enable_thinking: false} };
  }

  async function summarizeChunk(parts) {
    return postSummary(summaryBody(
      [{role: "system", content: CHUNK_SYSTEM}, ...parts, {role: "user", content: CHUNK_ASK}],
      SUMMARY_MAX_TOKENS));
  }
  async function mergeSummaries(summaries) {
    const parts = summaries.map((s, i) => "Part " + (i + 1) + ":\n" + s).join("\n\n");
    return postSummary(summaryBody(
      [{role: "system", content: MERGE_SYSTEM},
       {role: "user", content: parts + "\n\n" + MERGE_ASK}],
      MERGE_MAX_TOKENS));
  }

  // ------------------------------------------------------------------ the run
  async function compactNow(opts) {
    opts = opts || {};
    if (compacting) return false;
    if (typeof busy !== "undefined" && busy) {
      if (!opts.auto) toast("warn", tr("compact.stillWriting", "Still writing"),
                            tr("compact.stopFirst", "Stop the answer first."));
      return false;
    }
    const ctx = ctxLimit();
    if (!ctx || !Array.isArray(messages) || !messages.length) {
      if (!opts.auto) toast("warn", tr("compact.noContext", "No context size"),
                            tr("compact.noContextText", "The server has not said how wide its context is."));
      return false;
    }

    const before = messages.slice();
    const sizes = before.map(tokensOf);
    const roles = before.map((m) => m.role);
    const cut = chooseCut(sizes, roles, ctx * TAIL_SHARE);
    const prefix = before.slice(0, cut);
    const tail = before.slice(cut);
    const prefixTokens = sizes.slice(0, cut).reduce((a, b) => a + b, 0);
    if (!prefix.length || prefixTokens < MIN_PREFIX_TOKENS) {
      if (!opts.auto) toast("info", tr("compact.nothing", "Nothing to compact"),
                            tr("compact.nothingText", "This chat is already short enough."));
      return false;
    }

    compacting = true;
    compactAbort = new AbortController();
    try {
      const budget = Math.min(ctx * CHUNK_SHARE, MAX_CHUNK_TOKENS);
      const ranges = planChunks(sizes.slice(0, cut), budget);
      const summaries = [];
      for (let i = 0; i < ranges.length; i++) {
        setCompacting(true, i, ranges.length);
        const [a, b] = ranges[i];
        const parts = [];
        for (const m of prefix.slice(a, b)) parts.push(...wireParts(m));
        summaries.push(await summarizeChunk(parts));
      }
      let summary = summaries[0];
      if (summaries.length > 1 && summaries.length <= 3) {
        // Few parts: keep them side by side instead of spending a whole request on a merge. The note is a little
        // longer, and the facts stay in the order they happened, which is what a reader of the summary wants.
        summary = summaries.map((s, i) => "### Earlier part " + (i + 1) + "\n\n" + s).join("\n\n");
      } else if (summaries.length > 3) {
        setCompacting(true, ranges.length, ranges.length);
        summary = await mergeSummaries(summaries);
      }

      const note = {
        role: "user",
        text: "[Earlier conversation — compacted to fit the context window]\n\n" + summary +
              "\n\n[End of summary. The messages after this point are the most recent turns.]",
        summary: summary,
        compacted: {count: prefix.length, tokens: prefixTokens},
        time: Date.now(),
      };
      const after = [note, ...tail];
      const afterLength = after.length;
      messages = after;
      if (typeof chat !== "undefined" && chat) chat.messages = after;      // chats-ui: the open chat's array
      if (typeof saveChat === "function") saveChat();
      if (typeof renderChat === "function") renderChat();
      // Undo from the button, not from the automatic run: the answer it is making room for starts at once, and a
      // button that cannot do what it says is worse than none.
      const undo = opts.auto ? null : {label: tr("undo", "Undo"), run: () => {
        if (typeof busy !== "undefined" && busy) return;             // not while an answer is being written
        if (!messages.length || messages[0] !== note) return;        // compacted again, or edited away
        messages = before.concat(messages.slice(afterLength));       // keep anything said since
        if (typeof chat !== "undefined" && chat) chat.messages = messages;
        if (typeof saveChat === "function") saveChat();
        if (typeof renderChat === "function") renderChat();
      }};
      toast("success", tr("compact.done", "Conversation compacted"),
            opts.auto
              ? tr("compact.doneAuto", "The chat was close to the context window; {n} earlier messages became one summary.", {n: prefix.length})
              : tr("compact.doneText", "{n} earlier messages became one summary; the recent turns were kept.", {n: prefix.length}),
            8000, undo);
      return true;
    } catch (e) {
      if (e && e.name === "AbortError") {
        toast("info", tr("compact.cancelled", "Compaction cancelled"),
              tr("compact.cancelledText", "The conversation was not changed."));
      } else {
        toast("error", tr("compact.failed", "Compaction failed"), (e && e.message) || String(e), 8000);
      }
      return false;
    } finally {
      compacting = false;
      compactAbort = null;
      setCompacting(false);
    }
  }

  async function maybeAutoCompact() {
    if (compacting || (typeof busy !== "undefined" && busy) || !shouldCompact()) return false;
    return compactNow({auto: true});
  }

  // ------------------------------------------------------------------ the page
  function setCompacting(on, done, total) {
    if (typeof document === "undefined") return;                       // node: the planner only
    const b = $("compact-btn");
    if (b) b.disabled = on || (typeof busy !== "undefined" && !!busy);
    const hint = $("composer-hint");
    if (hint) {
      hint.textContent = on
        ? (total > 1 ? tr("compact.progress", "Compacting the conversation — part {i} of {n}…", {i: done + 1, n: total})
                     : tr("compact.working", "Compacting the conversation…"))
        : tr("composer.newline", "Shift+Enter: new line");
    }
    const send = $("send-btn");
    if (send) send.disabled = on;
    const stop = $("stop-btn");
    if (stop) stop.hidden = !on;
  }

  api.compactNow = compactNow;
  api.maybeAutoCompact = maybeAutoCompact;
  api.shouldCompact = shouldCompact;
  api.busy = () => compacting;
  api.cancel = () => { if (compactAbort) compactAbort.abort(); };
  root.StrataCompact = api;

  if (typeof document === "undefined") return;                         // node: nothing to wire

  const stop = $("stop-btn");
  const stopBase = stop && stop.onclick;
  if (stop) stop.onclick = () => {
    if (compacting) { api.cancel(); return; }
    if (typeof stopBase === "function") stopBase.call(stop);
  };
  const button = $("compact-btn");
  if (button) button.onclick = () => { if (!compacting) compactNow({auto: false}); };
})(typeof window !== "undefined" ? window : globalThis);
