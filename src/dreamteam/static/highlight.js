"use strict";
// Syntax highlighting of the board UI (docs/SPEC.md §18.4): files in «Файлы» (and the source of a .md file) and the
// fenced code blocks of Markdown that name their language. Files are written by agents, so the output is safe by
// construction: every character of the input is escaped, the only markup is <span class="hl-…">, and the text
// content of the result is the input itself (selecting and copying the code copies the original).
//
// One pass per language, no backtracking: at each position the rules registered for its first character are tried
// in order, and a word or a plain character is taken when none matches. A construct that may be unterminated (a
// string, a block comment) runs to the end of its line or of the text, and a scan that can fail (a closing bracket on
// the line) remembers where it failed so it is not repeated: the time is linear in the size of the text. A text
// longer than MAX_CHARS is shown plain.
//
// Classes: comment, string, number, literal (true, null…), keyword, builtin, type, title (the name a def/class/fn
// defines), meta (decorators, annotations, preprocessor, attributes), variable ($x in shells), regexp, tag, attr, key
// (JSON/TOML/YAML/INI keys), section (headings, [sections]), strong, em, add, del, hunk (diffs).

const Highlight = (() => {
  const MAX_CHARS = 300000;   // ~40 ms in Node for a cap-sized source file (~85 ms for one span per 2 chars); above: plain
  const MAX_NEST = 2;         // embedded languages: <script> in HTML, a fence in a .md source
  const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
  const esc = (s) => String(s).replace(/[&<>"']/g, (ch) => ESC[ch]);
  const words = (s) => new Set(s ? s.split(/\s+/).filter(Boolean) : []);

  /* ------------------------------------------------------------ engine */
  /** Closes the plain run before `from` and writes s[from, to) as a span of `cls`. */
  function emit(c, cls, from, to) {
    if (to <= from) return;
    if (from > c.plain) c.out += esc(c.s.slice(c.plain, from));
    c.out += `<span class="hl-${cls}">${esc(c.s.slice(from, to))}</span>`;
    c.plain = to;
    if (cls !== "comment") { c.last = cls; c.expect = false; }
  }
  /** Writes ready HTML for s[from, to) (an embedded language). */
  function put(c, from, to, html) {
    if (from > c.plain) c.out += esc(c.s.slice(c.plain, from));
    c.out += html;
    c.plain = to;
  }
  function lineEnd(s, i) { const e = s.indexOf("\n", i); return e < 0 ? s.length : e; }
  function bol(s, i) { return i === 0 || s[i - 1] === "\n"; }
  /** Whether only spaces and tabs are between the start of the line and i. */
  function lineStart(s, i) {
    let j = i - 1;
    while (j >= 0 && (s[j] === " " || s[j] === "\t")) j--;
    return j < 0 || s[j] === "\n";
  }
  function skipSpaces(s, j) { while (s[j] === " " || s[j] === "\t") j++; return j; }
  /** The position of `needle` on the rest of the line, or -1. A failure is remembered under `key` up to the end of
   * the line: a later search from further on the same line cannot succeed, so it is not made again. */
  function findOnLine(c, key, from, needle) {
    if (c.skip[key] >= from) return -1;
    const s = c.s, n = c.n, ch = needle[0];
    let j = from;
    for (; j < n && s[j] !== "\n"; j++) if (s[j] === ch && (needle.length === 1 || s.startsWith(needle, j))) return j;
    c.skip[key] = j;
    return -1;
  }
  /** The end of a quoted text whose body starts at j: after the closing quote, or at the end of the line (of the
   * text when `ml`). `escCh` escapes the next character; `doubled`: a doubled quote is a quote. */
  function scanQuoted(s, j, q, escCh, ml, doubled) {
    const n = s.length;
    for (; j < n; j++) {
      const ch = s[j];
      if (ch === escCh) { j++; continue; }
      if (ch === q) { if (doubled && s[j + 1] === q) { j++; continue; } return j + 1; }
      if (ch === "\n" && !ml) return j;
    }
    return n;
  }
  function scanTriple(s, j, q3, escCh) {
    const n = s.length;
    for (; j < n; j++) {
      if (s[j] === escCh) { j++; continue; }
      if (s[j] === q3[0] && s.startsWith(q3, j)) return j + 3;
    }
    return n;
  }

  const LANGS = {};
  const WORD = /[A-Za-z_$À-￿][\w$À-￿]*/y;
  /**
   * A language: word lists (kw keyword, lit literal, ty type, bi builtin; `defs` are keywords whose next word is the
   * defined name), `ci` case-insensitive words, `word` the regex of a word, `classify(c, w, i, end)` its own word
   * classes (undefined: the lists decide), and `rules`: [first characters (null: any), (c, i) => end or -1].
   */
  function define(id, d) {
    const L = {
      id, ci: !!d.ci, word: d.word || WORD, classify: d.classify || null, member: d.member !== false,
      kw: words(d.kw), lit: words(d.lit), ty: words(d.ty), bi: words(d.bi), defs: words(d.defs),
      table: [], other: [],
    };
    for (let k = 0; k < 128; k++) L.table.push([]);
    for (const [first, fn] of d.rules || []) {
      if (first === null) { L.table.forEach((list) => list.push(fn)); L.other.push(fn); }
      else for (const ch of first) L.table[ch.charCodeAt(0)].push(fn);
    }
    LANGS[id] = L;
    return L;
  }

  function word(c, i) {
    const L = c.L, s = c.s;
    L.word.lastIndex = i;
    const m = L.word.exec(s);
    if (!m) {   // a plain character (a run of spaces at once)
      const ch = s[i];
      if (ch === " " || ch === "\t") return skipSpaces(s, i + 1);
      if (ch !== "\n" && ch !== "\r") { c.last = ch; c.expect = false; }
      return i + 1;
    }
    const w = m[0], end = i + w.length;
    const key = L.ci ? w.toLowerCase() : w;
    let cls = L.classify ? L.classify(c, w, i, end) : undefined;
    if (cls === undefined) {
      // a member (obj.default, re.match) is a name, not a keyword
      if (L.member && s[i - 1] === "." && s[i - 2] !== ".") cls = null;
      else cls = L.kw.has(key) ? "keyword" : L.lit.has(key) ? "literal" : L.ty.has(key) ? "type" : L.bi.has(key) ? "builtin" : null;
    }
    if (c.expect && cls !== "keyword") cls = "title";
    if (cls) emit(c, cls, i, end); else { c.last = "word"; c.expect = false; }
    if (cls === "keyword" && L.defs.has(key)) c.expect = true;
    return end;
  }

  function run(id, src, depth) {
    const L = LANGS[id];
    const c = { s: src, n: src.length, out: "", plain: 0, L, depth, last: "", expect: false, skip: {}, tpl: [], bd: 0 };
    const n = c.n, table = L.table, other = L.other;
    let i = 0;
    outer: while (i < n) {
      const code = src.charCodeAt(i);
      const rules = code < 128 ? table[code] : other;
      for (let r = 0; r < rules.length; r++) {
        const e = rules[r](c, i);
        if (e > i) { i = e; continue outer; }
      }
      i = word(c, i);
    }
    if (c.plain < n) c.out += esc(src.slice(c.plain));
    return c.out;
  }

  /* ------------------------------------------------------------ rule factories */
  const lineComment = (prefix, test) => [prefix[0], (c, i) => {
    if (!c.s.startsWith(prefix, i) || (test && !test(c.s, i))) return -1;
    const e = lineEnd(c.s, i);
    emit(c, "comment", i, e);
    return e;
  }];
  const blockComment = (open, close, cls = "comment") => [open[0], (c, i) => {
    if (!c.s.startsWith(open, i)) return -1;
    const k = c.s.indexOf(close, i + open.length);
    const e = k < 0 ? c.n : k + close.length;
    emit(c, cls, i, e);
    return e;
  }];
  const quoted = (q, escCh = "\\", ml = false, doubled = false, cls = "string") => [q, (c, i) => {
    const e = scanQuoted(c.s, i + 1, q, escCh, ml, doubled);
    emit(c, cls, i, e);
    return e;
  }];
  const triple = (q3, escCh = "\\") => [q3[0], (c, i) => {
    if (!c.s.startsWith(q3, i)) return -1;
    const e = scanTriple(c.s, i + 3, q3, escCh);
    emit(c, "string", i, e);
    return e;
  }];
  const NUM = /(?:0[xX][\da-fA-F_]+|0[bBoO][\d_]+|(?:\d[\d_]*(?:\.(?![.A-Za-z_])[\d_]*)?|\.\d[\d_]*)(?:[eE][+-]?\d[\d_]*)?)[\w]*/y;
  const number = [".0123456789", (c, i) => {
    const s = c.s;
    if (s[i] === "." && !(s[i + 1] >= "0" && s[i + 1] <= "9")) return -1;
    NUM.lastIndex = i;
    const m = NUM.exec(s);
    if (!m) return -1;
    emit(c, "number", i, i + m[0].length);
    return i + m[0].length;
  }];
  /** @decorator / @Annotation: not after a name or a bracket (a @ b is an operator). */
  const decorator = (re = /@[A-Za-z_][\w.]*/y) => ["@", (c, i) => {
    const p = c.s[i - 1];
    if (p && /[\w)\]]/.test(p)) return -1;
    re.lastIndex = i;
    const m = re.exec(c.s);
    if (!m) return -1;
    emit(c, "meta", i, i + m[0].length);
    return i + m[0].length;
  }];
  /** A preprocessor line (#include, #region…) with its "\" continuations. */
  const preprocessor = ["#", (c, i) => {
    const s = c.s;
    if (!lineStart(s, i)) return -1;
    let e = lineEnd(s, i);
    while (e < c.n && (s[e - 1] === "\\" || (s[e - 1] === "\r" && s[e - 2] === "\\"))) e = lineEnd(s, e + 1);
    emit(c, "meta", i, e);
    return e;
  }];
  const CHAR = /'(?:\\(?:x[\da-fA-F]{1,4}|u\{?[\da-fA-F]{1,6}\}?|U[\da-fA-F]{8}|[0-7]{1,3}|[^\n])|[^\\'\n])'/y;
  const charLit = (lifetimes) => ["'", (c, i) => {
    CHAR.lastIndex = i;
    let m = CHAR.exec(c.s);
    if (m) { emit(c, "string", i, i + m[0].length); return i + m[0].length; }
    if (!lifetimes) return -1;
    const re = /'[A-Za-z_]\w*/y;   // a Rust lifetime
    re.lastIndex = i;
    m = re.exec(c.s);
    if (!m) return -1;
    emit(c, "meta", i, i + m[0].length);
    return i + m[0].length;
  }];
  /** "…" with variables inside ($x in shells and PowerShell): string pieces around variable spans. */
  function interp(c, i, q, escCh, varEnd, doubled) {
    const s = c.s, n = c.n;
    let start = i;
    for (let j = i + 1; j < n; j++) {
      const ch = s[j];
      if (ch === escCh) { j++; continue; }
      if (ch === q) {
        if (doubled && s[j + 1] === q) { j++; continue; }
        emit(c, "string", start, j + 1);
        return j + 1;
      }
      if (ch === "$") {
        const e = varEnd(c, j);
        if (e > j + 1) { emit(c, "string", start, j); emit(c, "variable", j, e); start = e; j = e - 1; }
      }
    }
    emit(c, "string", start, n);
    return n;
  }
  /** At the start of a line only. */
  const atBol = (fn) => [null, (c, i) => (bol(c.s, i) ? fn(c, i) : -1)];
  /** The end of the first line at or after `from` that is `delim` (`strip`: "" as is, "\t" after tabs (<<-), " " after
   * any indentation (<<~)), or -1. The short lines are indexed once (their text → their starts), so each lookup is a
   * binary search and a text full of openers stays linear. */
  function heredocEnd(c, delim, from, strip) {
    const s = c.s, n = c.n;
    if (!c.lines) {
      c.lines = { "": new Map(), "\t": new Map(), " ": new Map() };
      for (let ls = 0; ls < n;) {
        const nl = lineEnd(s, ls);
        let le = nl, p = ls, spaces = false;
        if (le > ls && s[le - 1] === "\r") le--;
        for (; s[p] === " " || s[p] === "\t"; p++) if (s[p] === " ") spaces = true;
        if (le > p && le - p <= 64) {
          const text = s.slice(p, le);
          for (const k of p === ls ? ["", "\t", " "] : spaces ? [" "] : ["\t", " "]) {
            const list = c.lines[k].get(text);
            if (list) list.push(ls); else c.lines[k].set(text, [ls]);
          }
        }
        ls = nl + 1;
      }
    }
    const list = c.lines[strip].get(delim);
    if (!list || list[list.length - 1] < from) return -1;
    let lo = 0, hi = list.length - 1;
    while (lo < hi) { const mid = (lo + hi) >> 1; if (list[mid] < from) lo = mid + 1; else hi = mid; }
    return lineEnd(s, list[lo]);
  }
  /** A heredoc opener (<<EOF, <<-'EOF', <<~EOS): `re` captures the strip mark and the delimiter (bare or quoted). The
   * rest of its line is code; the body (up to the delimiter line) is one string, written at that line's end. */
  const heredoc = (re, stripOf) => ["<", (c, i) => {
    const s = c.s;
    if (s[i + 1] !== "<" || s[i + 2] === "<" || s[i - 1] === "<") return -1;   // not <<< (a here-string)
    re.lastIndex = i;
    const m = re.exec(s);
    if (!m) return -1;
    const nl = lineEnd(s, i);
    if (nl >= c.n) return -1;
    const hd = c.hd || (c.hd = []);
    const prev = hd.length ? hd[hd.length - 1] : null;
    const from = prev && prev.nl === nl ? prev.to + 1 : nl + 1;   // cat <<A <<B: B's body follows A's
    const to = heredocEnd(c, m[2] || m[3] || m[4], from, stripOf(m[1]));
    if (to < 0) return -1;   // no delimiter line: a shift (1 << n), not a heredoc
    hd.push({ nl, to });
    emit(c, "string", i, i + m[0].length);
    return i + m[0].length;
  }];
  const heredocBody = ["\n", (c, i) => {
    const hd = c.hd;
    if (!hd) return -1;
    while (hd.length && hd[0].nl < i) hd.shift();   // its line was swallowed (a multi-line string): dropped
    if (!hd.length || hd[0].nl !== i) return -1;
    const to = hd[hd.length - 1].to;
    hd.length = 0;
    emit(c, "string", i + 1, to);
    return Math.max(to, i + 1);
  }];

  /* ------------------------------------------------------------ C family */
  const cComments = [lineComment("//"), blockComment("/*", "*/")];
  const C_TYPES = "char double float int long short signed unsigned void bool size_t ssize_t ptrdiff_t intptr_t uintptr_t "
    + "int8_t int16_t int32_t int64_t uint8_t uint16_t uint32_t uint64_t wchar_t FILE _Bool _Complex";
  const C_KW = "auto break case const continue default do else enum extern for goto if inline register restrict return "
    + "sizeof static struct switch typedef union volatile while _Atomic _Static_assert _Noreturn _Thread_local alignas alignof";
  define("c", {
    kw: C_KW, ty: C_TYPES, lit: "NULL true false",
    bi: "printf fprintf sprintf snprintf scanf malloc calloc realloc free memcpy memset memmove strlen strcmp strncmp strcpy strncpy fopen fclose exit assert",
    defs: "struct union enum",
    rules: [...cComments, preprocessor, quoted('"'), charLit(false), number],
  });
  define("cpp", {
    kw: C_KW + " class namespace template typename public private protected virtual override final new delete operator "
      + "friend explicit using try catch throw noexcept constexpr consteval constinit decltype mutable static_cast "
      + "dynamic_cast reinterpret_cast const_cast co_await co_return co_yield concept requires export import module",
    ty: C_TYPES + " std string string_view vector map unordered_map set unordered_set array pair tuple optional variant "
      + "unique_ptr shared_ptr weak_ptr size_type",
    lit: "NULL nullptr true false this",
    bi: "printf malloc free memcpy memset strlen cout cin cerr endl move forward make_unique make_shared",
    defs: "class struct union enum namespace",
    rules: [...cComments, preprocessor,
      ["uULR", (c, i) => {   // raw strings R"delim( … )delim": quotes and /* inside are text
        const m = /(?:u8|[uUL])?R"([^()\\\s"]{0,16})\(/y;
        m.lastIndex = i;
        const r = m.exec(c.s);
        if (!r) return -1;
        const k = c.s.indexOf(")" + r[1] + '"', i + r[0].length);
        const e = k < 0 ? c.n : k + r[1].length + 2;
        emit(c, "string", i, e);
        return e;
      }],
      quoted('"'), charLit(false), number],
  });
  define("csharp", {
    kw: "abstract as break case catch checked class const continue default delegate do else enum event explicit extern "
      + "finally fixed for foreach goto if implicit in interface internal is lock namespace new operator out override "
      + "params private protected public readonly ref return sealed sizeof stackalloc static struct switch throw try "
      + "typeof unchecked unsafe using virtual volatile while async await var yield record where when partial dynamic nameof",
    ty: "bool byte char decimal double float int long object sbyte short string uint ulong ushort void nint nuint "
      + "String List Dictionary Task IEnumerable Action Func",
    lit: "true false null this base value",
    bi: "Console Math",
    defs: "class struct interface enum record namespace",
    rules: [...cComments, preprocessor,
      ["@$", (c, i) => {   // verbatim @"…" ("" is a quote) and interpolated $"…" strings
        const m = /\$?@"|@\$"|\$"/y;
        m.lastIndex = i;
        const r = m.exec(c.s);
        if (!r) return -1;
        const verbatim = r[0].includes("@");
        const e = scanQuoted(c.s, i + r[0].length, '"', verbatim ? null : "\\", verbatim, verbatim);
        emit(c, "string", i, e);
        return e;
      }],
      triple('"""', null), quoted('"'), charLit(false), number],
  });
  define("java", {
    kw: "abstract assert break case catch class continue default do else enum extends final finally for if implements "
      + "import instanceof interface native new package private protected public return static strictfp switch "
      + "synchronized throw throws transient try volatile while var record sealed permits yield",
    ty: "boolean byte char double float int long short void String Object Integer Long Double Boolean List Map Set "
      + "ArrayList HashMap Optional",
    lit: "true false null this super",
    bi: "System Math",
    defs: "class interface enum record",
    rules: [...cComments, decorator(), triple('"""'), quoted('"'), charLit(false), number],
  });
  define("kotlin", {
    kw: "as break class continue do else for fun if in interface is object package return throw try typealias val var "
      + "when while by catch constructor finally import init where abstract companion const data enum final inline "
      + "inner internal lateinit open operator out override private protected public sealed suspend vararg",
    ty: "Int Long Short Byte Double Float Boolean Char String Unit Any Nothing List Map Set Array MutableList MutableMap",
    lit: "true false null this super it",
    bi: "println print listOf mapOf setOf mutableListOf mutableMapOf arrayOf require check error",
    defs: "fun class interface object",
    rules: [...cComments, decorator(), triple('"""', null), quoted('"'), charLit(false), number],
  });
  define("go", {
    kw: "break case chan const continue default defer else fallthrough for func go goto if import interface map package "
      + "range return select struct switch type var",
    ty: "bool byte complex64 complex128 error float32 float64 int int8 int16 int32 int64 rune string uint uint8 uint16 "
      + "uint32 uint64 uintptr any comparable",
    lit: "true false nil iota",
    bi: "append cap clear close complex copy delete imag len make max min new panic print println real recover",
    defs: "func type",
    rules: [...cComments, quoted('"'), quoted("`", null, true), charLit(false), number],
  });
  define("rust", {
    kw: "as break const continue crate dyn else enum extern fn for if impl in let loop match mod move mut pub ref return "
      + "static struct super trait type unsafe use where while async await yield",
    ty: "i8 i16 i32 i64 i128 isize u8 u16 u32 u64 u128 usize f32 f64 bool char str String Vec Option Result Box Rc Arc "
      + "HashMap HashSet BTreeMap Self",
    lit: "true false self",
    bi: "Some None Ok Err",
    defs: "fn struct enum trait type mod union",
    classify: (c, w, i, end) => (c.s[end] === "!" && c.s[end + 1] !== "=" ? "builtin" : undefined),   // a macro: name!
    rules: [...cComments,
      ["#", (c, i) => {   // attributes #[derive(…)] and #![…]
        const s = c.s;
        const open = s[i + 1] === "[" ? i + 1 : s[i + 1] === "!" && s[i + 2] === "[" ? i + 2 : -1;
        if (open < 0) return -1;
        let depth = 0, j = open;
        for (; j < c.n && s[j] !== "\n"; j++) {
          if (s[j] === "[") depth++;
          else if (s[j] === "]" && --depth === 0) { j++; break; }
        }
        emit(c, "meta", i, j);
        return j;
      }],
      ["rb", (c, i) => {   // raw strings r"…", r#"…"#, byte strings b"…", b'x'
        const m = /b?r(#*)"|b"|b'/y;
        m.lastIndex = i;
        const r = m.exec(c.s);
        if (!r) return -1;
        let e;
        if (r[0].endsWith("'")) { CHAR.lastIndex = i + 1; const ch = CHAR.exec(c.s); if (!ch) return -1; e = i + 1 + ch[0].length; }
        else if (r[0] === 'b"') e = scanQuoted(c.s, i + 2, '"', "\\", true);
        else { const k = c.s.indexOf('"' + r[1], i + r[0].length); e = k < 0 ? c.n : k + 1 + r[1].length; }
        emit(c, "string", i, e);
        return e;
      }],
      quoted('"', "\\", true), charLit(true), number],
  });

  /* ------------------------------------------------------------ JavaScript / TypeScript */
  // not after "<": "</p>" closes a JSX element
  const REGEX_AFTER = new Set(["", "keyword", "(", ",", "=", ":", "[", "!", "&", "|", "?", "{", ";", "+", "-", "*", "%", ">", "~", "^"]);
  /** A regex literal /…/flags where an operand is expected (after "=", "(", return…); else a division. */
  const regexLit = ["/", (c, i) => {
    if (!REGEX_AFTER.has(c.last) || c.skip.re >= i) return -1;
    const s = c.s;
    let j = i + 1, cls = false;
    for (; j < c.n && s[j] !== "\n"; j++) {
      const ch = s[j];
      if (ch === "\\") { j++; continue; }
      if (ch === "[") cls = true;
      else if (ch === "]") cls = false;
      else if (ch === "/" && !cls) break;
    }
    if (j >= c.n || s[j] !== "/") { c.skip.re = j; return -1; }   // no regex ends on this line
    j++;
    while (/[a-z]/.test(s[j] || "")) j++;
    emit(c, "regexp", i, j);
    return j;
  }];
  function tplPart(c, j, start) {   // a template literal from j (its span starts at `start`) up to "`" or "${"
    const s = c.s, n = c.n;
    for (; j < n; j++) {
      const ch = s[j];
      if (ch === "\\") { j++; continue; }
      if (ch === "`") { emit(c, "string", start, j + 1); return j + 1; }
      if (ch === "$" && s[j + 1] === "{" && c.tpl.length < 32) {
        emit(c, "string", start, j);
        c.tpl.push(0);   // the braces open in this ${…}: its "}" goes back to the template
        c.last = "{";
        return j + 2;
      }
    }
    emit(c, "string", start, n);
    return n;
  }
  const SQ = quoted("'")[1];
  const jsRules = [
    ...cComments,
    quoted('"'),
    // a quote right after a name cannot open a string in JS: it is an apostrophe of JSX text (<p>Don't</p>)
    ["'", (c, i) => (/[\w$]/.test(c.s[i - 1] || "") ? -1 : SQ(c, i))],
    ["`", (c, i) => tplPart(c, i + 1, i)],
    ["{}", (c, i) => {
      const t = c.tpl;
      if (!t.length) return -1;
      if (c.s[i] === "{") { t[t.length - 1]++; return -1; }
      if (t[t.length - 1] > 0) { t[t.length - 1]--; return -1; }
      t.pop();
      return tplPart(c, i + 1, i + 1);
    }],
    regexLit,
    decorator(),
    number,
  ];
  const JS_KW = "break case catch class const continue debugger default delete do else export extends finally for "
    + "function if import in instanceof let new return switch throw try typeof var void while with yield async await of "
    + "static from as";
  const JS_LIT = "true false null undefined NaN Infinity this super";
  const JS_BI = "console window document globalThis Object Array String Number Boolean Promise Map Set WeakMap WeakSet "
    + "JSON Math Date RegExp Error TypeError Symbol BigInt require module exports process";
  define("js", { kw: JS_KW, lit: JS_LIT, bi: JS_BI, defs: "function class", rules: jsRules });
  define("ts", {
    kw: JS_KW + " interface type enum implements declare readonly namespace abstract private public protected keyof "
      + "infer is satisfies",
    ty: "string number boolean any unknown never void object bigint symbol Record Partial Readonly Array Promise",
    lit: JS_LIT, bi: JS_BI, defs: "function class interface type enum namespace", rules: jsRules,
  });

  /* ------------------------------------------------------------ Python, Ruby, Lua, R, Julia, PHP */
  const PY_PREFIX = /[rRbBuUfFtT]{1,2}(?=['"])/y;
  define("python", {
    kw: "and as assert async await break class continue def del elif else except finally for from global if import in "
      + "is lambda nonlocal not or pass raise return try while with yield",
    lit: "True False None Ellipsis NotImplemented self cls __name__ __file__",
    bi: "abs all any bin bool bytearray bytes callable chr classmethod compile complex delattr dict dir divmod enumerate "
      + "eval exec filter float format frozenset getattr globals hasattr hash help hex id input int isinstance "
      + "issubclass iter len list locals map max memoryview min next object oct open ord pow print property range repr "
      + "reversed round set setattr slice sorted staticmethod str sum super tuple type vars zip __import__ "
      + "Exception BaseException ValueError TypeError KeyError IndexError AttributeError RuntimeError OSError "
      + "NotImplementedError StopIteration ImportError FileNotFoundError",
    defs: "def class",
    rules: [
      lineComment("#"),
      ["'\"rRbBuUfFtT", (c, i) => {   // prefixed, triple-quoted and plain strings
        const s = c.s;
        let j = i;
        if (s[i] !== '"' && s[i] !== "'") {
          PY_PREFIX.lastIndex = i;
          const m = PY_PREFIX.exec(s);
          if (!m) return -1;
          j = i + m[0].length;
        }
        const q = s[j];
        const e = s.startsWith(q + q + q, j) ? scanTriple(s, j + 3, q + q + q, "\\") : scanQuoted(s, j + 1, q, "\\", false);
        emit(c, "string", i, e);
        return e;
      }],
      decorator(),
      number,
    ],
  });
  define("ruby", {
    kw: "alias and begin break case class def defined? do else elsif end ensure for if in module next not or redo rescue "
      + "retry return then undef unless until when while yield require require_relative include extend attr_accessor "
      + "attr_reader attr_writer private protected public raise",
    lit: "true false nil self __FILE__ __LINE__",
    bi: "puts print p lambda proc loop",
    defs: "def class module",
    word: /[A-Za-z_][\w]*[?!]?/y,
    classify: (c, w) => (/^[A-Z]/.test(w) && !c.expect ? "type" : undefined),   // Constants
    rules: [
      atBol((c, i) => {   // =begin … =end
        if (!c.s.startsWith("=begin", i)) return -1;
        const k = c.s.indexOf("\n=end", i);
        const e = k < 0 ? c.n : lineEnd(c.s, k + 1);
        emit(c, "comment", i, e);
        return e;
      }),
      lineComment("#"),
      heredoc(/<<([~-]?)(?:'([^'\n]+)'|"([^"\n]+)"|([A-Za-z_]\w*))/y, (mark) => (mark ? " " : "")), heredocBody,
      regexLit,   // before the quotes: gsub(/'/, "") is a regex, not a string to the end of the file
      quoted('"', "\\", true), quoted("'", "\\", true), quoted("`", "\\", true),
      [":", (c, i) => {   // :symbol (not Foo::Bar, not "a ? b :c")
        const s = c.s;
        if (s[i + 1] === ":" || s[i - 1] === ":" || /\w/.test(s[i - 1] || "")) return -1;
        const m = /:[A-Za-z_]\w*[?!=]?/y;
        m.lastIndex = i;
        const r = m.exec(s);
        if (!r) return -1;
        emit(c, "literal", i, i + r[0].length);
        return i + r[0].length;
      }],
      ["@$", (c, i) => {
        const m = /@@?[A-Za-z_]\w*|\$[A-Za-z_]\w*/y;
        m.lastIndex = i;
        const r = m.exec(c.s);
        if (!r) return -1;
        emit(c, "variable", i, i + r[0].length);
        return i + r[0].length;
      }],
      number,
    ],
  });
  function longBracket(c, i) {   // Lua [[…]] / [==[…]==] from i (at "[")
    const m = /\[(=*)\[/y;
    m.lastIndex = i;
    const r = m.exec(c.s);
    if (!r) return -1;
    const k = c.s.indexOf("]" + r[1] + "]", i + r[0].length);
    return k < 0 ? c.n : k + r[1].length + 2;
  }
  define("lua", {
    kw: "and break do else elseif end for function goto if in local not or repeat return then until while",
    lit: "nil true false self",
    bi: "print pairs ipairs require type tostring tonumber setmetatable getmetatable error assert pcall xpcall select "
      + "string table math os io unpack next rawget rawset",
    defs: "function",
    rules: [
      ["-", (c, i) => {
        const s = c.s;
        if (s[i + 1] !== "-") return -1;
        let e = s[i + 2] === "[" ? longBracket(c, i + 2) : -1;
        if (e < 0) e = lineEnd(s, i);
        emit(c, "comment", i, e);
        return e;
      }],
      ["[", (c, i) => { const e = longBracket(c, i); if (e < 0) return -1; emit(c, "string", i, e); return e; }],
      quoted('"'), quoted("'"), number,
    ],
  });
  define("r", {
    kw: "if else repeat while function for in next break return",
    lit: "TRUE FALSE NULL NA NA_integer_ NA_real_ NA_character_ Inf NaN T F",
    bi: "c list library require print cat paste paste0 length data.frame matrix vector seq rep sum mean max min apply "
      + "lapply sapply names nrow ncol is.na stop warning",
    word: /[A-Za-z_.][\w.]*/y,
    member: false,
    rules: [
      lineComment("#"), quoted('"', "\\", true), quoted("'", "\\", true),
      ["%", (c, i) => {   // %in%, %>%
        const k = findOnLine(c, "%", i + 1, "%");
        if (k < 0) return -1;
        emit(c, "keyword", i, k + 1);
        return k + 1;
      }],
      number,
    ],
  });
  define("julia", {
    kw: "baremodule begin break catch const continue do else elseif end export finally for function global if import "
      + "let local macro module quote return struct try using while where mutable abstract primitive type in isa",
    lit: "true false nothing missing NaN Inf",
    ty: "Int Int8 Int16 Int32 Int64 UInt UInt8 Float16 Float32 Float64 String Bool Char Vector Matrix Array Dict Any "
      + "Nothing Tuple Symbol Real Integer Number AbstractArray",
    bi: "println print push! pop! map filter length size zeros ones collect sum maximum minimum",
    defs: "function macro module struct type",
    word: /[A-Za-z_][\w!]*/y,
    rules: [blockComment("#=", "=#"), lineComment("#"), triple('"""'), quoted('"', "\\", true), charLit(false),
      decorator(/@[A-Za-z_][\w!.]*/y), number],
  });
  define("php", {
    ci: true,
    kw: "abstract and as break case catch class clone const continue declare default do echo else elseif empty "
      + "enddeclare endfor endforeach endif endswitch endwhile extends final finally fn for foreach function global goto "
      + "if implements include include_once instanceof insteadof interface isset list match namespace new or print "
      + "private protected public readonly require require_once return static switch throw trait try unset use var "
      + "while xor yield enum",
    lit: "true false null",
    ty: "int float bool string array object callable iterable void mixed never self parent",
    defs: "function class interface trait enum",
    rules: [
      [null, (c, i) => {   // a template's HTML around <?php … ?> is markup, not PHP (an apostrophe in it is text)
        if (c.php === undefined) c.php = !c.s.includes("<?");   // a snippet without "<?php" (a fence) is all PHP
        if (c.php) return -1;
        let k = c.s.indexOf("<?", i);
        if (k < 0) k = c.n;
        if (k === i) return -1;
        if (c.depth < MAX_NEST) put(c, i, k, run("markup", c.s.slice(i, k), c.depth + 1));
        return k;
      }],
      ["<?", (c, i) => {
        const m = /<\?(?:php|=)?|\?>/y;
        m.lastIndex = i;
        const r = m.exec(c.s);
        if (!r) return -1;
        emit(c, "meta", i, i + r[0].length);
        c.php = r[0] !== "?>";
        return i + r[0].length;
      }],
      ...cComments, lineComment("#"), quoted('"', "\\", true), quoted("'", "\\", true),
      ["$", (c, i) => {
        const m = /\$[A-Za-z_]\w*/y;
        m.lastIndex = i;
        const r = m.exec(c.s);
        if (!r) return -1;
        emit(c, "variable", i, i + r[0].length);
        return i + r[0].length;
      }],
      number,
    ],
  });

  /* ------------------------------------------------------------ shells */
  const SH_SPECIAL = "0123456789@#?$!*-";
  const LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz";
  const VARNAME = /[A-Za-z_]\w*/y;
  function shVar(c, j) {   // the end of $NAME / ${…} / $1 / $? at j, or -1
    const s = c.s, x = s[j + 1];
    if (x === "{" || (x === "(" && c.L.id === "make")) {
      const k = findOnLine(c, x, j + 2, x === "{" ? "}" : ")");
      return k < 0 ? -1 : k + 1;
    }
    if (x && /[A-Za-z_]/.test(x)) { VARNAME.lastIndex = j + 1; VARNAME.exec(s); return VARNAME.lastIndex; }
    if (x && (SH_SPECIAL.includes(x) || (c.L.id === "make" && "<^+%".includes(x)))) return j + 2;
    return -1;
  }
  const shComment = lineComment("#", (s, i) => i === 0 || /[\s;&|(){}]/.test(s[i - 1]));   // not $# or a#b
  const shRules = [
    ["#", (c, i) => {   // #! on the first line
      if (i !== 0 || c.s[1] !== "!") return -1;
      const e = lineEnd(c.s, 0);
      emit(c, "meta", 0, e);
      return e;
    }],
    shComment,
    // cat <<EOF: the body is text (an apostrophe in it must not open a string to the end of the file)
    heredoc(/<<(-?)[ \t]*(?:'([^'\n]+)'|"([^"\n]+)"|\\?([A-Za-z_][\w.-]*))/y, (mark) => (mark ? "\t" : "")), heredocBody,
    quoted("'", null, true),
    ['"', (c, i) => interp(c, i, '"', "\\", shVar)],
    quoted("`", "\\", true),
    ["$", (c, i) => {
      if (c.s[i + 1] === "'") { const e = scanQuoted(c.s, i + 2, "'", "\\", true); emit(c, "string", i, e); return e; }
      const e = shVar(c, i);
      if (e < 0) return -1;
      emit(c, "variable", i, e);
      return e;
    }],
  ];
  const SH = {
    kw: "if then else elif fi case esac for select while until do done in function time coproc return break continue exit",
    lit: "true false",
    bi: "echo printf read cd pwd pushd popd export local declare typeset readonly unset shift source alias unalias set "
      + "eval exec trap test wait type command builtin let getopts mapfile umask ulimit",
    defs: "function",
    word: /[A-Za-z_][\w-]*/y,
    member: false,
    classify: (c, w, i, end) => {
      const s = c.s;
      if (s[end] === "=" && (i === 0 || /[\s;&|(]/.test(s[i - 1]))) return "variable";   // NAME=value
      if (s[end] === "(" && s[end + 1] === ")") return "title";                            // name() { … }
      return undefined;
    },
  };
  define("bash", Object.assign({}, SH, { rules: shRules }));
  const DOCKER = /(?:FROM|RUN|CMD|LABEL|MAINTAINER|EXPOSE|ENV|ADD|COPY|ENTRYPOINT|VOLUME|USER|WORKDIR|ARG|ONBUILD|STOPSIGNAL|HEALTHCHECK|SHELL)(?=[ \t]|$)/iy;
  define("dockerfile", Object.assign({}, SH, {
    rules: [
      [LETTERS, (c, i) => {   // an instruction at the start of a line
        if (!lineStart(c.s, i)) return -1;
        DOCKER.lastIndex = i;
        const m = DOCKER.exec(c.s);
        if (!m) return -1;
        emit(c, "keyword", i, i + m[0].length);
        return i + m[0].length;
      }],
      ...shRules.slice(1),
    ],
  }));
  define("make", Object.assign({}, SH, {
    kw: SH.kw + " ifeq ifneq ifdef ifndef endif include define endef export override",
    rules: [
      atBol((c, i) => {   // target: deps / VAR := value
        const s = c.s;
        if (s[i] === "\t" || s[i] === " " || s[i] === "#") return -1;
        let j = i;
        while (j < c.n && /[\w.%/$(){} -]/.test(s[j])) j++;
        let k = j;
        while (k > i && s[k - 1] === " ") k--;
        if (k === i) return -1;
        if (/^[:?+!]?=/.test(s.slice(j, j + 3))) { emit(c, "variable", i, k); return k; }
        if (s[j] === ":" && s[j + 1] !== "=") { emit(c, "title", i, k); return k; }
        return -1;
      }),
      ...shRules.slice(1),
    ],
  }));

  const PS_VAR = /\$(?:(?:env|global|script|local|private|using|variable):)?[A-Za-z_]\w*/iy;
  function psVar(c, j) {
    const s = c.s, x = s[j + 1];
    if (x === "{") { const k = findOnLine(c, "}", j + 2, "}"); return k < 0 ? -1 : k + 1; }
    PS_VAR.lastIndex = j;
    const m = PS_VAR.exec(s);
    if (m) return j + m[0].length;
    return x === "$" || x === "?" || x === "^" ? j + 2 : -1;
  }
  const PS_OPS = words("eq ne gt ge lt le like notlike match notmatch contains notcontains in notin replace split join "
    + "and or not xor band bor is isnot as f ceq cne clike cmatch imatch ireplace creplace");
  define("powershell", {
    ci: true,
    kw: "begin break catch class continue data define do dynamicparam else elseif end enum exit filter finally for "
      + "foreach from function if in param process return switch throw trap try until using var while workflow",
    defs: "function filter class enum",
    word: /[A-Za-z_]\w*(?:-[A-Za-z_]\w*)*/y,
    classify: (c, w) => (w.includes("-") ? "builtin" : undefined),   // Verb-Noun cmdlets
    rules: [
      blockComment("<#", "#>"), lineComment("#"),
      ["@", (c, i) => {   // here-strings @' … '@ and @" … "@ (the closing at the start of a line)
        const s = c.s, q = s[i + 1];
        if (q !== "'" && q !== '"') return -1;
        let j = skipSpaces(s, i + 2);
        if (s[j] === "\r") j++;
        if (j < c.n && s[j] !== "\n") return -1;
        const k = s.indexOf("\n" + q + "@", j);
        const e = k < 0 ? c.n : k + 3;
        emit(c, "string", i, e);
        return e;
      }],
      quoted("'", null, true, true),
      ['"', (c, i) => interp(c, i, '"', "`", psVar, true)],
      ["$", (c, i) => {
        const e = psVar(c, i);
        if (e < 0) return -1;
        const name = c.s.slice(i + 1, e).toLowerCase();
        emit(c, name === "true" || name === "false" || name === "null" ? "literal" : "variable", i, e);
        return e;
      }],
      ["[", (c, i) => {   // [type], [System.IO.File]
        const m = /\[[A-Za-z_][\w.]*(?:\[\])?\]/y;
        m.lastIndex = i;
        const r = m.exec(c.s);
        if (!r) return -1;
        emit(c, "type", i, i + r[0].length);
        return i + r[0].length;
      }],
      ["-", (c, i) => {   // -eq operators and -Parameter names
        const s = c.s;
        if (i > 0 && !/[\s(,|{=]/.test(s[i - 1])) return -1;
        const m = /-[A-Za-z][\w]*/y;
        m.lastIndex = i;
        const r = m.exec(s);
        if (!r) return -1;
        emit(c, PS_OPS.has(r[0].slice(1).toLowerCase()) ? "keyword" : "attr", i, i + r[0].length);
        return i + r[0].length;
      }],
      number,
    ],
  });
  define("batch", {
    ci: true,
    kw: "if else for in do goto call exit not exist defined errorlevel equ neq lss leq gtr geq setlocal endlocal shift",
    bi: "echo set cd pushd popd copy del move ren mkdir md rmdir rd type pause cls start title color dir find findstr where",
    lit: "off on nul",
    word: /[A-Za-z_][\w.-]*/y,
    member: false,
    rules: [
      ["@:rR", (c, i) => {   // REM / :: comments and :labels at the start of a line
        const s = c.s;
        if (!lineStart(s, i)) return -1;
        const p = s[i] === "@" ? i + 1 : i;
        const e = lineEnd(s, i);
        if (s.startsWith("::", p) || /^rem(?:\s|$)/i.test(s.slice(p, p + 4))) { emit(c, "comment", i, e); return e; }
        if (s[p] === ":") { emit(c, "title", i, e); return e; }
        return -1;
      }],
      ["%", (c, i) => {
        const m = /%%~?[A-Za-z]|%~[a-zA-Z]*[0-9*]|%[0-9*]|%[^%\s"=]+%/y;
        m.lastIndex = i;
        const r = m.exec(c.s);
        if (!r) return -1;
        emit(c, "variable", i, i + r[0].length);
        return i + r[0].length;
      }],
      ["!", (c, i) => {
        const m = /![A-Za-z_][\w]*!/y;
        m.lastIndex = i;
        const r = m.exec(c.s);
        if (!r) return -1;
        emit(c, "variable", i, i + r[0].length);
        return i + r[0].length;
      }],
      quoted('"', null), number,
    ],
  });

  /* ------------------------------------------------------------ data formats */
  /** A quoted key or string: a key when `sep` (":" or "=") follows it on the line. */
  const keyOrString = (q, escCh, sep, also) => [q, (c, i) => {
    const s = c.s;
    const e = scanQuoted(s, i + 1, q, escCh, false);
    const k = skipSpaces(s, e);
    emit(c, s[k] === sep || (also && s[k] === also) ? "key" : "string", i, e);
    return e;
  }];
  define("json", {
    lit: "true false null",
    rules: [...cComments, keyOrString('"', "\\", ":"), number],
  });
  define("toml", {
    lit: "true false", ty: "", bi: "inf nan",
    word: /[A-Za-z_][\w-]*/y,
    member: false,
    classify: (c, w, i, end) => {   // a bare key: followed by "=" or "." (dotted keys)
      const k = skipSpaces(c.s, end);
      return c.s[k] === "=" || (c.s[k] === "." && c.bd === 0) ? "key" : undefined;
    },
    rules: [
      lineComment("#"),
      atBol((c, i) => {   // [table] / [[array of tables]] (not a line of a multi-line array)
        const s = c.s;
        const p = skipSpaces(s, i);
        if (s[p] !== "[" || c.bd > 0) return -1;
        let j = p;
        while (j < c.n && s[j] !== "\n" && s[j] !== "#") j++;
        while (j > p && /\s/.test(s[j - 1])) j--;
        emit(c, "section", p, j);
        return j;
      }),
      ["[", (c) => { c.bd++; return -1; }],
      ["]", (c) => { if (c.bd > 0) c.bd--; return -1; }],
      triple('"""'), triple("'''", null), keyOrString('"', "\\", "=", "."), keyOrString("'", null, "=", "."),
      number,
    ],
  });
  const YAML_LIT = words("true false null yes no on off ~ True False Null TRUE FALSE NULL Yes No YES NO On Off ON OFF");
  const YAML_NUM = /^[-+]?(?:\d[\d_]*(?:\.\d*)?(?:[eE][-+]?\d+)?|0x[\da-fA-F]+|0o[0-7]+|\.\d+|\.(?:inf|nan|Inf|NaN|INF|NAN))$/;
  define("yaml", {
    rules: [
      atBol((c, i) => {   // a "key:" at the start of a line (after indentation and "- " markers), lines of a block scalar
        const s = c.s, e = lineEnd(s, i);
        let p = skipSpaces(s, i);
        if (c.yb !== undefined) {   // inside "key: |": lines indented deeper (and blank ones) are its text
          if (p >= e || s[p] === "\r" || p - i > c.yb) { if (p < e) emit(c, "string", p, e); return e > i ? e : -1; }
          c.yb = undefined;
        }
        if ((s.startsWith("---", p) || s.startsWith("...", p)) && p === i) { emit(c, "meta", p, p + 3); return p + 3; }
        while (s[p] === "-" && (s[p + 1] === " " || s[p + 1] === "\t")) p = skipSpaces(s, p + 1);
        const ch = s[p];
        if (p >= e || "#'\"[{&*!|>%@`".includes(ch)) return p > i ? p : -1;
        let j = p, key = -1;
        for (; j < e; j++) {
          if (s[j] === ":" && (j + 1 >= e || s[j + 1] === " " || s[j + 1] === "\t" || s[j + 1] === "\r")) { key = j; break; }
          if (s[j] === "#" && (s[j - 1] === " " || s[j - 1] === "\t")) break;
        }
        if (key < 0) return p > i ? p : -1;
        let k = key;
        while (k > p && (s[k - 1] === " " || s[k - 1] === "\t")) k--;
        emit(c, "key", p, k);
        // a block scalar follows: its lines are deeper than the key itself ("- run: |" → than "run", not the dash)
        if (/^:[ \t]*[|>][-+0-9]*[ \t]*(?:#.*)?\r?$/.test(s.slice(key, e))) c.yb = p - i;
        return k;
      }),
      lineComment("#", (s, i) => i === 0 || /\s/.test(s[i - 1])),
      keyOrString('"', "\\", ":"), keyOrString("'", null, ":"),
      ["&*", (c, i) => {   // &anchor, *alias
        const m = /[&*][^\s,\[\]{}]+/y;
        m.lastIndex = i;
        const r = m.exec(c.s);
        if (!r) return -1;
        emit(c, "variable", i, i + r[0].length);
        return i + r[0].length;
      }],
      ["!", (c, i) => {   // !!str, !tag
        const m = /!!?[\w\/.-]*/y;
        m.lastIndex = i;
        const r = m.exec(c.s);
        emit(c, "type", i, i + r[0].length);
        return i + r[0].length;
      }],
      [null, (c, i) => {   // a plain scalar: a number or a literal as a whole, else plain text (no colors inside)
        const s = c.s, ch = s[i];
        if (/[\s#'"\[\]{},&*!|>%@`:]/.test(ch) || (ch === "-" && /[\s]/.test(s[i + 1] || " "))) return -1;
        let j = i + 1;
        while (j < c.n && !/[\n,\[\]{}]/.test(s[j]) && !(s[j] === "#" && /\s/.test(s[j - 1]))) j++;
        let k = j;
        while (k > i && /\s/.test(s[k - 1])) k--;
        const v = s.slice(i, k);
        const cls = YAML_NUM.test(v) ? "number" : YAML_LIT.has(v) ? "literal" : null;
        if (cls) emit(c, cls, i, k);
        return k;
      }],
    ],
  });
  define("ini", {
    ci: true,
    lit: "true false yes no on off null none",
    word: /[A-Za-z_][\w.-]*/y,
    member: false,
    rules: [
      atBol((c, i) => {   // ; comments, [sections], key = value / key: value / export KEY=value
        const s = c.s, e = lineEnd(s, i);
        let p = skipSpaces(s, i);
        const ch = s[p];
        if (ch === ";" || ch === "#") { emit(c, "comment", p, e); return e; }
        if (ch === "[") { const k = findOnLine(c, "]", p, "]"); const end = k < 0 ? e : k + 1; emit(c, "section", p, end); return end; }
        if (s.startsWith("export ", p)) { emit(c, "keyword", p, p + 6); p = skipSpaces(s, p + 6); }
        let j = p;
        while (j < e && s[j] !== "=" && s[j] !== ":") j++;
        let k = j;
        while (k > p && (s[k - 1] === " " || s[k - 1] === "\t")) k--;
        if (j >= e || k === p) return p > i ? p : -1;
        emit(c, "key", p, k);
        return k;
      }),
      quoted('"'), quoted("'", null), number,
    ],
  });

  /* ------------------------------------------------------------ SQL */
  define("sql", {
    ci: true,
    kw: "select from where and or not insert into values update set delete create table drop alter add column index "
      + "view primary key foreign references unique default check constraint join inner left right outer full cross on "
      + "as group by order having limit offset union all distinct case when then else end exists in is like ilike "
      + "between asc desc with recursive returning begin commit rollback transaction if replace truncate grant revoke "
      + "database schema trigger procedure function return returns declare cast over partition window filter using "
      + "natural lateral explain analyze vacuum pragma autoincrement collate escape except intersect fetch first next "
      + "rows only do language",
    ty: "int integer bigint smallint tinyint serial bigserial decimal numeric real float double precision varchar char "
      + "text boolean bool date time timestamp timestamptz interval uuid json jsonb blob bytea",
    lit: "null true false",
    bi: "count sum avg min max coalesce nullif now length lower upper substring trim round abs concat date_trunc extract "
      + "row_number rank dense_rank lag lead string_agg array_agg",
    rules: [
      lineComment("--"), blockComment("/*", "*/"), quoted("'", null, true, true), quoted('"', null, false, true),
      ["@", (c, i) => {
        const m = /@@?[A-Za-z_]\w*/y;
        m.lastIndex = i;
        const r = m.exec(c.s);
        if (!r) return -1;
        emit(c, "variable", i, i + r[0].length);
        return i + r[0].length;
      }],
      number,
    ],
  });

  /* ------------------------------------------------------------ markup and styles */
  const TAG_NAME = /[A-Za-z][\w:.-]*/y;
  const ATTR_NAME = /[^\s"'>\/=<]+/y;
  const UNQUOTED = /[^\s>"'=<`]+/y;
  const ENTITY = /&(?:#\d{1,8}|#[xX][\da-fA-F]{1,8}|[A-Za-z][A-Za-z\d]{1,31});/y;
  function markupTag(c, i) {
    const s = c.s, n = c.n;
    const block = (open, close, cls) => {
      if (!s.startsWith(open, i)) return -1;
      const k = s.indexOf(close, i + open.length);
      const e = k < 0 ? n : k + close.length;
      emit(c, cls, i, e);
      return e;
    };
    if (s[i + 1] === "!") {
      let e = block("<!--", "-->", "comment");
      if (e < 0) e = block("<![CDATA[", "]]>", "string");
      return e >= 0 ? e : block("<!", ">", "meta");
    }
    if (s[i + 1] === "?") return block("<?", "?>", "meta");
    let j = s[i + 1] === "/" ? i + 2 : i + 1;
    const closing = j === i + 2;
    TAG_NAME.lastIndex = j;
    const m = TAG_NAME.exec(s);
    if (!m) return -1;
    emit(c, "tag", j, j + m[0].length);
    j += m[0].length;
    let self = false, value = false;
    while (j < n) {   // attributes up to ">" (an unclosed tag ends at the next "<")
      const ch = s[j];
      if (ch === " " || ch === "\t" || ch === "\n" || ch === "\r") { j++; continue; }
      if (ch === ">") { j++; break; }
      if (ch === "<") break;
      if (ch === "/" && s[j + 1] === ">") { j += 2; self = true; break; }
      if (ch === '"' || ch === "'") { const e = scanQuoted(s, j + 1, ch, null, true); emit(c, "string", j, e); j = e; value = false; continue; }
      if (ch === "=") { j++; value = true; continue; }
      const re = value ? UNQUOTED : ATTR_NAME;
      re.lastIndex = j;
      const a = re.exec(s);
      if (!a) { j++; value = false; continue; }
      emit(c, value ? "string" : "attr", j, j + a[0].length);
      j += a[0].length;
      value = false;
    }
    const name = m[0].toLowerCase();
    if (!closing && !self && (name === "script" || name === "style") && c.depth < MAX_NEST) {   // the embedded code
      const close = new RegExp("</" + name, "ig");
      close.lastIndex = j;
      const r = close.exec(s);
      const k = r ? r.index : n;
      if (k > j) put(c, j, k, run(name === "script" ? "js" : "css", s.slice(j, k), c.depth + 1));
      return k > j ? k : j;
    }
    return j;
  }
  define("markup", {
    word: /[A-Za-z0-9]+/y,
    rules: [
      ["<", markupTag],
      ["&", (c, i) => {
        ENTITY.lastIndex = i;
        const m = ENTITY.exec(c.s);
        if (!m) return -1;
        emit(c, "literal", i, i + m[0].length);
        return i + m[0].length;
      }],
    ],
  });

  // CSS state: c.bd the brace depth, c.val inside a declaration's value, c.pre inside an @rule's prelude
  const cssMatch = (re, cls) => (c, i) => {
    re.lastIndex = i;
    const m = re.exec(c.s);
    if (!m) return -1;
    emit(c, cls, i, i + m[0].length);
    return i + m[0].length;
  };
  const CSS = {
    word: /-{0,2}[A-Za-z_][\w-]*/y,
    member: false,
    classify: (c, w, i, end) => {
      const s = c.s;
      if (c.val) return s[end] === "(" ? "builtin" : null;   // var(), rgba(), calc()
      if (c.pre) return null;
      if (c.bd > 0 && (c.last === "{" || c.last === ";") && s[skipSpaces(s, end)] === ":") return "attr";   // a property
      return w.startsWith("-") ? null : "tag";   // a selector's element
    },
    rules: [
      blockComment("/*", "*/"), quoted('"'), quoted("'"),
      ["@", (c, i) => { const e = cssMatch(/@[\w-]+/y, "keyword")(c, i); if (e > 0) c.pre = true; return e; }],
      ["{", (c) => { c.bd++; c.val = false; c.pre = false; return -1; }],
      ["}", (c) => { if (c.bd > 0) c.bd--; c.val = false; return -1; }],
      [";", (c) => { c.val = false; c.pre = false; return -1; }],
      [":", (c, i) => {
        if (!c.val && c.last === "attr") { c.val = true; return -1; }
        return c.val || c.pre ? -1 : cssMatch(/::?[\w-]+/y, "meta")(c, i);   // :hover, ::before
      }],
      ["#", (c, i) => (c.val ? cssMatch(/#[\da-fA-F]{3,8}(?![\w-])/y, "number")(c, i) : c.pre ? -1 : cssMatch(/#[\w-]+/y, "title")(c, i))],
      number,
      [".", (c, i) => (c.val || c.pre ? -1 : cssMatch(/\.-?[A-Za-z_][\w-]*/y, "title")(c, i))],
      ["!", cssMatch(/!important/iy, "keyword")],
    ],
  };
  define("css", CSS);
  // SCSS / LESS: CSS with // line comments (not the // of url(http://…), which follows "(" or ":")
  define("scss", Object.assign({}, CSS, {
    rules: [lineComment("//", (s, i) => i === 0 || /[\s;{}]/.test(s[i - 1])), ...CSS.rules],
  }));

  define("latex", {
    word: /[A-Za-z0-9]+/y,
    rules: [
      lineComment("%"),
      ["\\", (c, i) => {
        const s = c.s;
        const m = /\\(?:[A-Za-z@]+\*?|[\uD800-\uDBFF][\uDC00-\uDFFF]|[^\n])?/y;   // a whole emoji, never half a pair
        m.lastIndex = i;
        const e = i + m.exec(s)[0].length;
        emit(c, "keyword", i, e);
        const cmd = s.slice(i, e);
        if ((cmd === "\\begin" || cmd === "\\end") && s[e] === "{") {   // \begin{env}
          const k = findOnLine(c, "}", e + 1, "}");
          if (k > e + 1) { emit(c, "title", e + 1, k); return k; }
        }
        return e;
      }],
      ["$", (c, i) => {   // $math$ and $$math$$
        const s = c.s, two = s[i + 1] === "$", key = two ? "$$" : "$";
        if (c.skip[key] !== undefined) return -1;
        const k = s.indexOf(key, i + key.length);
        if (k < 0) { c.skip[key] = c.n; return -1; }
        emit(c, "string", i, k + key.length);
        return k + key.length;
      }],
    ],
  });

  /* ------------------------------------------------------------ Markdown source, diffs */
  function mdLine(c, i) {   // the start of a line: fences (their code highlighted), headings, rules, quote and list markers
    const s = c.s, n = c.n, e = lineEnd(s, i);
    let p = i;
    while (p < e && p - i < 3 && s[p] === " ") p++;
    const ch = s[p];
    if ((ch === "`" || ch === "~") && s[p + 1] === ch && s[p + 2] === ch) {
      let k = p;
      while (s[k] === ch) k++;
      const len = k - p, info = s.slice(k, e).trim();
      if (!(ch === "`" && info.includes("`"))) {
        emit(c, "meta", p, e);
        let ls = e + 1, close = -1;
        while (ls < n) {   // the closing fence: as long, only spaces after it
          const le = lineEnd(s, ls);
          let q = ls;
          while (q < le && q - ls < 3 && s[q] === " ") q++;
          let r = q;
          while (s[r] === ch) r++;
          if (r - q >= len) { let t = r; while (t < le && /\s/.test(s[t])) t++; if (t === le) { close = ls; break; } }
          ls = le + 1;
        }
        const from = Math.min(e + 1, n), to = close < 0 ? n : close;
        const lang = info ? language(info.split(/\s/)[0]) : null;
        if (lang && to > from && c.depth < MAX_NEST) put(c, from, to, run(lang, s.slice(from, to), c.depth + 1));
        if (close < 0) return n;
        const ce = lineEnd(s, close);
        emit(c, "meta", close, ce);
        return ce;
      }
    }
    if (ch === "#") {
      let k = p;
      while (s[k] === "#") k++;
      if (k - p <= 6 && (k === e || /\s/.test(s[k]))) { emit(c, "section", p, e); return e; }
    }
    if (ch === "-" || ch === "*" || ch === "_") {   // a rule: three or more of one character
      let count = 0, k = p;
      for (; k < e; k++) { if (s[k] === ch) count++; else if (!/\s/.test(s[k])) break; }
      if (k === e && count >= 3) { emit(c, "keyword", p, e); return e; }
    }
    let k = p, any = false;
    for (let guard = 0; guard < 16; guard++) {   // > quotes and a list marker
      if (s[k] === ">") { emit(c, "keyword", k, k + 1); k = skipSpaces(s, k + 1); any = true; continue; }
      if ((s[k] === "-" || s[k] === "*" || s[k] === "+") && (s[k + 1] === " " || s[k + 1] === "\t")) {
        emit(c, "keyword", k, k + 1);
        k = skipSpaces(s, k + 1);
        any = true;
        break;
      }
      let d = k;
      while (d - k < 9 && s[d] >= "0" && s[d] <= "9") d++;
      if (d > k && (s[d] === "." || s[d] === ")") && (s[d + 1] === " " || s[d + 1] === "\t")) {
        emit(c, "keyword", k, d + 1);
        k = d + 1;
        any = true;
      }
      break;
    }
    return any ? k : -1;
  }
  define("markdown", {
    word: /[A-Za-z0-9]+/y,
    rules: [
      [null, (c, i) => (bol(c.s, i) ? mdLine(c, i) : -1)],
      ["\\", (c, i) => i + 2],   // \* \_ \` stay as they are
      ["`", (c, i) => {   // a code span on its line
        const s = c.s;
        let j = i;
        while (s[j] === "`") j++;
        const len = j - i;
        if (len > 16) return j;
        const k = findOnLine(c, "`" + len, j, "`".repeat(len));
        if (k < 0) return j;
        emit(c, "string", i, k + len);
        return k + len;
      }],
      ["*_", (c, i) => {   // **strong**, *em*, _em_ (not inside words)
        const s = c.s, ch = s[i];
        if (ch === "_" && /[A-Za-z0-9]/.test(s[i - 1] || "")) return -1;
        const dbl = s[i + 1] === ch;
        const from = dbl ? i + 2 : i + 1;
        if (!s[from] || /\s/.test(s[from])) return -1;
        const mark = dbl ? ch + ch : ch;
        const k = findOnLine(c, mark, from + 1, mark);
        if (k < 0) return from;
        emit(c, dbl ? "strong" : "em", i, k + mark.length);
        return k + mark.length;
      }],
      ["]", (c, i) => {   // [text](url): the url
        if (c.s[i + 1] !== "(") return -1;
        const k = findOnLine(c, ")", i + 2, ")");
        if (k < 0) return -1;
        emit(c, "attr", i + 2, k);
        return k;
      }],
      ["hH<", (c, i) => {
        if (/[A-Za-z0-9]/.test(c.s[i - 1] || "")) return -1;
        const m = c.s[i] === "<" ? /<(?:https?|mailto):[^\s<>]*>/iy : /https?:\/\/[^\s<>()[\]`]+/iy;
        m.lastIndex = i;
        const r = m.exec(c.s);
        if (!r) return -1;
        emit(c, "attr", i, i + r[0].length);
        return i + r[0].length;
      }],
    ],
  });
  define("diff", {
    rules: [[null, (c, i) => {
      const s = c.s;
      if (!bol(s, i)) return -1;
      const e = lineEnd(s, i), ch = s[i];
      let cls = null;
      if (c.dOld > 0 || c.dNew > 0) {   // inside a hunk its lines are counted: "--- x" there is a removed "-- x"
        if (ch === "-") { cls = "del"; c.dOld--; }
        else if (ch === "+") { cls = "add"; c.dNew--; }
        else if (ch === "\\") cls = "comment";
        else if (ch === " " || i === e || (ch === "\r" && e === i + 1)) { c.dOld--; c.dNew--; }   // context (or emptied)
        else c.dOld = c.dNew = 0;   // not a hunk line after all: read it as a header below
        if (c.dOld > 0 || c.dNew > 0 || cls || ch === " ") {
          if (cls) emit(c, cls, i, e);
          return e > i ? e : -1;
        }
      }
      if (s.startsWith("+++ ", i) || s.startsWith("--- ", i) || /^(?:diff |index |new file|deleted file|similarity|rename |old mode|new mode)/.test(s.slice(i, i + 13))) cls = "meta";
      else if (ch === "@") {
        const m = /@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@/y;   // a unified hunk: how many old and new lines follow
        m.lastIndex = i;
        const r = m.exec(s);
        c.dOld = r ? (r[1] === undefined ? 1 : +r[1]) : 0;
        c.dNew = r ? (r[2] === undefined ? 1 : +r[2]) : 0;
        cls = "hunk";
      } else if (ch === "+" || ch === ">") cls = "add";
      else if (ch === "-" || ch === "<") cls = "del";
      else if (ch === "\\") cls = "comment";
      if (cls) emit(c, cls, i, e);
      return e > i ? e : -1;
    }]],
  });

  /* ------------------------------------------------------------ language detection */
  const ALIASES = {
    python: ["py", "pyi", "pyw", "python3", "py3", "ipython", "gyp", "sage"],
    js: ["javascript", "jsx", "mjs", "cjs", "node", "es6"],
    ts: ["typescript", "tsx", "mts", "cts"],
    json: ["jsonc", "json5", "jsonl", "ndjson", "ipynb", "geojson", "webmanifest"],
    toml: [], yaml: ["yml"], ini: ["cfg", "conf", "env", "dotenv", "properties", "editorconfig", "gitconfig", "desktop"],
    bash: ["sh", "shell", "zsh", "ksh", "console", "shell-session", "shellsession", "terminal", "fish"],
    powershell: ["ps1", "psm1", "psd1", "ps", "pwsh", "posh"],
    batch: ["bat", "cmd", "bash-batch", "dosbatch"],
    markup: ["html", "htm", "xhtml", "xml", "svg", "xsd", "xsl", "xslt", "rss", "atom", "plist", "xaml", "csproj",
      "fsproj", "vbproj", "props", "targets", "vue", "svelte", "wsdl"],
    css: [], scss: ["less", "sass"],
    sql: ["mysql", "postgresql", "postgres", "psql", "plsql", "pgsql", "sqlite", "tsql"],
    c: ["h"], cpp: ["c++", "cc", "cxx", "hpp", "hh", "hxx", "h++", "ino", "cu", "cuh"],
    csharp: ["cs", "c#"], java: [], kotlin: ["kt", "kts"], go: ["golang"], rust: ["rs"],
    ruby: ["rb", "gemspec", "rake"], php: [], lua: [], r: ["rscript"], julia: ["jl"],
    latex: ["tex", "sty", "cls", "bib"], markdown: ["md", "mkd", "mdx"], diff: ["patch", "udiff"],
    dockerfile: ["docker", "containerfile"], make: ["makefile", "mk", "mak"],
  };
  const BY_NAME = {};
  for (const [id, list] of Object.entries(ALIASES)) for (const a of [id, ...list]) BY_NAME[a] = id;
  const FILES = {
    dockerfile: "dockerfile", containerfile: "dockerfile", makefile: "make", gnumakefile: "make",
    gemfile: "ruby", rakefile: "ruby", pipfile: "toml", ".bashrc": "bash", ".zshrc": "bash", ".profile": "bash",
    ".bash_profile": "bash", ".bash_aliases": "bash", ".zprofile": "bash", ".env": "ini", ".gitconfig": "ini",
    ".editorconfig": "ini", ".npmrc": "ini", "cmakelists.txt": null,
  };
  /** The language of a file name or path ("src/app.py", "Dockerfile") or a fence info string ("python", "sh"),
   * or null. */
  function language(name) {
    const s = String(name ?? "").trim().toLowerCase();
    if (!s) return null;
    if (!/[./\\]/.test(s)) {   // an info string ("python", "c++") or a file name without a dot ("Makefile")
      if (Object.prototype.hasOwnProperty.call(BY_NAME, s)) return BY_NAME[s];
      return Object.prototype.hasOwnProperty.call(FILES, s) ? FILES[s] : null;
    }
    const base = s.split(/[\\/]/).pop();
    if (Object.prototype.hasOwnProperty.call(FILES, base)) return FILES[base];
    if (base.startsWith(".env.")) return "ini";
    if (base.startsWith("dockerfile.") || base.endsWith(".dockerfile")) return "dockerfile";
    const dot = base.lastIndexOf(".");
    if (dot < 0) return null;
    const ext = base.slice(dot + 1);
    return Object.prototype.hasOwnProperty.call(BY_NAME, ext) ? BY_NAME[ext] : null;
  }

  /** The HTML of `text` highlighted as `lang` (an id of language()); plain escaped text when the language is unknown
   * or the text is longer than MAX_CHARS. */
  function render(text, lang) {
    const src = String(text ?? "");
    if (!lang || !Object.prototype.hasOwnProperty.call(LANGS, lang) || src.length > MAX_CHARS) return esc(src);
    try {
      return run(lang, src, 0);
    } catch (e) {   // never lose the text over a highlighting bug
      return esc(src);
    }
  }

  return { language, render, MAX_CHARS, languages: Object.keys(LANGS) };
})();

if (typeof module !== "undefined" && module.exports) module.exports = Highlight;
