"use strict";
// The skills parts both pages share (docs/SPEC.md §18.4): the add form (a folder, a .zip or a Git URL) with its
// import flow (a source holding several skills asks which ones, a clash with the library asks before replacing),
// the zip → base64 helper and the line that says what attaching a skill would install. Each page passes its own
// t(), esc() and request function; the texts (sk_*) live in both pages' dictionaries.

const Skills = (() => {
  const SOURCES = ["folder", "zip", "git"];
  const CHUNK = 0x8000;   // bytes per String.fromCharCode call: a whole archive at once overflows the call stack
  const PACKAGES_SHOWN = 8;

  function fmtSize(n) {
    const v = Number(n) || 0;
    if (v < 1024) return v + " B";
    if (v < 1048576) return (v / 1024).toFixed(1) + " KB";
    return (v / 1048576).toFixed(1) + " MB";
  }

  /** base64 of an ArrayBuffer (or a typed array): the zip goes inside JSON, so the hub's guard stays as it is. */
  function toBase64(buffer) {
    const bytes = buffer instanceof Uint8Array ? buffer : new Uint8Array(buffer);
    let bin = "";
    for (let i = 0; i < bytes.length; i += CHUNK) bin += String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK));
    return btoa(bin);
  }

  /** SKILL.md without its YAML front matter, for md(): the card shows the name and description already, and the
   * block would format as two rules around a paragraph. An unclosed block stays (it is not front matter). */
  function bodyOf(text) {
    return String(text || "").replace(/^﻿?---[ \t]*\r?\n(?:[\s\S]*?\r?\n)?---[ \t]*(?:\r?\n|$)/, "");
  }

  /** "requests, pypdf, …": the first packages of a manifest. */
  function packageList(packages) {
    const list = (packages || []).map(String);
    return list.slice(0, PACKAGES_SHOWN).join(", ") + (list.length > PACKAGES_SHOWN ? ", …" : "");
  }

  /** What attaching a skill installs, for its preview (`deps` of GET /api/skills/{name} or of a candidate; `install`
   * the settings' switches; `proposal`: a member's skill, which `install.proposals` covers). "" without manifests. */
  function previewLine(t, deps, install, proposal = false) {
    const found = ["python", "node"].filter((eco) => deps && deps[eco]);
    if (!found.length) return "";
    const on = !install || (install.enabled !== false && (!proposal || install.proposals !== false));
    if (!on) return t("sk_pkgs_off");
    const parts = [];
    let installs = false;
    for (const eco of found) {
      const d = deps[eco];
      const files = (d.files || []).join(", ");
      if (d.unsupported) parts.push(t("sk_pkgs_unsupported", { files, reason: d.unsupported }));
      else if ((d.packages || []).length) { parts.push(t("sk_pkgs_preview", { packages: packageList(d.packages), files })); installs = true; }
    }
    if (installs) parts.push(t("sk_pkgs_trust"));
    return parts.join(" ");
  }

  /** One import, as many requests as it takes: `post(body)` → {status, data}. A source holding several skills asks
   * `hooks.pick(candidates)` (names, or nothing to cancel) and posts again with them; a clash with the library (409
   * with `exists`) asks `hooks.replace(names)` and posts again with `replace`. → {ok: true, data} | {ok: false, error,
   * data} | {cancelled: true}. */
  async function runImport(post, body, hooks) {
    let request = Object.assign({}, body);
    for (let round = 0; round < 4; round++) {
      const res = await post(request);
      const data = (res && res.data) || {};
      const status = Number(res && res.status) || 0;
      if (status === 409 && Array.isArray(data.exists) && data.exists.length && !request.replace) {
        if (!(await hooks.replace(data.exists))) return { cancelled: true };
        request = Object.assign({}, request, { replace: true });
        continue;
      }
      if (status >= 400 || status === 0) return { ok: false, error: String(data.error || status || "?"), data };
      if (data.need_pick && !request.pick) {
        const names = await hooks.pick(data.candidates || []);
        if (!names || !names.length) return { cancelled: true };
        request = Object.assign({}, request, { pick: names });
        continue;
      }
      return { ok: true, data };
    }
    return { ok: false, error: "?", data: {} };
  }

  /** The text after an import: what came in (and was attached), what was left out of a skill, what failed. */
  function reportLines(t, data) {
    const out = [];
    const names = (data.imported || []).map((e) => e.name);
    if (names.length) out.push({ text: t("sk_imported", { names: names.join(", ") }) });
    for (const a of data.attached || []) out.push({ text: t(a.status === "unchanged" ? "sk_attached_same" : "sk_attached", { name: a.name }) });
    for (const f of data.failed || []) out.push({ text: `${f.name}: ${f.error}`, warn: true });
    for (const s of data.skipped || []) out.push({ text: t("sk_skipped", { path: s.path, error: s.error }), warn: true });
    return out;
  }

  /** The form's HTML: three sources (folder, zip, Git), one input each, an import button, a status line and the
   * place where a source with several skills lists them. `sandbox`: the hub runs in the Docker sandbox, so a folder
   * path is the container's (the hint says so; setSandbox shows or hides it once that is known). */
  function formHtml(t, esc, opts = {}) {
    const src = opts.source || "folder";
    const tabs = SOURCES.map((s) => `<button type="button" data-sk-src="${s}" class="${s === src ? "active" : ""}">${esc(t("sk_src_" + s))}</button>`).join("");
    const pane = (s, html) => `<div class="sk-pane${s === src ? "" : " hidden"}" data-sk-pane="${s}">${html}</div>`;
    return `<div class="sk-tabs">${tabs}</div>
      ${pane("folder", `<input type="text" data-sk-in="folder" autocomplete="off" placeholder="${esc(t("sk_folder_ph"))}"><span class="field-hint${opts.sandbox ? "" : " hidden"}" data-sk-sandbox>${esc(t("sk_folder_sandbox"))}</span>`)}
      ${pane("zip", `<input type="file" data-sk-in="zip" accept=".zip">`)}
      ${pane("git", `<input type="text" data-sk-in="git" autocomplete="off" placeholder="https://github.com/org/repo/tree/main/skills/pdf"><span class="field-hint">${esc(t("sk_git_hint"))}</span>`)}
      <div class="sk-go"><button class="btn small" type="button" data-sk-go><i class="ic ic-plus"></i>${esc(t(opts.attach ? "sk_add_attach" : "sk_add"))}</button>
        <span class="sk-status" data-sk-status></span></div>
      <div class="sk-pick hidden" data-sk-pick></div>`;
  }

  /** The candidates of a source holding several skills, as checkboxes (a broken one shows its error). */
  function pickHtml(t, esc, candidates) {
    const rows = (candidates || []).map((c) => `<label class="check${c.error ? " dim" : ""}"><input type="checkbox" value="${esc(c.name)}"${c.error ? " disabled" : ""}>
      <span><b>${esc(c.name)}</b> <span class="hint">${esc(c.error || c.description || "")}</span></span></label>`).join("");
    return `<div class="sk-pick-head">${esc(t("sk_pick"))}</div>${rows}
      <div class="sk-go"><button class="btn small" type="button" data-sk-pick-go>${esc(t("sk_pick_go"))}</button>
      <button class="btn small" type="button" data-sk-pick-cancel>${esc(t("sk_cancel"))}</button></div>`;
  }

  /** Show or hide the sandbox hint of every add form under `root` (the skills listing says `sandbox` once loaded). */
  function setSandbox(root, on) {
    if (!root || typeof root.querySelectorAll !== "function") return;
    root.querySelectorAll("[data-sk-sandbox]").forEach((el) => el.classList.toggle("hidden", !on));
  }

  /** The add form in `box` (browser only). opts: {t, esc, post(body) → {status, data}, onImported(data), limits,
   * attach (the session's form: imported skills are attached too), confirm(text), sandbox (a value or a function:
   * the hub runs in the Docker sandbox)}. */
  function mount(box, opts) {
    const { t, esc } = opts;
    let source = "folder";
    box.classList.add("skill-add");
    const sandbox = typeof opts.sandbox === "function" ? opts.sandbox() : opts.sandbox;
    box.innerHTML = formHtml(t, esc, { attach: opts.attach, sandbox: !!sandbox });
    const $ = (sel) => box.querySelector(sel);
    const status = (text, warn = false) => { const el = $("[data-sk-status]"); el.textContent = text; el.classList.toggle("err-text", warn); };
    let picking = null;   // resolves the pick question
    const pickArea = $("[data-sk-pick]");
    const hooks = {
      replace: async (names) => (opts.confirm || confirm)(t("sk_replace_q", { names: names.join(", ") })),
      pick: (candidates) => new Promise((resolve) => {
        pickArea.innerHTML = pickHtml(t, esc, candidates);
        pickArea.classList.remove("hidden");
        picking = resolve;
      }),
    };
    const endPick = (names) => {
      const done = picking;
      picking = null;
      pickArea.classList.add("hidden");
      pickArea.innerHTML = "";
      if (done) done(names);
    };
    async function body() {
      if (source === "zip") {
        const file = $('[data-sk-in="zip"]').files[0];
        if (!file) return null;
        const limits = typeof opts.limits === "function" ? opts.limits() : opts.limits;   // (loaded after the mount)
        const max = limits && limits.max_archive_mb;
        if (max && file.size > max * 1048576) throw new Error(t("sk_zip_big", { mb: max }));
        return { zip: toBase64(await file.arrayBuffer()), filename: file.name };
      }
      const text = $(`[data-sk-in="${source}"]`).value.trim();
      return text ? { [source === "folder" ? "path" : "git"]: text } : null;
    }
    // Enter in a field imports: inside the new-session form it would submit the whole form, skill left out (a
    // checkbox of the pick list is stopped too; the file input keeps Enter, which opens its picker)
    box.addEventListener("keydown", (e) => {
      const el = e.target;
      if (e.key !== "Enter" || e.isComposing || !el || el.tagName !== "INPUT" || el.type === "file") return;
      e.preventDefault();
      if (el.dataset && el.dataset.skIn) $("[data-sk-go]").click();
    });
    box.addEventListener("click", async (e) => {
      const el = e.target.closest("button");
      if (!el || !box.contains(el)) return;
      const ds = el.dataset;
      if (ds.skSrc) {
        source = ds.skSrc;
        box.querySelectorAll("[data-sk-src]").forEach((b) => b.classList.toggle("active", b.dataset.skSrc === source));
        box.querySelectorAll("[data-sk-pane]").forEach((p) => p.classList.toggle("hidden", p.dataset.skPane !== source));
      } else if ("skPickGo" in ds) {
        endPick(Array.from(pickArea.querySelectorAll("input:checked")).map((c) => c.value));
      } else if ("skPickCancel" in ds) {
        endPick(null);
      } else if ("skGo" in ds) {
        if (el.disabled) return;
        let request;
        try { request = await body(); } catch (err) { status(err.message, true); return; }
        if (!request) { status(t("sk_need_source"), true); return; }
        if (opts.attach) request.attach = true;
        el.disabled = true;
        status(t("sk_importing"));
        try {
          const res = await runImport(opts.post, request, hooks);
          if (res.cancelled) status("");
          else if (!res.ok) status(res.error, true);
          else {
            const lines = reportLines(t, res.data);
            status(lines.map((l) => l.text).join(" "), lines.some((l) => l.warn));
            const input = $(`[data-sk-in="${source}"]`);
            if (input && source !== "zip") input.value = "";
            if (opts.onImported) opts.onImported(res.data);
          }
        } catch (err) { status(err.message || String(err), true); }
        el.disabled = false;
      }
    });
  }

  return { fmtSize, toBase64, bodyOf, packageList, previewLine, runImport, reportLines, formHtml, pickHtml, mount,
           setSandbox, SOURCES };
})();

if (typeof module !== "undefined" && module.exports) module.exports = Skills;
