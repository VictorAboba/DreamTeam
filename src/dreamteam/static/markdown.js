"use strict";
// Markdown of the board UI (docs/SPEC.md §18.4): the texts that agents, external members and the user write
// (messages, task descriptions and results, posts, the goal, role and MCP descriptions, .md files) are shown
// formatted. The input is untrusted (LLM output, MCP members), so the renderer is safe by construction: every
// character of the input is escaped, only a fixed set of tags and attributes is emitted (raw HTML is shown as
// text, links only to http:, https: and mailto:, images become links), and every scan is linear (no regex that
// backtracks, nesting is capped) because a text can be 100 KB.
//
// Blocks: ATX headings, paragraphs (a single newline is a line break: agents write that way), ``` / ~~~ fences
// (an unclosed one runs to the end; highlighted by highlight.js when they name a known language), blockquotes, lists (nested by indentation, GFM task items), GFM pipe tables,
// rules. Inline: code spans, **strong** / __strong__, *em* / _em_ (no emphasis inside words with "_"), ~~strike~~,
// [links](url), <autolinks>, bare http(s) URLs, backslash escapes. LaTeX ($..$, $$..$$, \(..\), \[..\]) is shown as
// written, like a code span without the code look. A text without Markdown looks as written: paragraph lines keep
// their indentation and extra blank lines stay. `decorate` (optional) gets the escaped HTML of every run of plain
// text outside code, math and links: the board adds its task links (#12) and @mentions there.

const Markdown = (() => {
  const MAX_DEPTH = 12;       // nesting of blockquotes and lists: deeper markers stay text
  const MAX_BRACKETS = 64;    // "[" waiting for their "]": more stay text
  const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ESC[c]);
  const ASCII_PUNCT = /[!-/:-@[-`{-~]/;
  const PUNCT = /[\p{P}\p{S}]/u;
  const SPACE = /\s/;
  const QUOTE = /^ {0,3}>/;
  const QUOTE_STRIP = /^ {0,3}> ?/;
  const AUTOLINK = /<([A-Za-z][A-Za-z0-9+.-]{1,31}:[^\s<>]*)>/y;
  // no control characters: safeHref rejects them, and a rejected match would be scanned again from the next "h"
  const BARE_URL = /https?:\/\/[^\s<>[\]`\u0000-\u001f\u007f]+/iy;
  const URL_BEFORE = /[\s*_~("'[]/;
  const URL_TRAIL = "?!.,:;*_~'\"";

  let decorate = null;   // the current render's text hook
  // highlight.js colours fences that name their language (a page global before this script, a module in Node);
  // without it the code is plain
  const HL = typeof Highlight !== "undefined" ? Highlight
    : typeof require === "function" ? (() => { try { return require("./highlight.js"); } catch (e) { return null; } })() : null;

  /** The href of a link target, or null: only http(s):// and mailto: (any case), no control characters. */
  function safeHref(url) {
    const u = String(url ?? "").trim();
    if (!/^(?:https?:\/\/|mailto:)/i.test(u) || /[\u0000-\u001f\u007f]/.test(u)) return null;
    return u.replace(/ /g, "%20");
  }
  const anchor = (href) => `<a href="${esc(href)}" target="_blank" rel="noopener noreferrer">`;

  /* ------------------------------------------------------------ blocks */
  function expandTabs(line) {   // the leading tabs as spaces (tab stops of 4)
    if (!/^ *\t/.test(line)) return line;
    let i = 0, col = 0;
    for (; i < line.length && (line[i] === " " || line[i] === "\t"); i++) col = line[i] === "\t" ? col + 4 - (col % 4) : col + 1;
    return " ".repeat(col) + line.slice(i);
  }
  function indentOf(line) { let i = 0; while (line.charCodeAt(i) === 32) i++; return i; }
  function isBlank(line) { return !/\S/.test(line); }
  function stripIndent(line, n) { let i = 0; while (i < n && line.charCodeAt(i) === 32) i++; return line.slice(i); }

  function fenceOpen(line) {
    const ind = indentOf(line);
    const ch = line[ind];
    if (ind > 3 || (ch !== "`" && ch !== "~")) return null;
    let n = ind;
    while (line[n] === ch) n++;
    if (n - ind < 3) return null;
    const info = line.slice(n).trim();
    if (ch === "`" && info.includes("`")) return null;
    return { ch, len: n - ind, ind, lang: info.split(/\s/)[0] };
  }
  function fenceClose(line, f) {
    const ind = indentOf(line);
    if (ind > 3) return false;
    let n = ind;
    while (line[n] === f.ch) n++;
    return n - ind >= f.len && isBlank(line.slice(n));
  }
  function heading(line) {
    const ind = indentOf(line);
    if (ind > 3) return null;
    let n = ind;
    while (line[n] === "#") n++;
    const level = n - ind;
    if (level < 1 || level > 6 || (n < line.length && line[n] !== " " && line[n] !== "\t")) return null;
    let text = line.slice(n).trim();
    let e = text.length;   // a closing run of "#" (after a space, or the whole text) is not part of it
    while (e > 0 && text[e - 1] === "#") e--;
    if (e === 0) text = "";
    else if (e < text.length && (text[e - 1] === " " || text[e - 1] === "\t")) text = text.slice(0, e).trim();
    return { level, text };
  }
  function isHr(line) {
    if (indentOf(line) > 3) return false;
    const s = line.replace(/[ \t]+/g, "");
    return s.length >= 3 && (s[0] === "-" || s[0] === "*" || s[0] === "_") && s === s[0].repeat(s.length);
  }
  /** A list item marker: {ordered, start, ind, w (the indent of its continuation lines), content}. */
  function listItem(line) {
    const ind = indentOf(line);
    let n = ind, ordered = false, start = 1;
    const c = line[n];
    if (c === "-" || c === "*" || c === "+") n++;
    else {
      let d = n;
      while (d - n < 10 && line.charCodeAt(d) >= 48 && line.charCodeAt(d) <= 57) d++;
      if (d === n || d - n > 9 || (line[d] !== "." && line[d] !== ")")) return null;
      ordered = true;
      start = Number(line.slice(n, d));
      n = d + 1;
    }
    if (n >= line.length || (line[n] !== " " && line[n] !== "\t")) return null;   // "-" alone stays text
    let sp = n;
    while (line[sp] === " " || line[sp] === "\t") sp++;
    const wide = sp - n > 4 || sp >= line.length;
    return { ordered, start, ind, w: wide ? n + 1 : sp, content: wide ? line.slice(n + 1) : line.slice(sp), empty: sp >= line.length };
  }
  /** The cells of a table row; "\|" is a "|" inside a cell. */
  function splitRow(line) {
    let s = line.trim();
    if (s.startsWith("|")) s = s.slice(1);
    if (s.endsWith("|") && !s.endsWith("\\|")) s = s.slice(0, -1);
    const cells = [];
    let cur = "", from = 0;
    for (let i = 0; i < s.length; i++) {
      if (s[i] === "\\" && s[i + 1] === "|") { cur += s.slice(from, i) + "|"; from = i + 2; i++; }
      else if (s[i] === "|") { cells.push((cur + s.slice(from, i)).trim()); cur = ""; from = i + 1; }
    }
    cells.push((cur + s.slice(from)).trim());
    return cells;
  }
  /** The alignments of a table delimiter row ("" / "l" / "c" / "r"), or null when the line is not one. */
  function tableAlign(line) {
    if (!line.includes("|") || !line.includes("-")) return null;
    const out = [];
    for (const c of splitRow(line)) {
      if (!/^:?-+:?$/.test(c)) return null;
      out.push(c.endsWith(":") ? (c.startsWith(":") ? "c" : "r") : c.startsWith(":") ? "l" : "");
    }
    return out;
  }
  function tableAt(lines, i) {
    if (!lines[i].includes("|") || i + 1 >= lines.length) return null;
    const align = tableAlign(lines[i + 1]);
    if (!align) return null;
    const head = splitRow(lines[i]);
    return head.length === align.length ? { head, align } : null;
  }
  /** Whether line i starts a block that ends the paragraph before it. */
  function interrupts(lines, i) {
    const line = lines[i];
    if (fenceOpen(line) || heading(line) || isHr(line) || QUOTE.test(line)) return true;
    const li = listItem(line);
    if (li && !li.empty && (!li.ordered || li.start === 1)) return true;
    return !!tableAt(lines, i);
  }

  // The paragraphs of a list item are marked with P_OPEN / P_CLOSE until the list knows whether it is loose (<p>) or
  // tight (bare text). "\u0000" never reaches here from the input: render() replaces it.
  const P_OPEN = "\u0000(", P_CLOSE = "\u0000)";

  /**
   * HTML of block lines. `item`: the content of a list item (paragraphs marked, see P_OPEN); `info.gap` is set when
   * blank lines separate two of these blocks (the item makes its list loose).
   */
  function blocks(lines, depth, item, info) {
    let out = "";
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      if (isBlank(line)) {
        let k = i;
        while (k < lines.length && isBlank(lines[k])) k++;
        if (out && k < lines.length) {   // between two blocks: extra blank lines stay visible, as in the plain text
          if (info) info.gap = true;
          out += "<br>".repeat(k - i - 1);
        }
        i = k;
        continue;
      }
      const f = fenceOpen(line);
      if (f) {
        const body = [];
        for (i++; i < lines.length && !fenceClose(lines[i], f); i++) body.push(stripIndent(lines[i], f.ind));
        i++;   // the closing fence (or the end: an unclosed fence runs to it)
        const code = body.join("\n");
        out += (f.lang ? `<div class="md-lang">${esc(f.lang)}</div>` : "")
          + `<pre><code>${HL && f.lang ? HL.render(code, HL.language(f.lang)) : esc(code)}</code></pre>`;
        continue;
      }
      const h = heading(line);
      if (h) { out += `<h${h.level}>${inline(h.text)}</h${h.level}>`; i++; continue; }
      if (isHr(line)) { out += "<hr>"; i++; continue; }
      if (depth < MAX_DEPTH && QUOTE.test(line)) {
        const inner = [];
        for (; i < lines.length && QUOTE.test(lines[i]); i++) inner.push(lines[i].replace(QUOTE_STRIP, ""));
        out += `<blockquote>${blocks(inner, depth + 1, false)}</blockquote>`;
        continue;
      }
      const li = depth < MAX_DEPTH ? listItem(line) : null;
      if (li) { const r = list(lines, i, depth, li.ordered); out += r.html; i = r.next; continue; }
      const table = tableAt(lines, i);
      if (table) { const r = tableHtml(lines, i, table); out += r.html; i = r.next; continue; }
      // the lines keep their indentation (and .md p keeps runs of spaces): a plain text looks as it is written
      const para = [line.trimEnd()];
      for (i++; i < lines.length && !isBlank(lines[i]) && !interrupts(lines, i); i++) para.push(lines[i].trimEnd());
      const html = inline(para.join("\n"));
      out += item ? P_OPEN + html + P_CLOSE : `<p>${html}</p>`;
    }
    return out;
  }

  function list(lines, i, depth, ordered) {
    const items = [];
    let cur = null, blank = false, loose = false, blankFrom = i;
    while (i < lines.length) {
      const line = lines[i];
      if (isBlank(line)) {
        if (!blank) blankFrom = i;
        if (cur) cur.lines.push("");
        blank = true; i++;
        continue;
      }
      const ind = indentOf(line);
      const li = listItem(line);
      // indented to the item's text, or a list of the other kind under it: the item goes on (a nested block;
      // whether a blank line before it makes the list loose is up to the item's blocks, see info.gap)
      if (cur && (ind >= cur.w || (li && li.ordered !== ordered && ind > cur.ind))) {
        cur.lines.push(stripIndent(line, Math.min(ind, cur.w)));
        blank = false; i++;
        continue;
      }
      if (li && li.ordered === ordered && !isHr(line)) {
        if (cur && blank) loose = true;
        cur = { ind: li.ind, w: li.w, start: li.start, lines: [li.content] };
        items.push(cur);
        blank = false; i++;
        continue;
      }
      if (cur && !blank && !interrupts(lines, i)) { cur.lines.push(line.trim()); i++; continue; }   // a lazy continuation
      break;
    }
    if (blank) i = blankFrom;   // the blank lines after the list separate it from the next block of its parent
    const tag = ordered ? "ol" : "ul";
    const start = ordered && items[0].start !== 1 ? ` start="${Number(items[0].start)}"` : "";
    const info = { gap: false };
    let html = items.map((it) => {
      const body = it.lines;
      while (body.length && body[body.length - 1] === "") body.pop();
      const task = body.length ? /^\[([ xX])\](?:[ \t]|$)/.exec(body[0]) : null;
      if (task) body[0] = body[0].slice(3).trimStart();
      const mark = task ? `<input type="checkbox" disabled${task[1] === " " ? "" : " checked"}> ` : "";
      return `<li${task ? ' class="md-task"' : ""}>${mark}${blocks(body, depth + 1, true, info)}</li>`;
    }).join("");
    // loose (CommonMark): blank lines between the items, or between the blocks of an item (not inside a nested
    // list or a code block, which are blocks of their own). The nested lists have replaced their own marks already.
    loose = loose || info.gap;
    html = html.split(P_OPEN).join(loose ? "<p>" : "").split(P_CLOSE).join(loose ? "</p>" : "");
    return { html: `<${tag}${start}>${html}</${tag}>`, next: i };
  }

  function tableHtml(lines, i, { head, align }) {
    const rows = [];
    for (i += 2; i < lines.length && lines[i].includes("|") && !isBlank(lines[i]) && !fenceOpen(lines[i])
         && !heading(lines[i]) && !QUOTE.test(lines[i]); i++) rows.push(splitRow(lines[i]));
    const cell = (tag, text, k) => `<${tag}${align[k] ? ` class="md-${align[k]}"` : ""}>${inline(text || "")}</${tag}>`;
    const body = rows.map((r) => `<tr>${align.map((_, k) => cell("td", r[k], k)).join("")}</tr>`).join("");
    return {
      html: `<div class="md-table"><table><thead><tr>${head.map((c, k) => cell("th", c, k)).join("")}</tr></thead>`
        + (body ? `<tbody>${body}</tbody>` : "") + "</table></div>",
      next: i,
    };
  }

  /* ------------------------------------------------------------ inline */
  /** The target of a link after its "]" at `p` (`(url "title")`): {url, end} or null. */
  function linkDest(src, p) {
    if (src[p] !== "(") return null;
    const n = src.length;
    let i = p + 1;
    while (i < n && SPACE.test(src[i])) i++;
    let url;
    if (src[i] === "<") {
      let j = i + 1;
      while (j < n && src[j] !== ">" && src[j] !== "<" && src[j] !== "\n") j += src[j] === "\\" ? 2 : 1;
      if (src[j] !== ">") return null;
      url = src.slice(i + 1, j);
      i = j + 1;
    } else {
      let j = i, depth = 0;
      for (; j < n; j++) {
        const ch = src[j];
        if (ch === "\\" && ASCII_PUNCT.test(src[j + 1] || "")) { j++; continue; }
        if (ch <= " ") break;
        if (ch === "(") { if (++depth > 32) return null; }
        else if (ch === ")") { if (depth === 0) break; depth--; }
      }
      if (depth) return null;
      url = src.slice(i, j);
      i = j;
    }
    url = url.replace(/\\([!-/:-@[-`{-~])/g, "$1");
    while (i < n && SPACE.test(src[i])) i++;
    if (src[i] === '"' || src[i] === "'" || src[i] === "(") {   // a title: parsed, not shown
      const open = src[i], close = open === "(" ? ")" : open;
      let j = i + 1;
      // an unescaped "(" ends a "(" title (CommonMark): the scan stops at the next one, so "[a](x (" repeated stays linear
      while (j < n && src[j] !== close) {
        if (open === "(" && src[j] === "(") return null;
        j += src[j] === "\\" ? 2 : 1;
      }
      if (j >= n) return null;
      for (i = j + 1; i < n && SPACE.test(src[i]);) i++;
    }
    return src[i] === ")" ? { url, end: i + 1 } : null;
  }

  /** A bare URL without the punctuation that ends a sentence (and an unbalanced ")"). */
  function urlLength(url) {
    let opens = 0, closes = 0;
    for (let k = 0; k < url.length; k++) { if (url[k] === "(") opens++; else if (url[k] === ")") closes++; }
    let e = url.length;
    for (;;) {
      const last = url[e - 1];
      if (URL_TRAIL.includes(last)) e--;
      else if (last === ")" && closes > opens) { e--; closes--; }
      else return e;
    }
  }

  /** CommonMark's "process emphasis" over the delimiters delims[bottom..]; they are then removed from the stack. */
  function processEmphasis(delims, bottom) {
    const list = delims.slice(bottom);
    for (let k = 0; k < list.length; k++) { list[k].prev = list[k - 1] || null; list[k].next = list[k + 1] || null; }
    const unlink = (d) => { if (d.prev) d.prev.next = d.next; if (d.next) d.next.prev = d.prev; };
    const floors = {};   // no opener below this delimiter for closers of this kind (keeps it linear)
    let closer = list[0] || null;
    while (closer) {
      if (!closer.canClose) { closer = closer.next; continue; }
      const key = closer.ch + (closer.canOpen ? "1" : "0") + (closer.orig % 3);
      const floor = floors[key];
      let opener = closer.prev, found = null;
      for (; opener && opener !== floor; opener = opener.prev) {
        if (opener.ch !== closer.ch || !opener.canOpen) continue;
        const odd = (opener.canClose || closer.canOpen) && (opener.orig + closer.orig) % 3 === 0
          && !(opener.orig % 3 === 0 && closer.orig % 3 === 0);
        if (closer.ch === "~" || !odd) { found = opener; break; }
      }
      if (!found) {
        floors[key] = closer.prev;
        const next = closer.next;
        if (!closer.canOpen) unlink(closer);
        closer = next;
        continue;
      }
      const use = closer.ch === "~" || (found.n >= 2 && closer.n >= 2) ? 2 : 1;
      const tag = closer.ch === "~" ? "del" : use === 2 ? "strong" : "em";
      found.n -= use; closer.n -= use;
      found.open = `<${tag}>` + found.open;
      closer.close += `</${tag}>`;
      found.next = closer; closer.prev = found;   // the delimiters between them stay text
      if (!found.n) unlink(found);
      if (!closer.n) { const next = closer.next; unlink(closer); closer = next; }
    }
    delims.length = bottom;
  }

  function inline(src) {
    const nodes = [], delims = [], brackets = [];
    const n = src.length;
    let text = "", from = 0, ticks = null;
    const take = (to) => { text += src.slice(from, to); };
    const flush = () => { if (text) { nodes.push({ t: "text", s: text }); text = ""; } };
    // the next run of exactly `len` backticks at or after `at`: all runs are listed once, so the search stays linear
    const closingTicks = (len, at) => {
      if (!ticks) {
        ticks = new Map();
        for (let k = 0; k < n;) {
          if (src[k] !== "`") { k++; continue; }
          let e = k;
          while (src[e] === "`") e++;
          if (!ticks.has(e - k)) ticks.set(e - k, { at: 0, list: [] });
          ticks.get(e - k).list.push(k);
          k = e;
        }
      }
      const r = ticks.get(len);
      if (!r) return -1;
      while (r.at < r.list.length && r.list[r.at] < at) r.at++;
      return r.at < r.list.length ? r.list[r.at] : -1;
    };
    // The first match at or after `at` of the closer `key`, memoized: `at` only grows during the scan, so every
    // position is looked at once ("$a $b $c ..." without a closer stays linear).
    const found = {};
    const nextCloser = (key, at, find) => {
      const f = found[key];
      if (f !== undefined && (f < 0 || f >= at)) return f;
      return (found[key] = find(at));
    };
    // a "$" ("$$") that closes math: not escaped, not part of a longer run; "$" also after a non-space, not before a digit
    const dollarAt = (len) => (at) => {
      for (let k = src.indexOf("$", at); k >= 0; k = src.indexOf("$", k + 1)) {
        let e = k;
        while (src[e] === "$") e++;
        if (e - k === len && src[k - 1] !== "\\"
            && (len === 2 || (!SPACE.test(src[k - 1]) && !(src[e] >= "0" && src[e] <= "9")))) return k;
        k = e - 1;
      }
      return -1;
    };
    const math = (from, to) => ({ t: "html", s: esc(src.slice(from, to)).replace(/\n/g, "<br>") });
    let i = 0;
    while (i < n) {
      const c = src[i];
      if (c === "\\" && (src[i + 1] === "(" || src[i + 1] === "[")) {   // \( math \) and \[ math \]: shown as written
        const tok = src[i + 1] === "(" ? "\\)" : "\\]";
        const close = nextCloser(tok, i + 2, (at) => src.indexOf(tok, at));
        if (close > i + 2) {
          take(i); flush();
          nodes.push(math(i, close + 2));
          i = from = close + 2;
          continue;
        }
      }
      if (c === "$") {   // $math$ and $$math$$ (LaTeX): shown as written, no emphasis or escapes inside
        let j = i;
        while (src[j] === "$") j++;
        const len = j - i;
        const close = len > 2 || (len === 1 && (j >= n || SPACE.test(src[j]))) ? -1
          : nextCloser("$" + len, j, dollarAt(len));
        if (close < 0) { i = j; continue; }
        take(i); flush();
        nodes.push(math(i, close + len));
        i = from = close + len;
        continue;
      }
      if (c === "\\") {
        const d = src[i + 1];
        if (d === "\n") { take(i); from = ++i; continue; }   // a hard break: the newline makes it
        if (d !== undefined && ASCII_PUNCT.test(d)) { take(i); text += d; i += 2; from = i; continue; }
        i++;
        continue;
      }
      if (c === "`") {
        let j = i;
        while (src[j] === "`") j++;
        const close = closingTicks(j - i, j);
        if (close < 0) { i = j; continue; }   // no closing run: the backticks are text
        take(i); flush();
        let code = src.slice(j, close).replace(/\n/g, " ");
        if (code.length > 1 && code[0] === " " && code[code.length - 1] === " " && /[^ ]/.test(code)) code = code.slice(1, -1);
        nodes.push({ t: "html", s: `<code>${esc(code)}</code>` });
        i = from = close + (j - i);
        continue;
      }
      if (c === "\n") {
        let e = i;
        while (e > from && (src[e - 1] === " " || src[e - 1] === "\t")) e--;
        take(e); flush();
        nodes.push({ t: "html", s: "<br>" });
        from = ++i;   // the next line keeps its indentation (shown by .md p's white-space: pre-wrap)
        continue;
      }
      if (c === "*" || c === "_" || c === "~") {
        let j = i;
        while (src[j] === c) j++;
        if (c === "~" && j - i !== 2) { i = j; continue; }   // only "~~" strikes ("~5 min" stays text)
        take(i); flush();
        const before = i > 0 ? src[i - 1] : " ", after = j < n ? src[j] : " ";
        const bSpace = SPACE.test(before), aSpace = SPACE.test(after);
        const bPunct = PUNCT.test(before), aPunct = PUNCT.test(after);
        const left = !aSpace && (!aPunct || bSpace || bPunct);
        const right = !bSpace && (!bPunct || aSpace || aPunct);
        // "_" never opens or closes inside a word: snake_case_names stay as they are
        const canOpen = c === "_" ? left && (!right || bPunct) : left;
        const canClose = c === "_" ? right && (!left || aPunct) : right;
        const node = { t: "delim", ch: c, n: j - i, orig: j - i, canOpen, canClose, open: "", close: "" };
        nodes.push(node);
        if (canOpen || canClose) delims.push(node);
        i = from = j;
        continue;
      }
      if (c === "[" || (c === "!" && src[i + 1] === "[")) {
        const len = c === "!" ? 2 : 1;
        take(i); flush();
        const node = { t: "text", s: src.slice(i, i + len) };
        nodes.push(node);
        if (brackets.length < MAX_BRACKETS) {
          brackets.push({ node, at: nodes.length - 1, image: len === 2, active: true, bottom: delims.length, textAt: i + len });
        }
        i = from = i + len;
        continue;
      }
      if (c === "]") {
        const op = brackets.length ? brackets.pop() : null;
        const dest = op && op.active ? linkDest(src, i + 1) : null;
        const href = dest ? safeHref(dest.url) : null;
        if (!href) { i++; continue; }   // not a link (or a link to another scheme): the text stays as written
        take(i); flush();
        processEmphasis(delims, op.bottom);
        if (op.image) {   // no images: the alt text links to the picture
          for (let k = op.at + 1; k < nodes.length; k++) nodes[k].hidden = true;
          op.node.t = "html";
          op.node.alt = src.slice(op.textAt, i) || dest.url;
          op.node.s = `${anchor(href)}${esc(op.node.alt)}</a>`;
        } else {
          // an image in the link's text ([![badge](b.svg)](ci)) is its alt text: an <a> never holds another <a>
          for (let k = op.at + 1; k < nodes.length; k++) {
            nodes[k].inLink = true;
            if (nodes[k].alt !== undefined) nodes[k].s = esc(nodes[k].alt);
          }
          op.node.t = "html";
          op.node.s = anchor(href);
          nodes.push({ t: "html", s: "</a>" });
          for (const b of brackets) if (!b.image) b.active = false;   // no links inside links
        }
        i = from = dest.end;
        continue;
      }
      if (c === "<") {
        AUTOLINK.lastIndex = i;
        const m = AUTOLINK.exec(src);
        const href = m ? safeHref(m[1]) : null;
        if (href) {
          take(i); flush();
          nodes.push({ t: "url", href, s: m[1] });
          i = from = i + m[0].length;
          continue;
        }
        i++;
        continue;
      }
      if ((c === "h" || c === "H") && (i === 0 || URL_BEFORE.test(src[i - 1]))) {
        BARE_URL.lastIndex = i;
        const m = BARE_URL.exec(src);
        if (m) {
          const url = m[0].slice(0, urlLength(m[0]));
          const href = safeHref(url);
          if (href && url.length > url.indexOf("//") + 2) {
            take(i); flush();
            nodes.push({ t: "url", href, s: url });
            i = from = i + url.length;
            continue;
          }
        }
      }
      i++;
    }
    take(n); flush();
    processEmphasis(delims, 0);
    // render: runs of plain text (with the literal delimiters) go through `decorate`, except inside links
    let out = "", buf = "", bufLink = false;
    const emit = () => { if (buf) { out += bufLink || !decorate ? esc(buf) : decorate(esc(buf)); buf = ""; } };
    for (const nd of nodes) {
      if (nd.hidden) continue;
      if (!!nd.inLink !== bufLink) { emit(); bufLink = !!nd.inLink; }
      if (nd.t === "text") buf += nd.s;
      else if (nd.t === "delim") {
        if (nd.close) { emit(); out += nd.close; }
        buf += nd.ch.repeat(nd.n);
        if (nd.open) { emit(); out += nd.open; }
      } else if (nd.t === "url") {
        if (nd.inLink) buf += nd.s;
        else { emit(); out += `${anchor(nd.href)}${esc(nd.s)}</a>`; }
      } else { emit(); out += nd.s; }
    }
    emit();
    return out;
  }

  /** HTML of a Markdown text (block content, no wrapper). opts.decorate(escapedHtml) → html marks up plain text. */
  function render(text, opts) {
    const src = String(text ?? "").replace(/\r\n?/g, "\n").replace(/\u0000/g, "�");
    decorate = (opts && opts.decorate) || null;
    try {
      return blocks(src.split("\n").map(expandTabs), 0, false);
    } finally {
      decorate = null;
    }
  }

  return { render, safeHref };
})();

if (typeof module !== "undefined" && module.exports) module.exports = Markdown;
