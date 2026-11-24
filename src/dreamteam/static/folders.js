"use strict";
// The folder parts both pages share (docs/SPEC.md §18.4 "Folders on both pages"): the check line under a folder field
// (POST /api/folders/check: what choosing the folder would do), the folder browser (POST /api/folders/list: the hub's
// folders, a layer above the page's modal) and the «Сменить папку…» dialog of a session. Each page passes its own
// t(), esc(), request function (a launcher route at the root, never /s/{sid}/...), toast and modal functions
// (Folders.setup); the texts (fo_*, fp_*, cf_*, cd_*) live in both pages' dictionaries. The hub decides every rule:
// nothing here judges a folder, it only words the hub's answers.

const Folders = (() => {
  const CHECK_DELAY = 400;      // ms after the last keystroke before a typed path is checked
  const CHECKING_DELAY = 300;   // ms before «Проверяю папку…» shows: no flicker for a quick answer
  const DEFAULT = "\u0000default";   // the key of a check of the session's own folder ({"default": true})
  // the cd_* keys the check line picks (console.cd_preview's texts and the git lines), by name: t("cd_" + key)
  const LINE_KEYS = ["text_missing", "text_empty", "text_files", "text_fresh", "text_lost", "text_back", "text_running",
                     "git_disabled", "git_none"];
  // the keys of console.cd_result_lines (resultLines), shown as t("cd_" + key)
  const RESULT_KEYS = ["unchanged", "moved", "now", "adopted", "created", "git_moved", "git_existing",
                       "git_initialized", "git_disabled", "git_none", "mcp_moved", "old_kept", "old_mcp_committed",
                       "old_mcp_uncommitted", "old_missing", "start_error"];
  const GIT_RESULTS = ["moved", "existing", "initialized", "disabled", "none"];
  const WINDOWS_BAD = /[\\:*?"<>|\x00-\x1f]/;
  const DEVICE_NAME = /^(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?$/i;

  let P = null;   // the page: {t, esc, request(path, body) → data (throws an Error with .status), toast, ...}
  let B = null;   // the open folder browser
  let moving = false;   // the change dialog's request runs: the dialog cannot close

  /** The page's functions: {t, esc, request, toast(text, isErr), openModal(html), closeModal(force)}. */
  function setup(page) { P = page; }

  /** A path typed with one pair of matching quotes around it (Explorer's "Copy as path") without them, trimmed: the
   * twin of console.unquote. */
  function unquote(text) {
    const s = String(text ?? "").trim();
    if (s.length >= 2 && s[0] === s[s.length - 1] && (s[0] === "\"" || s[0] === "'")) return s.slice(1, -1).trim();
    return s;
  }

  // ---- the words of an answer --------------------------------------------------------------------------------
  /** The check line for an answer of POST /api/folders/check: {cls: "ok" | "warn" | "err", text}. opts: {session:
   * a move of a session (else a new session's folder), old: the session's current folder}. */
  function lineWords(t, answer, opts = {}) {
    if (!answer || typeof answer !== "object") return null;
    if (answer.error) return { cls: "err", text: String(answer.error) };
    if (answer.use === "source") return { cls: "ok", text: t(answer.state === "zip" ? "fo_src_zip" : "fo_src_folder") };
    const words = [];
    const git = answer.git;
    const files = answer.state === "files";
    const gitLine = () => {
      if (git === "initialized" && files) words.push(t("fo_git_init"));
      else if (git === "disabled") words.push(t("cd_git_disabled"));
      else if (git === "none") words.push(t("cd_git_none"));
    };
    const unchanged = answer.mode === "unchanged";
    if (!opts.session) {
      if (answer.state === "missing") words.push(t("fo_new_missing"));
      else if (answer.state === "empty") words.push(t("fo_new_empty"));
      else if (git === "existing" && answer.branch) words.push(t("fo_new_files", { branch: answer.branch }));
      else { words.push(t("fo_new_files_plain")); gitLine(); }
    } else if (unchanged) {
      words.push(t("fo_unchanged"));
    } else {
      const key = answer.fresh ? "text_fresh" : answer.lost ? "text_lost" : answer.back ? "text_back"
        : answer.state === "missing" ? "text_missing" : answer.state === "empty" ? "text_empty" : "text_files";
      words.push(t("cd_" + key, { old: opts.old || "" }));
      if (key === "text_files" && answer.branch) words.push(t("fo_branch", { branch: answer.branch }));
      gitLine();
    }
    const mcp = Array.isArray(answer.mcp) ? answer.mcp : [];
    if (!unchanged && mcp.length) words.push(t("fo_mcp", { names: mcp.map(String).join(", ") }));
    if (!unchanged && answer.running) words.push(t("cd_text_running"));
    const api = !unchanged && answer.api_folder && typeof answer.api_folder === "object" ? answer.api_folder : null;
    if (api) words.push(t("cf_api", { root: api.root }));
    const warn = answer.mode === "adopted" || (opts.session && !unchanged && answer.running) || !!api;
    return { cls: warn ? "warn" : "ok", text: words.join(" ") };
  }

  /** A workspace change (`result` of POST /api/sessions/{sid}/workspace, or of a create with `workspace`) as [key,
   * fields] lines: the twin of console.cd_result_lines; the "start_error" line is an error. */
  function resultLines(result, title) {
    const r = result && typeof result === "object" ? result : {};
    const truthy = (x) => (Array.isArray(x) ? x.length > 0 : !!x);
    const mode = r.mode, to = r.to ?? null;
    if (mode === "unchanged") return [["unchanged", { title, path: to }]];
    const head = mode === "adopted" || mode === "created" ? mode : r.old_kept || r.old_missing ? "now" : "moved";
    const lines = [[head, { title, path: to }]];
    if (GIT_RESULTS.includes(r.git)) lines.push(["git_" + r.git, {}]);
    if (truthy(r.mcp_moved)) lines.push(["mcp_moved", { names: r.mcp_moved.map(String).join(", ") }]);
    const changed = r.old_changed && typeof r.old_changed === "object" && !Array.isArray(r.old_changed)
      ? r.old_changed : null;
    if (changed && Array.isArray(changed.paths) && changed.paths.length) {
      const paths = changed.paths.map(String).join(", ");
      const path = (r.old_kept || r.from) ?? null;
      const commit = String(changed.commit).slice(0, 8);
      if (truthy(changed.commit)) lines.push(["old_mcp_committed", { path, paths, commit }]);
      else lines.push(["old_mcp_uncommitted", { path, paths, error: changed.error || "?" }]);
    } else if (r.old_kept) {
      lines.push(["old_kept", { path: r.old_kept }]);
    }
    if (r.old_missing && mode !== "created") lines.push(["old_missing", { path: r.from ?? null }]);
    if (r.start_error) lines.push(["start_error", { error: r.start_error }]);
    return lines;
  }
  /** resultLines in the page's words: [{text, err}], one toast each. */
  function resultTexts(t, result, title) {
    return resultLines(result, title).map(([key, fields]) => ({ text: t("cd_" + key, fields),
                                                               err: key === "start_error" }));
  }

  /** A new folder's name the browser takes: just a name, and one a folder of the hub's computer can have (`sep` the
   * hub's os.sep: "\\" on Windows, whose names refuse more). */
  function newNameOk(name, sep) {
    const n = String(name ?? "");
    if (!n.trim() || n === "." || n === ".." || n.includes("/") || n.includes("\u0000")) return false;
    if (sep === "\\" && (WINDOWS_BAD.test(n) || /[. ]$/.test(n) || DEVICE_NAME.test(n))) return false;
    return true;
  }

  /** The label of a place (a root of the listing). */
  function rootLabel(t, root) {
    const name = String((root && root.name) || (root && root.path) || "");
    switch (root && root.kind) {
      case "home": return t("fp_root_home");
      case "drive": return t("fp_root_drive", { name });
      case "system": return t("fp_root_system");
      case "api": return t("fp_root_api", { name });
      case "dreamteam": return t("fp_root_dreamteam");
      default: return name;
    }
  }

  /** The breadcrumb of `path` inside its root: [{path, label|null (the root: its place's label)}]. */
  function crumbs(path, root, sep) {
    if (!path) return [];
    const win = sep === "\\";
    const fold = (s) => (win ? s.toLowerCase() : s);
    const isSep = (c) => c === sep || (win && c === "/");
    // inside the root: its prefix up to a separator (root /home/u never holds /home/user2)
    const inside = !!root && fold(path).startsWith(fold(root))
      && (path.length === root.length || isSep(root[root.length - 1]) || isSep(path[root.length]));
    if (!inside) return [{ path, label: path }];
    const out = [{ path: root, label: null }];
    const parts = path.slice(root.length).split(win ? /[\\/]+/ : /\/+/).filter(Boolean);
    let acc = root;
    for (const part of parts) {
      acc = acc.endsWith(sep) || (win && acc.endsWith("/")) ? acc + part : acc + sep + part;
      out.push({ path: acc, label: part });
    }
    return out;
  }

  // ---- the check line ----------------------------------------------------------------------------------------
  /** The check line of a folder field: `input` the field, `line` its line (role="status"). opts: {use ("workspace" |
   * "source"), session (a move of that session), isDefault() (its own folder is checked: {"default": true}), old()
   * (the session's current folder, the {old} of the texts), onChange(result) (after every change of what is shown or
   * of ready())}. → {run(now), ready(), result, schedule()}; `result` {key, answer | null, failed: text | null,
   * words: {cls, text}} for the field's current value, else null. */
  function checker(input, line, opts = {}) {
    const page = P;
    const t = page.t;
    let timer = null, checking = null, seq = 0, pending = false, shown = null;
    const key = () => (opts.isDefault && opts.isDefault() ? DEFAULT : unquote(input.value));
    // the dialog was closed: no request is sent for its field any more, and a late answer changes nothing
    const gone = () => input.isConnected === false;
    const changed = () => { if (opts.onChange) opts.onChange(shown && shown.key === key() ? shown : null); };
    const setLine = (words) => {
      if (!line) return;
      line.textContent = words ? words.text : "";
      line.className = "fo-line" + (words && words.cls ? " " + words.cls : "");
      if (input.setAttribute) {
        if (words && words.cls === "err") input.setAttribute("aria-invalid", "true");
        else if (input.removeAttribute) input.removeAttribute("aria-invalid");
      }
    };
    if (line && line.id && input.setAttribute) input.setAttribute("aria-describedby", line.id);
    function stopChecking() { clearTimeout(checking); checking = null; }
    /** A keystroke, a paste, a change: check again once the typing stops. */
    function schedule() {
      // a `change` that changed nothing (the field left for the dialog's button) keeps the answer shown; a check
      // that could not run is tried again
      if (!timer && !pending && shown && shown.failed === null && shown.key === key()) return;
      clearTimeout(timer);
      timer = null;
      if (!key()) { seq++; pending = false; stopChecking(); shown = null; setLine(null); changed(); return; }
      timer = setTimeout(() => { timer = null; if (!gone()) run(true); }, CHECK_DELAY);
      changed();
    }
    async function run(now = true) {
      if (!now) { schedule(); return null; }
      clearTimeout(timer);
      timer = null;
      if (gone()) { seq++; pending = false; stopChecking(); return null; }
      const k = key();
      const my = ++seq;
      stopChecking();
      if (!k) { pending = false; shown = null; setLine(null); changed(); return null; }
      pending = true;
      checking = setTimeout(() => {
        if (my === seq && pending && !gone()) setLine({ cls: "", text: t("fo_checking") });
      }, CHECKING_DELAY);
      changed();
      const body = { use: opts.use || "workspace" };
      if (opts.session) body.session = opts.session;
      if (k === DEFAULT) body.default = true; else body.path = k;
      let answer = null, failed = null;
      try {
        answer = await page.request("/api/folders/check", body);
      } catch (e) {
        const status = Number(e && e.status) || 0;
        const text = (e && e.message) || String(e);
        // the call itself refused (a bad body, an unknown session, main): said as the hub says it; a check that
        // could not run (no connection, 429, 5xx) blocks nothing: the hub decides at the create or the move
        if (status >= 400 && status < 500 && status !== 429) {
          answer = { use: body.use, path: body.path, error: text, status };
        } else {
          failed = text;
        }
      }
      if (my !== seq || gone()) return null;   // a newer request is out, the field was emptied, the dialog closed
      if (key() !== k) return null;  // the field changed meanwhile: its own timer checks it
      pending = false;
      stopChecking();
      if (answer && k !== DEFAULT && answer.path !== k) { changed(); return null; }   // not this value's answer
      // (the hub's texts end with a full stop, and so does the {error} of the text: one is enough)
      const words = failed !== null
        ? { cls: "warn", text: t("fo_check_failed", { error: failed.replace(/[.\s]+$/, "") }) }
        : lineWords(t, answer, { session: !!opts.session, old: opts.old ? opts.old() : "" });
      shown = { key: k, answer, failed, words };
      setLine(words);
      changed();
      return shown;
    }
    /** True only while the shown result is the answer for the field's current value. */
    function ready() { return !timer && !pending && !!shown && shown.key === key(); }
    if (input.addEventListener) {
      input.addEventListener("input", schedule);
      input.addEventListener("change", schedule);
    }
    return {
      run, ready, schedule,
      get result() { return shown && shown.key === key() ? shown : null; },
    };
  }

  // ---- the folder browser ------------------------------------------------------------------------------------
  function isOpen() { return !!B; }

  /** A DOM element: attrs (null / false skipped; `text` its text, `class`, `hidden`, `disabled`, `fp` the browser's
   * own data of the element) and children (strings are text). */
  function h(tag, attrs, ...children) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "text") el.textContent = String(v);
      else if (k === "class") el.className = v;
      else if (k === "hidden" || k === "disabled") el[k] = true;
      else if (k === "fp") el._fp = v;
      else el.setAttribute(k, v === true ? "" : String(v));
    }
    for (const c of children.flat()) {
      if (c === null || c === undefined || c === false) continue;
      el.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    }
    return el;
  }
  /** The browser's element of an event target (the nearest one with browser data), inside the layer. */
  function fpOf(el) {
    while (el && el !== (B && B.box)) { if (el._fp) return el; el = el.parentNode; }
    return null;
  }
  function visible(el) {
    for (let x = el; x; x = x.parentNode) {
      if (x.hidden || (x.classList && x.classList.contains("hidden"))) return false;
      if (B && x === B.box) break;
    }
    return !el.getClientRects || el.getClientRects().length > 0;
  }

  /** Opens the browser: opts {use, session, start (the field's value), onChoose(path), opener (the «Обзор…» button,
   * refocused when the browser closes without a choice)}. */
  function browse(opts = {}) {
    const page = P;
    const t = page.t;
    if (B) closeBrowser(false);
    let back = document.querySelector("#folder-picker-backdrop");
    let box = document.querySelector("#folder-picker");
    if (!back || !box) {
      back = h("div", { class: "fp-backdrop hidden", id: "folder-picker-backdrop" });
      box = h("div", { class: "modal fp", id: "folder-picker", role: "dialog", "aria-modal": "true",
                       "aria-labelledby": "fp-title", tabindex: "-1" });
      back.appendChild(box);
      document.body.appendChild(back);
    }
    box.textContent = "";
    const r = {};
    r.close = h("button", { class: "icon-btn small", type: "button", title: t("cancel"), "aria-label": t("cancel"),
                            fp: { act: "cancel" } }, h("i", { class: "ic ic-close" }));
    r.places = h("div", { class: "fp-places" });
    r.placesSelect = h("select", { class: "fp-places-select", "aria-labelledby": "fp-title" });
    r.up = h("button", { class: "btn small fp-up", type: "button", disabled: true, fp: { act: "up" } },
             h("i", { class: "ic ic-chevron fp-up-ic" }), t("fp_up"));
    r.crumbs = h("nav", { class: "fp-crumbs" });
    r.trail = h("span", { class: "fp-trail" });
    r.crumbs.appendChild(r.up);
    r.crumbs.appendChild(r.trail);
    r.list = h("ul", { class: "fp-list", role: "listbox", id: "fp-list" });
    r.status = h("div", { class: "fp-status", role: "status", "aria-live": "polite" });
    r.path = h("code", { class: "fp-path" });
    r.cannot = h("div", { class: "fp-cannot", hidden: true });
    r.newBtn = h("button", { class: "btn small", type: "button", "aria-expanded": "false",
                             "aria-controls": "fp-new-row", fp: { act: "new" } },
                 h("i", { class: "ic ic-plus" }), t("fp_new"));
    r.newInput = h("input", { type: "text", class: "fp-new-name", spellcheck: "false", autocomplete: "off",
                              "aria-label": t("fp_new") });
    r.newOk = h("button", { class: "btn small", type: "button", fp: { act: "new-ok" } }, t("fp_new_ok"));
    r.newError = h("div", { class: "fp-new-error err-text", role: "alert" });
    r.newRow = h("div", { class: "fp-new", id: "fp-new-row", hidden: true },
                 h("div", { class: "fo-row" }, r.newInput, r.newOk),
                 h("div", { class: "field-hint", text: t("fp_new_hint") }), r.newError);
    r.cancel = h("button", { class: "btn", type: "button", fp: { act: "cancel" } }, t("cancel"));
    r.choose = h("button", { class: "btn primary", type: "button", disabled: true, fp: { act: "choose" } },
                 t("fp_choose"));
    box.appendChild(h("div", { class: "modal-head" }, h("h3", { id: "fp-title", text: t("fp_title") }), r.close));
    box.appendChild(h("div", { class: "fp-body" }, r.places,
                      h("div", { class: "fp-main" }, r.placesSelect, r.crumbs, r.list, r.status)));
    box.appendChild(h("div", { class: "fp-foot" }, r.path, r.cannot, r.newRow,
                      h("div", { class: "modal-actions" }, r.newBtn, h("span", { class: "spacer" }), r.cancel,
                        r.choose)));
    // the page's modal under the browser takes no focus and no click while it is open (`inert`)
    const under = document.querySelector("#modal-backdrop");
    if (under && under.setAttribute) under.setAttribute("inert", "");
    B = { page, opts, back, box, r, under, data: null, seq: 0, loading: false, error: null, note: null,
          requested: null, down: false, clicked: null };
    if (!box._fpWired) {
      box._fpWired = true;
      box.addEventListener("keydown", onKey);
      box.addEventListener("click", onClick);
      box.addEventListener("dblclick", onDouble);
      box.addEventListener("change", onPlaceSelect);
      back.addEventListener("mousedown", (e) => { if (B) B.down = e.target === back; });
      back.addEventListener("click", (e) => {
        if (!B || e.target !== back) return;
        e.stopPropagation();
        if (B.down) closeBrowser(true);   // (a text selection that ends outside is no click on the backdrop)
        if (B) B.down = false;
      });
    }
    back.classList.remove("hidden");
    const start = unquote(opts.start);
    if (start) list(start, { first: true }); else openFirst(null);
  }

  function closeBrowser(refocus) {
    const b = B;
    if (!b) return;
    B = null;
    if (b.under && b.under.removeAttribute) b.under.removeAttribute("inert");
    b.back.classList.add("hidden");
    b.box.textContent = "";
    if (refocus && b.opts.opener && b.opts.opener.focus) b.opts.opener.focus();
  }

  /** The roots alone, then the first root (the home on the host; in a container the DreamTeam home or the first api
   * root), with `note` (a refusal of the typed path) in the status line. */
  async function openFirst(note) {
    const b = B;
    const my = ++b.seq;
    b.loading = true;
    renderStatus();
    let data;
    try { data = await request(b, null); } catch (e) {
      if (B !== b || my !== b.seq) return;
      b.loading = false; b.error = (e && e.message) || String(e); b.note = note; renderStatus(); focusFirst();
      return;
    }
    if (B !== b || my !== b.seq) return;
    const first = (data.roots || [])[0];
    if (!first) { b.loading = false; b.data = data; b.note = note; render(); focusFirst(); return; }
    await list(first.path, { note });
  }

  function request(b, path) {
    const body = { use: b.opts.use || "workspace" };
    if (b.opts.session) body.session = b.opts.session;
    if (path) body.path = path;
    return b.page.request("/api/folders/list", body);
  }

  /** Lists `path` (one listing at a time: a newer one supersedes it). how: {first (the browser's first listing: a
   * refusal opens the first root instead), focus (the entry path to focus after), note}. */
  async function list(path, how = {}) {
    const b = B;
    if (!b) return;
    const my = ++b.seq;
    b.loading = true;
    b.note = how.note || null;   // (the refusal of a typed path goes with the first root's listing only)
    renderStatus();
    let data = null, err = null;
    try { data = await request(b, path); } catch (e) { err = e; }
    if (B !== b || my !== b.seq) return;
    if (err) {
      const status = Number(err.status) || 0;
      const text = (err && err.message) || String(err);
      if (how.first && status >= 400 && status < 500 && status !== 429) { await openFirst(text); return; }
      b.loading = false; b.error = text; renderStatus();
      if (!b.data) focusFirst();
      return;
    }
    b.loading = false; b.error = null; b.note = how.note || null; b.requested = path;
    b.data = data;
    render();
    focusFirst(how.focus);
  }

  function render() {
    const b = B, t = b.page.t, r = b.r, d = b.data || {};
    const roots = Array.isArray(d.roots) ? d.roots : [];
    // the places: a column of buttons (a select on a narrow screen), the recent folders under their heading
    r.places.textContent = "";
    r.placesSelect.textContent = "";
    let group = null, recent = null;   // the recent folders' optgroup and their group of buttons (api roots after it)
    roots.forEach((root, i) => {
      const label = rootLabel(t, root);
      const tip = root.kind === "api" ? `${root.path}\n${t("fp_root_api_hint")}` : root.path;
      if (root.kind === "recent" && !group) {
        recent = h("div", { class: "fp-recent", role: "group", "aria-labelledby": "fp-recent-head" },
                   h("div", { class: "fp-group", id: "fp-recent-head", text: t("fp_recent") }));
        r.places.appendChild(recent);
        group = h("optgroup", { label: t("fp_recent") });
        r.placesSelect.appendChild(group);
      }
      const active = d.root === root.path;
      (root.kind === "recent" ? recent : r.places).appendChild(
        h("button", { type: "button", class: "fp-place" + (active ? " active" : ""), title: tip,
                      "aria-current": active ? "true" : null, fp: { act: "root", path: root.path } },
          h("i", { class: "ic ic-" + (root.kind === "recent" ? "clock" : "folder") }), h("span", { text: label })));
      const option = h("option", { value: String(i), text: label, title: tip });
      if (active) option.selected = true;
      (root.kind === "recent" ? group : r.placesSelect).appendChild(option);
    });
    if (!roots.some((root) => root.path === d.root)) {   // a folder outside the places: the select shows "—"
      const none = h("option", { value: "", text: "—" });
      none.selected = true;
      r.placesSelect.appendChild(none);
    }
    // the breadcrumb inside its root, after «Наверх»
    r.up.disabled = !d.parent;
    r.trail.textContent = "";
    const rootOf = roots.find((root) => root.path === d.root);
    crumbs(d.path, d.root, d.sep).forEach((c, i, all) => {
      if (i) r.trail.appendChild(h("span", { class: "fp-sep", "aria-hidden": "true", text: "›" }));
      const last = i === all.length - 1;
      r.trail.appendChild(h("button", { type: "button", class: "link fp-crumb", "aria-current": last ? "page" : null,
                                        fp: { act: "crumb", path: c.path } },
                            h("bdi", { dir: "ltr",
                                       text: c.label === null ? (rootOf ? rootLabel(t, rootOf) : c.path) : c.label })));
    });
    // the subfolders
    r.list.textContent = "";
    r.list.setAttribute("aria-label", d.path || t("fp_title"));
    (Array.isArray(d.entries) ? d.entries : []).forEach((entry, i) => r.list.appendChild(entryEl(entry, i)));
    renderStatus();
    // the foot: the folder shown, why it cannot be chosen
    r.path.textContent = d.path || "";
    r.choose.disabled = !(d.path && d.selectable);
    const why = !d.path || d.selectable ? ""
      : d.refused ? String(d.refused)
        : d.busy && (b.opts.use || "workspace") === "workspace"
          ? (d.busy.sid ? t("fp_busy_why", { title: d.busy.title || d.busy.sid }) : t("fp_creating"))
          : t("fp_nav_why");
    r.cannot.textContent = why ? t("fp_cannot", { why }) : "";
    r.cannot.hidden = !why;
    r.newBtn.disabled = !d.path;
  }

  function entryEl(entry, i) {
    const t = B.page.t;
    const use = B.opts.use || "workspace";
    const marks = [];
    const mark = (text, cls = "", title = null) => marks.push(h("span", { class: "fp-mark" + (cls ? " " + cls : ""),
                                                                           title, text }));
    if (entry.repo) mark(t("fp_repo"));
    if (entry.files === false) mark(t("fp_empty"));
    if (entry.noaccess) mark(t("fp_noaccess"), "warn");
    if (entry.link) mark(t("fp_link"));
    if (entry.busy) {
      mark(entry.busy.sid ? t("fp_busy", { title: entry.busy.title || entry.busy.sid }) : t("fp_creating"),
           use === "workspace" ? "warn" : "");
    }
    if (entry.current) mark(t("fp_current"), "accent");
    if (entry.refused) mark(t("fp_refused"), "muted", String(entry.refused));
    // the refusal: the option's description (`hidden`: read by aria-describedby, never twice in its name)
    const why = entry.refused
      ? h("span", { class: "fp-why", id: `fp-why-${i}`, hidden: true, text: String(entry.refused) }) : null;
    return h("li", { role: "option", id: `fp-opt-${i}`, tabindex: i === 0 ? "0" : "-1", "aria-selected": "false",
                     "aria-disabled": entry.open === false ? "true" : null,
                     "aria-describedby": why ? `fp-why-${i}` : null,
                     title: entry.refused ? String(entry.refused) : null,
                     class: "fp-entry" + (entry.refused ? " refused" : "") + (entry.open === false ? " closed" : ""),
                     fp: { act: "entry", entry, index: i } },
             h("i", { class: "ic ic-folder", "aria-hidden": "true" }),
             h("bdi", { dir: "ltr", class: "fp-name", text: entry.name }),
             h("span", { class: "fp-marks" }, marks), why);
  }

  function renderStatus() {
    const b = B;
    if (!b) return;
    const t = b.page.t, d = b.data;
    const parts = [];
    if (b.loading) parts.push(t("fp_loading"));
    else {
      if (b.error) parts.push(b.error);
      if (b.note) parts.push(b.note);
      if (d) {
        if (d.error) parts.push(String(d.error));
        if (d.fallback && b.requested) parts.push(t("fp_fallback", { path: b.requested }));
        const n = Array.isArray(d.entries) ? d.entries.length : 0;
        if (d.path && !d.error && !n) parts.push(t("fp_none"));
        if (d.truncated) parts.push(t("fp_truncated", { n }));
        if (d.partial) parts.push(t("fp_partial"));
      }
    }
    b.r.status.textContent = parts.join(" ");
  }

  function entryEls() { return B ? Array.from(B.r.list.children || []) : []; }
  /** The roving tabindex: the focused entry is the list's one tab stop. */
  function focusEntry(i) {
    const items = entryEls();
    if (!items.length) return;
    const k = Math.max(0, Math.min(items.length - 1, i));
    items.forEach((el, j) => {
      el.setAttribute("tabindex", j === k ? "0" : "-1");
      el.setAttribute("aria-selected", j === k ? "true" : "false");
    });
    items[k].focus();
  }
  /** After a listing: the entry `path` (the folder we came up from), else the first entry, else «Выбрать эту папку»
   * (or «Отмена» while that cannot be chosen). */
  function focusFirst(path) {
    const b = B;
    if (!b) return;
    const items = entryEls();
    const at = path ? items.findIndex((el) => el._fp.entry.path === path) : -1;
    if (items.length) focusEntry(at < 0 ? 0 : at);
    else if (!b.r.choose.disabled) b.r.choose.focus();
    else b.r.cancel.focus();
  }

  function goUp() { if (B && B.data && B.data.parent) list(B.data.parent, { focus: B.data.path }); }
  function openEntry(el) { if (el && el._fp.entry.open !== false) list(el._fp.entry.path); }
  function choosable(entry) {
    return !!entry && !entry.refused && !((B.opts.use || "workspace") === "workspace" && entry.busy);
  }
  function choose(path) {
    const fn = B && B.opts.onChoose;
    closeBrowser(false);
    if (fn) fn(path);
  }
  function showNew(on) {
    const r = B.r;
    r.newRow.hidden = !on;
    r.newBtn.setAttribute("aria-expanded", on ? "true" : "false");
    r.newError.textContent = "";
    if (on) { r.newInput.value = ""; r.newInput.focus(); }
  }
  function chooseNew() {
    const b = B, r = b.r, d = b.data || {};
    const name = String(r.newInput.value || "");
    if (!d.path) return;
    if (!newNameOk(name, d.sep)) { r.newError.textContent = b.page.t("fp_new_bad"); r.newInput.focus(); return; }
    const sep = d.sep || "/";
    choose(d.path.endsWith(sep) ? d.path + name : d.path + sep + name);
  }

  function onClick(e) {
    const b = B;
    if (!b) return;
    const el = fpOf(e.target);
    const fp = el && !el.disabled ? el._fp : null;
    // the entry of a double click is its first click's: that click has opened it, and its listing may already show
    // the entry's subfolders under the pointer when the second click and the dblclick come (onDouble)
    if (!(e.detail > 1)) b.clicked = fp && fp.act === "entry" ? fp.entry : null;
    if (!fp) return;
    if (fp.act === "cancel") closeBrowser(true);
    else if (fp.act === "root" || fp.act === "crumb") list(fp.path);
    else if (fp.act === "up") goUp();
    else if (fp.act === "entry") { if (e.detail > 1) return; focusEntry(fp.index); openEntry(el); }
    else if (fp.act === "choose") { if (b.data && b.data.path && b.data.selectable) choose(b.data.path); }
    else if (fp.act === "new") showNew(b.r.newRow.hidden);
    else if (fp.act === "new-ok") chooseNew();
  }
  /** A double click on an entry chooses the entry of its first click (which has started opening it: that listing is
   * dropped), whatever row is under the pointer by now. */
  function onDouble(e) {
    const b = B;
    if (!b) return;
    const el = fpOf(e.target);
    const entry = b.clicked || (el && el._fp.act === "entry" ? el._fp.entry : null);
    b.clicked = null;
    if (choosable(entry)) choose(entry.path);
  }
  function onPlaceSelect(e) {
    const b = B;
    if (!b || e.target !== b.r.placesSelect || e.target.value === "") return;
    const root = ((b.data && b.data.roots) || [])[Number(e.target.value)];
    if (root) list(root.path);
  }
  function onKey(e) {
    const b = B;
    if (!b) return;
    const r = b.r, target = e.target;
    const inNew = r.newRow.contains(target);
    if (e.key === "Escape") {
      // the browser's own: the dialog under it never sees it (home.js and app.js listen on document, bubbling)
      e.stopPropagation();
      e.preventDefault();
      if (inNew) { showNew(false); r.newBtn.focus(); } else closeBrowser(true);
      return;
    }
    if (inNew) {   // the name input's keys are its own; Enter is its «Выбрать»
      if (e.key === "Enter" && target === r.newInput) { e.preventDefault(); chooseNew(); }
      return;
    }
    if (e.key === "Tab") { trap(e); return; }
    // the way up works from anywhere in the browser (an empty folder leaves the focus on «Выбрать эту папку»),
    // but the places select keeps its own keys
    if (((e.key === "ArrowUp" && e.altKey) || e.key === "Backspace") && target !== r.placesSelect) {
      e.preventDefault();
      goUp();
      return;
    }
    const inList = r.list.contains(target), inCrumbs = r.crumbs.contains(target);
    if (!inList && !inCrumbs) return;
    const items = entryEls();
    const current = inList ? fpOf(target) : null;
    const i = current ? items.indexOf(current) : -1;
    let handled = true;
    if (e.key === "ArrowDown") focusEntry(inList ? i + 1 : 0);
    else if (e.key === "ArrowUp") { if (inList) focusEntry(i - 1); }
    else if (e.key === "Home") focusEntry(0);
    else if (e.key === "End") focusEntry(items.length - 1);
    else if ((e.key === "Enter" || e.key === " ") && inList) openEntry(current);
    else handled = false;
    if (handled) e.preventDefault();
  }
  /** Tab and Shift+Tab cycle inside the browser. */
  function trap(e) {
    const all = Array.from(B.box.querySelectorAll("button, select, input, [tabindex]"))
      .filter((el) => !el.disabled && el.getAttribute("tabindex") !== "-1" && visible(el));
    if (!all.length) return;
    const at = all.indexOf(document.activeElement);
    const next = e.shiftKey ? (at <= 0 ? all.length - 1 : at - 1) : (at < 0 || at === all.length - 1 ? 0 : at + 1);
    e.preventDefault();
    all[next].focus();
  }

  // ---- «Сменить папку…» --------------------------------------------------------------------------------------
  /** True while the change dialog's request runs: the page's modal cannot close then. */
  function busy() { return moving; }

  /** The change dialog of a session in the page's modal: {sid, title, workspace (the summary's or the snapshot's),
   * running, onDone(data)}. */
  function changeDialog({ sid, title, workspace, running, onDone }) {
    const page = P;
    const { t, esc } = page;
    const ws = workspace && typeof workspace === "object" ? workspace : {};
    const kindText = !ws.default && ws.path && !ws.exists ? t("ws_missing_badge")
      : ws.default ? t("ws_default") : ws.adopted ? t("ws_adopted") : t("ws_moved");
    const name = title || sid;
    const own = ws.default ? " disabled" : "";   // «Вернуть в свою папку» while it works in its own folder
    page.openModal(`<div class="modal-head"><h3>${esc(t("cf_title", { title: name }))}</h3>
        <button class="icon-btn small" type="button" id="cf-x" data-close title="${esc(t("cancel"))}"
          aria-label="${esc(t("cancel"))}"><i class="ic ic-close"></i></button></div>
      <p class="cf-now">${esc(t("cf_now", { path: ws.path || "", kind: kindText }))}</p>
      <div class="cf-choices">
        <label class="check"><input type="radio" name="cf-mode" id="cf-other" value="other" checked>
          <span>${esc(t("cf_other"))}</span></label>
        <div class="fo-row cf-other-row"><input id="cf-path" type="text" spellcheck="false" autocomplete="off"
          aria-label="${esc(t("cf_other"))}" aria-describedby="cf-check">
          <button class="btn" type="button" id="cf-browse" aria-haspopup="dialog">${esc(t("fo_browse"))}</button></div>
        <label class="check"><input type="radio" name="cf-mode" id="cf-default" value="default"${own}>
          <span>${esc(t("cf_default"))}</span></label>
        <div class="fo-line" id="cf-check" role="status" aria-live="polite"></div>
      </div>
      <p class="field-hint cf-hint">${esc(t("cf_hint"))}</p>
      <div class="err-text" id="cf-error" role="alert"></div>
      <div class="modal-actions">
        <button class="btn" type="button" id="cf-cancel" data-close>${esc(t("cancel"))}</button>
        <button class="btn primary" type="button" id="cf-go" disabled>${esc(t("cf_go"))}</button>
      </div>`);
    const $ = (sel) => document.querySelector(sel);
    const input = $("#cf-path"), go = $("#cf-go"), other = $("#cf-other"), dflt = $("#cf-default");
    const browseBtn = $("#cf-browse"), cancel = $("#cf-cancel"), error = $("#cf-error"), x = $("#cf-x");
    const isDefault = () => dflt.checked === true;
    /** What the click sends for the shown check: its answer's target and expect; a typed folder whose check could not
     * run goes as typed, without expect (the hub decides and says, as for a create); its own folder needs an answer.
     * → {path | null (its own folder), expect | null} or null (the button disabled). */
    const move = () => {
      const res = check.result;
      if (!check.ready() || !res) return null;
      const a = res.answer;
      if (a) {
        return a.error || a.mode === "unchanged" ? null : { path: isDefault() ? null : a.target, expect: a.expect };
      }
      return res.failed !== null && !isDefault() ? { path: unquote(input.value), expect: null } : null;
    };
    const update = () => { if (!moving) go.disabled = !move(); };
    const check = checker(input, $("#cf-check"), { use: "workspace", session: sid, isDefault, old: () => ws.path || "",
                                                   onChange: update });
    const pick = () => {
      input.disabled = isDefault();
      browseBtn.disabled = isDefault();
      error.textContent = "";
      check.run(true);
    };
    other.addEventListener("change", pick);
    dflt.addEventListener("change", pick);
    browseBtn.addEventListener("click", () => browse({
      use: "workspace", session: sid, start: input.value, opener: browseBtn,
      onChoose: (path) => { input.value = path; if (input.focus) input.focus(); check.run(true); },
    }));
    go.addEventListener("click", async () => {
      const res = check.result, to = move();
      if (moving || go.disabled || !to) return;
      const back = to.path === null;
      const shown = back ? res.answer.target : to.path;
      if (!confirm(t(back ? "cd_ask_default" : "cd_ask", { title: name, path: shown, text: res.words.text }))) return;
      moving = true;
      const label = go.textContent;
      for (const el of [go, cancel, x, input, browseBtn, other, dflt]) if (el) el.disabled = true;
      go.textContent = t("cf_moving");
      error.textContent = "";
      const body = back ? { default: true } : { path: to.path };
      if (to.expect) body.expect = to.expect;
      try {
        const data = await page.request(`/api/sessions/${encodeURIComponent(sid)}/workspace`, body);
        moving = false;
        for (const line of resultTexts(t, data && data.result, name)) page.toast(line.text, line.err);
        page.closeModal(true);
        if (onDone) onDone(data);
      } catch (e) {
        moving = false;
        go.textContent = label;
        for (const el of [cancel, x, other]) if (el) el.disabled = false;
        dflt.disabled = !!ws.default;
        input.disabled = browseBtn.disabled = isDefault();
        error.textContent = (e && e.message) || String(e);
        check.run(true);   // the folder changed since its check, or another session took it: say what it is now
      }
    });
    if (input.focus) input.focus();
    return check;
  }

  return { setup, unquote, lineWords, resultLines, resultTexts, newNameOk, rootLabel, crumbs, checker, browse,
           closeBrowser, isOpen, busy, changeDialog, LINE_KEYS, RESULT_KEYS, CHECK_DELAY, CHECKING_DELAY };
})();

if (typeof module !== "undefined" && module.exports) module.exports = Folders;
