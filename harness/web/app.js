"use strict";
const TOKEN = document.querySelector('meta[name="factory-token"]').content;
const MAIN = document.getElementById("main");
const ERR = document.getElementById("error");
const ACTIVE = ["queued", "waiting_gpu", "running"];
const TERMINAL = ["succeeded", "failed", "rejected", "error", "cancelled", "dead"];

// ---- Toast notifications (always visible) + browser notifications (P06) ----
let _notifPermission = null;
const _jobStatusBefore = {}; // jobId -> previous status (used by renderJob)
const _globalJobStatuses = {}; // job_id -> last seen status, toutes pages

// Notifie chaque job qui vient d'entrer dans un état terminal. Appelé avec les
// données déjà chargées quand la page les a, sinon depuis render().
function trackJobStatuses(jobs) {
  for (const j of jobs || []) {
    if (!j.job_id || !j.status) continue;
    const prev = _globalJobStatuses[j.job_id];
    if (prev && !TERMINAL.includes(prev) && TERMINAL.includes(j.status)) {
      notifyJobDone(j.job_id, j.status);
    }
    _globalJobStatuses[j.job_id] = j.status;
  }
}

async function ensureNotifPermission() {
  if (_notifPermission !== null) return;
  try { _notifPermission = await Notification.requestPermission(); }
  catch { _notifPermission = "denied"; }
}

// Toast container (single global element)
const _TOAST = document.createElement("div");
_TOAST.className = "toast-container";
document.body.appendChild(_TOAST);

function showToast(message, status) {
  const cls = "toast toast-" + (status || "failed");
  const el = document.createElement("div");
  el.className = cls;
  // Nodes, jamais innerHTML: un toast porte le titre d'un job, donc du texte
  // qui vient d'un modele (invariant garde par test_factory_web).
  el.appendChild(document.createTextNode(message));
  const close = document.createElement("span");
  close.className = "toast-close";
  close.textContent = "×";
  el.appendChild(close);
  _TOAST.appendChild(el);
  // play notification sound
  try { playNotificationSound(); } catch (_) {}
  // click to close
  el.addEventListener("click", (e) => {
    if (e.target.classList.contains("toast-close") || e.target === el) {
      el.classList.add("out");
      setTimeout(() => el.remove(), 300);
    }
  });
  setTimeout(() => {
    if (!el.classList.contains("out")) {
      el.classList.add("out");
      setTimeout(() => el.remove(), 300);
    }
  }, 6000);
}

// Simple notification sound via Web Audio API (no external files needed)
function playNotificationSound() {
  const ctx = new (window.AudioContext || window.webkitAudioContext)();
  const osc = ctx.createOscillator();
  const gain = ctx.createGain();
  osc.connect(gain);
  gain.connect(ctx.destination);
  osc.frequency.setValueAtTime(880, ctx.currentTime);
  osc.frequency.setValueAtTime(1100, ctx.currentTime + 0.08);
  gain.gain.setValueAtTime(0.15, ctx.currentTime);
  gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.3);
  osc.start(ctx.currentTime);
  osc.stop(ctx.currentTime + 0.3);
}

function notifyJobDone(jobId, newStatus) {
  const emoji = newStatus === "succeeded" ? "✅" : "❌";
  // Toast always shows (even when tab is focused)
  showToast(emoji + " job " + jobId.slice(0, 8) + " — " + newStatus, newStatus);
  // Also try native browser notification
  ensureNotifPermission().then(() => {
    if (_notifPermission === "granted") {
      new Notification("local-factory · job " + emoji, {
        body: jobId + " — " + newStatus,
        tag: jobId,
      });
    }
  }).catch(() => {});
}

// Request permission early — runs as soon as script loads (user context)
ensureNotifPermission();

async function api(path, opts = {}) {
  const headers = { "Content-Type": "application/json" };
  if ((opts.method || "GET") !== "GET") headers["X-Factory-Token"] = TOKEN;
  const resp = await fetch(path, Object.assign({}, opts, { headers }));
  const data = await resp.json();
  if (!resp.ok) throw new Error(data.error + ": " + data.detail);
  return data;
}

function showError(e) {
  ERR.hidden = false;
  ERR.textContent = e.message;
  setTimeout(() => { ERR.hidden = true; }, 8000);
}

// /api/runtime est local et instantane (pas d'appel au serveur modele):
// le cache ne sert qu'a eviter un fetch par render.
let RUNTIME_CACHE = { at: 0, data: null };
async function runtimeModels(maxAge = 30000) {
  if (RUNTIME_CACHE.data && Date.now() - RUNTIME_CACHE.at < maxAge)
    return RUNTIME_CACHE.data;
  const data = await api("/api/runtime/models");
  RUNTIME_CACHE = { at: Date.now(), data };
  return data;
}

function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "text") el.textContent = v;
    else el.setAttribute(k, v);
  }
  // `append(null)` écrit le texte « null ». Un enfant conditionnel est le cas
  // normal ici (le bouton de scroll n'existe pas hors session) : c'est ce qui
  // affichait « null » sous « Choisis une session ».
  el.append(...children.filter((c) => c !== null && c !== undefined
                                      && c !== false));
  return el;
}

// One sprite, one style. `use` keeps the markup to one line per icon and the
// file cached once -- served locally, never fetched from a CDN.
function icon(name, cls) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "ico" + (cls ? " " + cls : ""));
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", "/static/icons.svg#ico-" + name);
  svg.append(use);
  return svg;
}

// Un bouton dessiné. Le libellé ne disparaît pas, il déménage dans le
// `title` : sans lui un pictogramme est une devinette, et le lecteur d'écran
// n'a plus rien à annoncer.
function iconBtn(name, label, onclick, cls) {
  const b = h("button", { class: "icon-btn" + (cls ? " " + cls : ""),
    title: label, "aria-label": label, onclick });
  b.append(icon(name));
  return b;
}

// Un lien sortant, dessiné aussi. « console » était rendu en bleu de lien par
// défaut : illisible sur le fond noir, et long pour ce qu'il dit.
function iconLink(name, label, href) {
  const a = h("a", { class: "icon-btn", href, target: "_blank",
    rel: "noopener", title: label, "aria-label": label });
  a.append(icon(name));
  return a;
}

// Un titre de section porte sa marque. Le pictogramme est décoratif — le texte
// dit tout — mais il rend une page longue repérable sans la lire.
function heading(tag, name, text) {
  return h(tag, { class: "with-ico" }, icon(name), h("span", { text }));
}

// Une mesure et son pictogramme, pour les barres de chiffres.
function stat(name, text, title) {
  return h("span", { class: "with-ico", title: title || text },
    icon(name), h("span", { text }));
}

const fmtWhen = (ts) => ts ? new Date(ts * 1000).toLocaleString("fr-FR") : "—";
const fmtDur = (s) => s == null ? "—"
  : s < 60 ? Math.round(s) + " s"
  : Math.floor(s / 60) + " min " + Math.round(s % 60) + " s";
const fmtMs = (ms) => ms == null ? "—"
  : ms < 1000 ? ms + " ms"
  : ms < 60000 ? Math.round(ms / 1000) + " s"
  : Math.floor(ms / 60000) + " min " + Math.round((ms % 60000) / 1000) + " s";
const fmtTokens = (n) => n == null ? "—"
  : n >= 1e6 ? (n / 1e6).toFixed(2) + " M"
  : n >= 1e3 ? (n / 1e3).toFixed(1) + " k"
  : String(n);
const fmtCost = (c) => c == null || c === 0 ? "—"
  : "$" + c.toFixed(4);
const badge = (status) => h("span", { class: "badge " + status, text: status });


// ---- Plan + trace panel (agent sessions) ----
// Le carnet ne vit que dans le prompt du modele: sans cette vue, rien n'indique
// a l'ecran qu'un plan existe, ni ou il en est, ni ce qui s'est mal passe.

const fmtPct = (p) => (p == null ? "—" : p + " %");

// Plan slot — created once at load, lives in #plan-slot-container (outside #main)
let panel = null; // holds the DOM node returned by planPanel()
const planSlot = h("div", { class: "plan-slot" });

function planPanel(trace) {
  if (!trace) return h("div", { class: "empty", text: "Aucun plan — lancez un agent pour voir le plan ici." });
  const plan = trace.plan;
  const s = trace.summary || {};
  const kids = [];

  const ctx = h("div", { class: "trace-ctx" },
    h("b", { text: "contexte " + fmtTokens(trace.ctx_est) + " tok" }),
    h("span", { text: s.ctx_peak_pct ? "pic " + fmtPct(s.ctx_peak_pct) : "" }));
  kids.push(ctx);

  if (plan) {
    const revised = plan.revisions
      ? h("span", { class: "trace-warn",
                    title: "chaque revision repart du plan au lieu de l'executer",
                    text: "plan revu " + plan.revisions + "×" }) : "";
    const done = (plan.steps || []).filter((x) => x.done).length;
    kids.push(h("div", { class: "plan-head" },
      h("b", { text: "Plan" }),
      h("span", { text: done + "/" + (plan.steps || []).length }),
      // La phase est deduite du transcript, pas declaree par le modele.
      trace.phase ? h("span", { class: "phase phase-" + trace.phase,
                                text: trace.phase }) : "",
      revised,
      trace.stagnation
        ? h("span", { class: "trace-warn",
                      title: "tours sans progression visible",
                      text: "stagnation " + trace.stagnation }) : ""));
    kids.push(h("ol", { class: "plan-steps" },
      ...(plan.steps || []).map((st) => h("li", { class: st.done ? "done" : "" },
        h("span", { class: "plan-step", text: st.step || "" }),
        st.done_when ? h("code", { class: "plan-when", text: st.done_when }) : "",
      ))));
  } else {
    kids.push(h("div", { class: "empty", text: "Aucun plan posé." }));
  }

  kids.push(h("div", { class: "trace-head" },
    h("b", { text: "Appels" }),
    h("span", { text: (s.calls || 0) + " · " + (s.errors || 0) + " err · "
      + (s.replayed || 0) + " rejoués" })));

  const per = Object.entries(s.tool_calls || {})
    .sort((a, b) => b[1] - a[1]).slice(0, 8);
  if (per.length) {
    kids.push(h("table", { class: "trace-tools" },
      h("tbody", {}, ...per.map(([name, n]) => h("tr", {},
        h("td", { text: name }),
        h("td", { class: "mono", text: String(n) }),
        h("td", { class: "mono", text: fmtMs((s.tool_ms || {})[name]) }))))));
  }

  // Ce que la session gaspille, chiffre. Les incidents disent qu'un appel a ete
  // rejoue; ceci dit ce qu'il a coute -- et c'est ce chiffre qui decide.
  const findings = trace.findings || [];
  if (findings.length) {
    const waste = trace.waste || {};
    kids.push(h("div", { class: "trace-head" },
      h("b", { text: "Coûts" }),
      h("span", { text: waste.weight_s ? "~" + waste.weight_s + " s perdues" : "" })));
    kids.push(h("ul", { class: "trace-anomalies trace-findings" },
      ...findings.slice(0, 6).map((f) => h("li", {},
        h("span", { class: "anom " + (f.code || ""), text: f.code || "" }),
        h("span", { class: "mono finding-cost",
                    text: f.cost_s + " s / " + fmtTokens(f.cost_tokens) + " tok" }),
        h("span", { class: "anom-detail", text: " " + (f.detail || "") })))));
  }

  const anomalies = trace.anomalies || [];
  if (anomalies.length) {
    kids.push(h("div", { class: "trace-head" }, h("b", { text: "Incidents" })));
    kids.push(h("ul", { class: "trace-anomalies" },
      ...anomalies.slice(0, 12).map((a) => h("li", {},
        h("span", { class: "anom " + (a.what || ""), text: a.what || "" }),
        h("span", { class: "anom-detail", text: a.detail || "" })))));
  }
  return h("aside", { class: "plan-panel" }, ...kids);
}

async function fetchTrace(sessionId) {
  if (!sessionId) return null;
  try { return await api("/api/chats/" + sessionId + "/trace"); }
  catch (e) { return null; }   // jamais bloquant pour le rendu du chat
}

// ---- Jobs list ----

async function renderJobs() {
  const [jobsData, stats] = await Promise.all([
    api("/api/jobs"),
    api("/api/stats").catch(() => null),
  ]);
  const jobs = jobsData.jobs;
  // Stats bar
  const statsBar = stats ? h("div", { class: "stats-bar" },
    stat("jobs", stats.total_jobs + " jobs", "jobs enregistrés au total"),
    stat("chart", fmtTokens(stats.total_tokens) + " tokens", "tokens consommés"),
    stat("coin", fmtCost(stats.total_cost), "coût cumulé"),
    stat("clock", fmtMs(stats.total_time_ms), "temps de calcul cumulé"),
    stats.throughput_tps != null
      ? stat("bolt", (stats.throughput_tps).toFixed(1) + " t/s", "débit moyen") : "",
    ...Object.entries(stats.status_counts || {}).map(([s, c]) =>
      h("span", { class: "badge " + s, text: s + " : " + c })),
  ) : "";
  const rows = jobs.map((j) => h("tr", { onclick: () => { location.hash = "jobs/" + j.job_id; } },
    h("td", { text: j.job_id }),
    h("td", { text: j.kind || "delegate" }),
    h("td", { text: j.project || "—" }),
    h("td", { class: "goal", text: (j.goal || "").slice(0, 80) }),
    h("td", {}, badge(j.status)),
    h("td", { text: j.model ? j.model + " (essai " + (j.attempt || "?") + ")" : "—" }),
    h("td", { class: "mono", text: fmtTokens(j.total_tokens) }),
    h("td", { class: "mono", text: fmtCost(j.total_cost) }),
    h("td", { class: "mono", text: j.attempt_count || "—" }),
    h("td", { text: fmtWhen(j.created_at) }),
    h("td", { text: fmtDur(j.duration) })));
  MAIN.replaceChildren(
    heading("h2", "jobs", "Jobs"),
    statsBar,
    jobs.length === 0 ? h("p", { class: "empty", text: "Aucun job. Passe par « Déléguer »." })
      : h("table", {},
          h("thead", {}, h("tr", {},
            ...["id", "type", "projet", "goal", "état", "modèle", "tokens", "coût", "essais", "créé", "durée"]
              .map((t) => h("th", { text: t })))),
          h("tbody", {}, ...rows)));
  trackJobStatuses(jobs);
  return jobs.some((j) => ACTIVE.includes(j.status));
}

// ---- Job detail ----

let JOB_LOG_SCROLL = { job: null, stick: true, top: 0 };

function renderDiff(diff) {
  const pre = h("pre", { class: "diff" });
  for (const line of (diff || "").split("\n")) {
    const cls = line.startsWith("+") ? "add" : line.startsWith("-") ? "del"
      : line.startsWith("@@") ? "hunk" : "";
    pre.append(h("span", { class: cls, text: line + "\n" }));
  }
  return pre;
}

async function renderJob(jobId) {
  const [status, log] = await Promise.all([
    api("/api/jobs/" + jobId),
    api("/api/jobs/" + jobId + "/log?tail=200")]);
  const active = ACTIVE.includes(status.status);
  // P06: notify when job completes (active -> terminal transition)
  const prev = _jobStatusBefore[jobId];
  if (prev && !active && TERMINAL.includes(status.status) &&
      (!prev || ACTIVE.includes(prev))) {
    notifyJobDone(jobId, status.status);
  }
  _jobStatusBefore[jobId] = status.status;
  const head = h("div", { class: "banner" },
    h("h2", { text: jobId }), badge(status.status),
    h("span", { text: status.model ? "étage " + (status.stage || "?") + " · " +
      status.model + " · essai " + (status.attempt || "?") : "" }),
    status.pull_total ? h("span", { text:
      Math.round(100 * (status.pull_completed || 0) / status.pull_total) + " % · " +
      (status.pull_status || "") }) : "",
    active ? iconBtn("stop", "Annuler ce job", async () => {
      try { await api("/api/jobs/" + jobId + "/cancel", { method: "POST" }); tick(); }
      catch (e) { showError(e); }
    }, "danger") : "");
  const logPre = h("pre", { class: "log", text: log.log || "(vide)" });
  const parts = [head, heading("h3", "terminal", "Log"), logPre];
  if (status.status === "waiting_gpu") {
    // "waiting_gpu" alone leaves the operator guessing, and the wait is up to
    // the chat idle timer -- 10 minutes (E2E audit §5).
    const rt = await api("/api/runtime").catch(() => ({}));
    parts.splice(1, 0, h("div", { class: "chat-notice" },
      rt.held_by === "chat"
        ? h("span", {}, "Le GPU est pris par une session de chat" +
            (rt.loaded ? " (" + rt.loaded + ")" : "") + " — décharge le modèle ",
          h("a", { href: "#runtime", text: "dans l'onglet Modèles" }),
          " pour démarrer tout de suite, sinon ce job attend l'idle timer.")
        : h("span", { text: rt.held_by === "job"
            ? "Le GPU est pris par un autre job : celui-ci démarrera à sa suite."
            : "En attente du GPU." })));
  }
  if (!active) {
    const result = await api("/api/jobs/" + jobId + "/result");
    if (result.ready) {
      parts.push(heading("h3", result.status === "succeeded" ? "ok" : "err",
                         "Verdict"),
        h("div", { class: "banner" }, badge(result.status),
          result.review ? h("span", { text: "reviewer : " + result.review.verdict }) : ""));
      if (result.review && result.review.notes)
        parts.push(h("pre", { class: "log", text: result.review.notes }));
      if (result.draft) {
        const d = result.draft;
        parts.push(heading("h3", "pencil", "Proposition du spec writer"));
        if ((result.warnings || []).length)
          parts.push(h("pre", { class: "log",
            text: result.warnings.map((w) => "⚠ " + w).join("\n") }));
        for (const [name, src] of Object.entries(d.tests))
          parts.push(h("p", {}, h("b", { text: name })),
            h("pre", { class: "log", text: src }));
        parts.push(
          h("p", { text: "Targets : " + (d.target_files.join(", ") || "—") }),
          h("p", { text: "Context : " + (d.context_files.join(", ") || "—") }));
        if ((d.assumptions || []).length)
          parts.push(h("p", { text: "Hypothèses : " + d.assumptions.join(" · ") }));
        parts.push(h("button", { text: "Utiliser ce brouillon", onclick: () => {
          sessionStorage.setItem("draft-prefill", JSON.stringify({
            project: result.project, goal: result.goal, tests: d.tests,
            target_files: d.target_files, context_files: d.context_files }));
          location.hash = "delegate";
        } }));
      }
      if (result.raw)
        parts.push(heading("h3", "warn", "Réponse brute (non parsable)"),
          h("pre", { class: "log", text: result.raw }));
      if (result.diff) {
        const applyBtn = result.status !== "succeeded" ? ""
          : status.applied_at
            ? h("span", { class: "badge succeeded",
                          text: "appliqué le " + fmtWhen(status.applied_at) })
            : iconBtn("save", "Appliquer le diff au working tree", async () => {
                try { await api("/api/jobs/" + jobId + "/apply", { method: "POST" }); tick(); }
                catch (e) { showError(e); }
              });
        // Un `iconLink` pointe vers l'extérieur ; ici le lien télécharge un
        // blob local, d'où le bouton dessiné à la main.
        const dl = h("a", { class: "icon-btn", download: jobId + ".patch",
          href: URL.createObjectURL(new Blob([result.diff])),
          title: "Télécharger le patch", "aria-label": "Télécharger le patch" });
        dl.append(icon("download"));
        parts.push(heading("h3", "diff", "Diff"),
          h("div", { class: "row-actions" },
            applyBtn,
            iconBtn("copy", "Copier le diff", () =>
              navigator.clipboard.writeText(result.diff)),
            dl),
          renderDiff(result.diff));
      }
    } else {
      parts.push(h("p", { class: "empty", text: result.error || "Pas de résultat." }));
    }
  }
  MAIN.replaceChildren(...parts);
  // Live tail. The view re-renders whole every 2 s, so the <pre> is a new
  // element each time and starts at the top of the tail; pin it to the bottom
  // unless the operator scrolled up to read (the same magnet as the chat log,
  // kept across renders because the element is not).
  if (JOB_LOG_SCROLL.job !== jobId)
    JOB_LOG_SCROLL = { job: jobId, stick: true, top: 0 };
  logPre.scrollTop = JOB_LOG_SCROLL.stick ? logPre.scrollHeight
                                          : JOB_LOG_SCROLL.top;
  logPre.addEventListener("scroll", () => {
    JOB_LOG_SCROLL.top = logPre.scrollTop;
    JOB_LOG_SCROLL.stick =
      logPre.scrollHeight - logPre.scrollTop - logPre.clientHeight < 48;
  });
  return active;
}

// ---- Delegate ----

function filePicker(title) {
  const state = { files: [], chosen: new Set() };
  const list = h("div", { class: "files" });
  const filter = h("input", { placeholder: "filtrer…", oninput: draw });
  const manual = h("input", { placeholder: "ou un nouveau chemin, ex. slugify.py" });
  const addBtn = h("button", { class: "ghost", text: "Ajouter", onclick: () => {
    const p = manual.value.trim();
    if (!p) return;
    if (!state.files.includes(p)) state.files.unshift(p);
    state.chosen.add(p);
    manual.value = "";
    draw();
  } });
  // A repo-sized list costs its DOM up front and the operator reads maybe ten
  // lines of it (E2E audit §6). Cap it, say so, and always keep what is
  // already ticked visible -- a selection hidden by the cap is a trap.
  const SHOWN = 60;
  const more = h("p", { class: "empty" });
  function draw() {
    const q = filter.value.toLowerCase();
    const hit = state.files.filter((f) => f.toLowerCase().includes(q));
    const chosen = hit.filter((f) => state.chosen.has(f));
    const rest = hit.filter((f) => !state.chosen.has(f));
    const shown = [...chosen, ...rest.slice(0, Math.max(0, SHOWN - chosen.length))];
    list.replaceChildren(...shown.map((f) => {
      const cb = h("input", { type: "checkbox" });
      cb.checked = state.chosen.has(f);
      cb.addEventListener("change", () =>
        cb.checked ? state.chosen.add(f) : state.chosen.delete(f));
      return h("label", {}, cb, h("span", { text: f }));
    }));
    const hidden = hit.length - shown.length;
    more.textContent = hidden > 0
      ? hidden + " autres fichiers correspondent — affine le filtre" : "";
  }
  const root = h("fieldset", {}, h("legend", { text: title }), filter, list, more,
    h("div", { class: "manual" }, manual, addBtn));
  return { root, set files(v) { state.files = v; state.chosen.clear(); draw(); },
           get chosen() { return [...state.chosen]; },
           set chosen(v) {
             for (const p of v) if (!state.files.includes(p)) state.files.unshift(p);
             state.chosen = new Set(v);
             draw();
           } };
}

async function renderDelegate() {
  const prefill = JSON.parse(sessionStorage.getItem("draft-prefill") || "null");
  sessionStorage.removeItem("draft-prefill");
  const { projects } = await api("/api/projects");
  const project = h("select", {}, ...projects.map((p) => h("option", { text: p })));
  const goal = h("textarea", { rows: 3, placeholder: "Ce que le changement doit accomplir" });
  if (prefill) {
    project.value = prefill.project;
    goal.value = prefill.goal;
  }
  const tests = h("div", {});
  const addTest = (name = "", src = "") => tests.append(h("div", { class: "test-block" },
    h("input", { class: "test-name", placeholder: "test_exemple.py", value: name }),
    h("textarea", { class: "test-src", rows: 10,
      placeholder: "def test_...():\n    assert ...", text: src })));
  if (prefill && Object.keys(prefill.tests).length)
    for (const [name, src] of Object.entries(prefill.tests)) addTest(name, src);
  else addTest();
  const targets = filePicker("Targets (le modèle peut les écrire)");
  const contexts = filePicker("Context (lecture seule)");
  async function loadFiles() {
    try {
      const { files } = await api("/api/repo-files?project=" +
        encodeURIComponent(project.value));
      targets.files = files;
      contexts.files = files;
    } catch (e) { showError(e); }
  }
  project.addEventListener("change", loadFiles);
  if (projects.length) loadFiles().then(() => {
    if (prefill) {
      targets.chosen = prefill.target_files;
      contexts.chosen = prefill.context_files;
    }
  });
  const draftBtn = h("button", { class: "ghost",
    text: "✨ Proposer tests & fichiers (modèle local)", onclick: async () => {
      if (!goal.value.trim()) { showError(new Error("Écris d'abord le goal.")); return; }
      try {
        const r = await api("/api/drafts", { method: "POST", body: JSON.stringify({
          project: project.value, goal: goal.value }) });
        location.hash = "jobs/" + r.job_id;
      } catch (e) { showError(e); }
    } });
  const submit = h("button", { text: "Déléguer", onclick: async () => {
    const testMap = {};
    for (const block of tests.children) {
      const name = block.querySelector(".test-name").value.trim();
      const src = block.querySelector(".test-src").value;
      if (name) testMap[name] = src;
    }
    try {
      const r = await api("/api/jobs", { method: "POST", body: JSON.stringify({
        project: project.value, goal: goal.value, tests: testMap,
        target_files: targets.chosen, context_files: contexts.chosen }) });
      location.hash = "jobs/" + r.job_id;
    } catch (e) { showError(e); }
  } });
  MAIN.replaceChildren(heading("h2", "send", "Déléguer"),
    h("label", { text: "Projet" }), project,
    h("label", { text: "Goal" }), goal,
    draftBtn,
    h("label", { text: "Tests (le juge)" }), tests,
    h("button", { class: "ghost", text: "+ test", onclick: addTest }),
    targets.root, contexts.root, submit);
  return false;
}

// ---- Runtime (llama-server) ----

const fmtGB = (b) => b == null ? "—" : (b / 1e9).toFixed(1) + " GB";

// The catalogue is curated (3 tuned builds, not Ollama's tag list). Surface
// the quant from the GGUF filename so a build doesn't read as a "base" model.
// Ollama-blob paths are a bare sha256 and carry no quant here (GGUF-metadata
// read is the follow-up); named GGUFs (the 35b) show theirs.
const quantOf = (path) => {
  const base = (path || "").split(/[\\/]/).pop();
  const q = base.match(/(UD-)?I?Q\d+(_[A-Z0-9]+)*|BF16|F16|F32/i);
  const mtp = /MTP/i.test(base) ? " · MTP" : "";
  return q ? q[0].toUpperCase() + mtp : mtp.replace(/^ · /, "");
};

// Une fenêtre se lit d'un coup d'œil ou pas du tout : 262144 ne dit rien,
// 256k dit tout.
const fmtCtx = (n) => !n ? "—"
  : (n >= 1e6 ? (n / 1e6).toFixed(n % 1e6 ? 1 : 0) + "M"
              : Math.round(n / 1024) + "k");

const fmtPrice = (m) => m.free ? "gratuit"
  : (m.usd_per_mtok_out == null ? "—"
     : "$" + m.usd_per_mtok_in + " / $" + m.usd_per_mtok_out + " par Mtok");

// La fiche d'un modèle, là où on le choisit. Les mêmes faits que l'onglet
// Modèles : l'onglet sert à comparer, la fiche à vérifier sur quoi on est
// assis sans quitter la session. Lecture seule pour l'instant -- le réglage
// fin (température, échantillonnage) n'a pas encore de place où se ranger,
// et une commande qui ne persiste pas ment.
function modelCard(m, ctxLocal, sessionCtx) {
  if (!m) return h("div", { class: "model-card empty", text: "Modèle inconnu du catalogue." });
  const row = (k, v, cls) => h("div", { class: "mc-row" },
    h("span", { class: "mc-key label-caps", text: k }),
    h("span", { class: "mc-val" + (cls ? " " + cls : ""), text: v }));
  const rows = [
    row("id", m.name, "mono"),
    row("fournisseur", m.route_label || "—"),
    row("fenêtre", m.cloud ? fmtCtx(m.context_length) : fmtCtx(ctxLocal)),
    row("session", sessionCtx ? fmtCtx(sessionCtx) : "défaut"),
    row("outils", m.tools === false ? "non" : "oui"),
  ];
  if (m.cloud) {
    rows.push(row("prix", fmtPrice(m)));
    rows.push(row("source", m.pinned ? "épinglé dans factory.toml"
                                     : "catalogue OpenRouter"));
    if (m.unknown) {
      rows.push(row("alerte", "id absent du registre OpenRouter", "warn"));
    }
  } else {
    rows.push(row("quant", quantOf(m.path) || "—"));
    rows.push(row("poids", m.missing ? "GGUF manquant" : fmtGB(m.size)));
    rows.push(row("args", m.args.join(" ") || "—"));
  }
  // Les deux sections dictées le 29/07. Elles ne sont pas branchées et ne
  // font pas semblant : les réglages de session n'ont nulle part où être
  // rangés (factory.toml ne porte que des profils GLOBAUX), et les repères
  // n'ont de source mesurée que pour trois modèles locaux sur dix-huit.
  return h("div", { class: "model-card-wrap" },
    h("div", { class: "model-card" }, ...rows),
    soonCard("sliders", "Réglages poussés",
      "Température, top-p, top-k, pénalités — par modèle ET par session. "
      + "Il manque d'abord un endroit où ranger un réglage de session : "
      + "factory.toml ne porte que des profils globaux, et une commande qui "
      + "ne persiste pas mentirait."),
    soonCard("chart", "Repères de perf",
      "Sur quoi ce modèle est bon. La seule source à nous (le banc A/B, "
      + "experiments/coder_bench) couvre trois modèles locaux ; importer des "
      + "scores publics serait invérifiable ici. La source se tranche avant "
      + "d'afficher un chiffre."));
}

async function renderRuntime() {
  const [s, cat, quota] = await Promise.all(
    [api("/api/runtime"), runtimeModels(0),
     api("/api/cloud/quota").catch((e) => ({ error: String(e) }))]);
  const models = cat.models;
  // `ev.target` est l'élément CLIQUÉ : sur un bouton dessiné c'est le <svg>,
  // qui n'a pas de `disabled`. `currentTarget` est le bouton, toujours.
  const actNow = async (path, body, ev) => {
    const btn = ev.currentTarget;
    btn.disabled = true;
    try { await api(path, { method: "POST", body: JSON.stringify(body || {}) }); tick(); }
    catch (e) { showError(e); btn.disabled = false; }
  };
  const state = s.loaded ? s.loaded + " chargé"
    : (s.held_by === "job" ? "GPU occupé par un job" : "aucun modèle chargé");
  const parts = [heading("h2", "models", "Modèles"),
    h("div", { class: "banner" },
      badge(s.loaded ? "succeeded" : "dead"),
      h("span", { text: "llama-server · " + state }),
      s.loaded ? iconBtn("eject", "Décharger le modèle de la VRAM",
                         (ev) => actNow("/api/runtime/unload", {}, ev)) : null)];

  // Le palier gratuit est rationné en REQUÊTES, pas en tokens, et un tour
  // d'agent dépense une requête par appel d'outil : sans ce bandeau, le mur
  // arrive sans prévenir et ressemble à une panne.
  if (!quota.error) {
    parts.push(h("div", { class: "banner" },
      badge(quota.free_tier ? "running" : "succeeded"),
      h("span", { text: "OpenRouter · " + (quota.free_tier ? "palier gratuit"
                                                           : "crédits achetés") }),
      h("span", { class: "mono", text: quota.free_rpm + " req/min · "
                  + quota.free_rpd + " req/jour" }),
      h("span", { class: "muted", text: "dépensé : $" + quota.usage_usd }),
      h("span", { class: "muted", text: quota.upgrade_hint })));
  }

  const meta = cat.cloud_catalog || {};
  const stale = meta.stale
    ? " · catalogue hors ligne (dernière réponse connue" +
      (meta.error ? " : " + meta.error : "") + ")"
    : "";
  // Dix-huit modèles à plat obligeaient à lire chaque ligne pour savoir qui
  // les sert. Le regroupement lit `route_label`, qui vient du serveur : la
  // règle de routage n'est écrite qu'une fois, dans `providers`.
  const groups = [];
  for (const m of models) {
    const key = m.route_label || "Autres";
    let g = groups.find((x) => x.key === key);
    if (!g) groups.push(g = { key, cloud: m.cloud, rows: [] });
    g.rows.push(m);
  }
  const groupHead = (g) => {
    const tr = h("tr", { class: "group-row" });
    const head = h("div", { class: "group-head" });
    head.append(icon(g.cloud ? "cloud" : "gpu"),
      h("span", { class: "group-name", text: g.key }),
      h("span", { class: "group-count", text: String(g.rows.length) }));
    tr.append(h("td", { colspan: "8" }, head));
    return tr;
  };
  parts.push(heading("h3", "gpu", "Catalogue"),
    h("div", { class: "banner" },
      h("span", { class: "muted", text: models.filter((m) => m.cloud).length
                  + " modèles cloud · " + models.filter((m) => !m.cloud).length
                  + " locaux" + stale }),
      iconBtn("refresh", "Rafraîchir le catalogue depuis OpenRouter",
              (ev) => actNow("/api/cloud/refresh", {}, ev))),
    h("table", {}, h("thead", {}, h("tr", {},
      ...["modèle", "quant", "contexte", "prix", "outils", "GGUF", "flags", ""]
        .map((t) => h("th", { text: t })))),
      h("tbody", {}, ...groups.flatMap((g) => [groupHead(g),
        ...g.rows.map((m) => h("tr", {},
        h("td", { text: m.label || m.name, title: m.name }),
        h("td", { text: m.cloud ? "cloud" : (quantOf(m.path) || "—") }),
        // Le plafond local est celui de la VRAM ; celui d'un modèle cloud est
        // celui du fournisseur. Les afficher pareil laissait croire que 64k
        // était la limite de tout le monde.
        h("td", { class: "mono",
                  text: m.cloud ? fmtCtx(m.context_length) : fmtCtx(cat.ctx_local),
                  title: m.cloud ? "fenêtre servie par le fournisseur"
                                 : "fenêtre du serveur local" }),
        h("td", { text: m.cloud ? fmtPrice(m) : "local" }),
        h("td", { text: m.tools === false ? "non" : "oui",
                  title: m.tools === false
                    ? "ne sait pas appeler d'outil : refusé pour les sessions agent"
                    : "utilisable en session agent" }),
        // Un modèle cloud n'a pas de VRAM : rien à charger, rien à peser, rien
        // qui puisse manquer sur le disque. Le bouton Charger n'a de sens que
        // pour un GGUF.
        // « distant (OpenRouter) » était écrit en dur : depuis les routes
        // directes, c'est faux pour la moitié du catalogue -- et ChatGPT
        // s'annonçait servi par un agrégateur qu'il ne traverse jamais.
        h("td", { text: m.cloud ? "distant · " + (m.route_label || "cloud")
                                : (m.missing ? "GGUF manquant" : fmtGB(m.size)) }),
        h("td", { text: m.args.join(" ") || "—" }),
        h("td", {}, m.cloud
          ? (m.unknown
             // Un id absent du registre est un 404 en attente : le dire ici
             // coûte une ligne, le découvrir en pleine session coûte un tour.
             ? h("span", { class: "warn", title: "id absent du registre "
                           + "OpenRouter : vérifie l'orthographe dans "
                           + "factory.toml", text: "id inconnu" })
             : h("span", { class: "muted",
                           text: m.pinned ? "épinglé" : "à la demande" }))
          : (m.loaded ? badge("succeeded")
            : (m.missing ? null
               : iconBtn("download", "Charger " + m.name + " en VRAM",
                         (ev) => actNow("/api/runtime/load",
                                        { model: m.name }, ev)))))))]))),
    h("p", { class: "empty", text: models.length ? ""
      : "Aucune entrée [llama_server.models] dans factory.toml." }));

  parts.push(await renderProviders());
  MAIN.replaceChildren(...parts);
  return false;
}

// Les clés ne redescendent jamais du serveur : chaque ligne montre un indice
// (`sk-a...lue`) et d'où vient la clé active, jamais sa valeur.
async function renderProviders() {
  const { providers, credentials } = await api("/api/providers");
  const byKey = Object.fromEntries(credentials.map((c) => [c.name, c]));
  const rows = providers.map((p) => {
    const cred = byKey[p.key] || { set: false };
    const input = h("input", { type: "password", placeholder: cred.set
      ? "remplacer la clé" : "coller la clé", autocomplete: "off" });
    const save = async (value, ev) => {
      ev.target.disabled = true;
      try { await api("/api/providers/key", { method: "POST",
              body: JSON.stringify({ name: p.key, value }) });
            input.value = ""; tick(); }
      catch (e) { showError(e); ev.target.disabled = false; }
    };
    return h("tr", {},
      h("td", { text: p.label }),
      h("td", { class: "mono", text: p.prefix || "(par défaut)",
                title: p.prefix ? "préfixe à écrire devant l'id du modèle"
                                : "les ids sans préfixe passent par OpenRouter" }),
      // Une route navigateur n'a pas de clé à configurer : elle a une session
      // ouverte ou pas, et le studio ne peut pas le savoir d'ici. Dire « non
      // configuré » serait faux la moitié du temps.
      h("td", {}, p.browser
        ? h("span", { class: "muted",
                      title: "connexion dans le profil du navigateur ; "
                             + "py -3.12 harness/chatgpt_web.py login",
                      text: "profil navigateur" })
        : cred.set
        // `badge("succeeded")` affichait le mot « succeeded » : c'est un nom
        // d'état de job, pas ce qu'on veut lire en face d'une clé.
        ? h("span", {}, h("span", { class: "badge succeeded", text: "configurée" }),
            h("span", { class: "mono", text: " " + cred.hint }),
            h("span", { class: "muted",
                        text: cred.source === "env" ? " (variable d'env)"
                                                    : " (secrets.toml)" }))
        : h("span", { class: "muted", text: "non configuré" })),
      h("td", {}, p.browser ? h("span", { class: "muted", text: "—" }) : input),
      h("td", {}, h("span", { class: "actions" },
        p.browser
          ? iconLink("external", "ouvrir ChatGPT et se connecter", p.console)
          : iconBtn("save", "Enregistrer la clé",
                    (ev) => save(input.value, ev)),
        !p.browser && cred.set && cred.source !== "env"
          ? iconBtn("trash", "Effacer la clé", (ev) => save("", ev), "danger")
          : null,
        p.browser ? null
          : iconLink("external", "console " + p.label, p.console))));
  });
  return h("section", {},
    heading("h3", "cloud", "Fournisseurs"),
    h("p", { class: "muted", text: "Les clés sont écrites dans secrets.toml "
      + "(hors dépôt) et ne repartent jamais vers le navigateur. Une variable "
      + "d'environnement exportée avant le lancement l'emporte sur le fichier." }),
    h("table", {}, h("thead", {}, h("tr", {},
      ...["fournisseur", "préfixe", "clé", "", ""].map((t) => h("th", { text: t })))),
      h("tbody", {}, ...rows)));
}

// ---- Projects ----

// Une section pas encore branchée. Elle DIT qu'elle ne l'est pas : un panneau
// vide qui ressemble à une vraie vue se lit comme une panne, et c'est le genre
// de doute qui coûte une session à diagnostiquer.
function soonCard(name, title, note) {
  const head = h("div", { class: "soon-head" });
  head.append(icon(name), h("span", { text: title }),
              h("span", { class: "soon-tag", text: "à venir" }));
  return h("section", { class: "soon-card" }, head,
    h("p", { class: "muted", text: note }));
}

// Quel projet est ouvert, hors du DOM : il est reconstruit à chaque tour de
// polling, donc l'état de l'écran ne peut pas vivre dedans.
let openProject = null;

async function renderProjects() {
  const { projects } = await api("/api/projects");
  const name = h("input", { placeholder: "clé, ex. lucena" });
  const path = h("input", { placeholder: "chemin absolu d'un repo git" });
  const regression = h("textarea", { rows: 3,
    placeholder: "regression_cmd, un argument par ligne (optionnel)\n-m\npytest\n-q" });
  const submit = h("button", { text: "Ajouter", onclick: async () => {
    const cmd = regression.value.split("\n").map((s) => s.trim()).filter(Boolean);
    try {
      await api("/api/projects", { method: "POST", body: JSON.stringify({
        name: name.value.trim(), path: path.value.trim(),
        regression_cmd: cmd.length ? cmd : null }) });
      tick();
    } catch (e) { showError(e); }
  } });
  // Un projet s'ouvre. Le détail est encore en attente côté serveur (aucune
  // route ne rend un dépôt, un état git ou des instructions), donc chaque
  // section annonce ce qu'elle contiendra plutôt que de faire semblant.
  const detail = h("div", { class: "project-detail", hidden: "" });
  const open = (name) => {
    // La vue se redessine toute seule toutes les dix secondes et remplace
    // MAIN : sans mémoire, la fiche ouverte se refermait sous les doigts.
    openProject = name;
    detail.hidden = false;
    detail.replaceChildren(
      h("div", { class: "banner" },
        h("h3", { text: name }),
        iconBtn("stop", "Fermer la fiche",
                () => { detail.hidden = true; openProject = null; })),
      soonCard("git", "Dépôt et vue git",
        "Branche courante, état de l'arbre, derniers commits, et de quoi "
        + "brancher ou revenir en arrière sans passer par un terminal."),
      soonCard("file", "Instructions du projet",
        "Le CLAUDE.md / agent.md que les sessions de ce projet reçoivent, "
        + "éditable ici."),
      soonCard("ok", "Suite de régression",
        "La commande déclarée dans factory.toml, son dernier verdict, et un "
        + "bouton pour la relancer."),
      soonCard("plan", "TODO du projet",
        "La liste de tâches par projet, alimentée par les sessions."));
  };
  MAIN.replaceChildren(heading("h2", "projects", "Projets"),
    projects.length === 0 ? h("p", { class: "empty", text: "Aucun projet." })
      : h("table", {}, h("tbody", {},
          ...projects.map((p) => h("tr", { onclick: () => open(p) },
            h("td", {}, h("span", { class: "group-head" },
              icon("projects"), h("span", { text: p })))),
          ))),
    detail,
    h("fieldset", { class: "add-project" },
      h("legend", { text: "Ajouter un projet (factory.toml)" }),
      h("label", { text: "Nom (clé)" }), name,
      h("label", { text: "Chemin" }), path,
      h("label", { text: "Suite de régression" }), regression,
      submit));
  if (openProject && projects.includes(openProject)) open(openProject);
  return false;
}

// ---- Benchmarks ----

// L'onglet existe, la vue est annoncée, rien n'est branché. Ce qui bloque
// n'est pas l'écran mais la SOURCE : le banc A/B qu'on possède
// (experiments/coder_bench/runs/*/report.json) ne couvre que trois modèles
// locaux, et importer des scores publics donnerait des chiffres invérifiables
// depuis cette machine. Choisir la source vient avant tracer la courbe.
async function renderBenchmarks() {
  MAIN.replaceChildren(heading("h2", "chart", "Benchmarks"),
    h("p", { class: "muted", text: "Comparer les modèles sur des tâches "
      + "réelles, pas sur des scores annoncés ailleurs." }),
    soonCard("chart", "Banc A/B des codeurs",
      "Les runs de experiments/coder_bench : réussite par barreau, coût, "
      + "tours par tâche. Mesuré ici, sur nos tâches — trois modèles locaux "
      + "pour l'instant."),
    soonCard("cloud", "Modèles cloud",
      "Quinze modèles au catalogue, aucun passé au banc. À décider : les "
      + "faire tourner, ou n'afficher que ce qu'on observe en session (taux "
      + "d'échec d'outil, tours par tâche)."),
    soonCard("clock", "Vitesse et latence",
      "Tokens/s et time-to-first-token par modèle et par lane. Les chiffres "
      + "existent déjà par session ; il manque leur agrégation."));
  return false;
}

// ---- Dashboard ----

async function renderDashboard() {
  const { jobs } = await api("/api/jobs");
  const byStatus = {};
  const byModel = {};
  let durSum = 0, durN = 0;
  for (const j of jobs) {
    byStatus[j.status] = (byStatus[j.status] || 0) + 1;
    if (j.model && TERMINAL.includes(j.status)) {
      byModel[j.model] = byModel[j.model] || { ok: 0, total: 0 };
      byModel[j.model].total += 1;
      if (j.status === "succeeded") byModel[j.model].ok += 1;
    }
    if (j.duration != null) { durSum += j.duration; durN += 1; }
  }
  const failures = jobs.filter((j) =>
    ["failed", "rejected", "error", "dead"].includes(j.status)).slice(0, 5);
  const card = (name, value, label) => h("div", { class: "card" },
    h("div", { class: "card-head with-ico" }, icon(name),
      h("span", { text: label })),
    h("b", { text: value }));
  MAIN.replaceChildren(
    heading("h2", "chart", "Dashboard"),
    h("div", { class: "cards" },
      card("jobs", String(jobs.length), "jobs au total"),
      card("ok", String(byStatus.succeeded || 0), "succès"),
      card("clock", fmtDur(durN ? durSum / durN : null), "durée moyenne")),
    heading("h3", "models", "Par modèle"),
    h("table", {}, h("thead", {}, h("tr", {},
      ...["modèle", "succès", "jobs"].map((t) => h("th", { text: t })))),
      h("tbody", {}, ...Object.entries(byModel).map(([m, s]) =>
        h("tr", {}, h("td", { text: m }),
          h("td", { text: Math.round(100 * s.ok / s.total) + " %" }),
          h("td", { text: String(s.total) }))))),
    heading("h3", "err", "Derniers échecs"),
    ...failures.map((j) => h("p", {},
      h("a", { href: "#jobs/" + j.job_id, text: j.job_id }), " ", badge(j.status),
      h("span", { text: " " + (j.goal || "").slice(0, 60) }))));
  return false;
}

// ---- Chat ----

// ---- Chat metrics line ----

// One renderer for live "done" payloads and saved m.metrics: token counts
// against the context window, and a warning the moment the window is the
// reason a reply looks short or cut.
// What the prompt weighed, not what the server had to evaluate: on a stable
// prefix the KV cache drives prefill_count to ~0 (prompt_n 1 for a 1103-token
// prompt, measured 2026-07-22), which used to empty the gauge on every turn
// after the first. Old sessions carry no prompt_count -- fall back.
function promptTokens(met) {
  return met.prompt_count || met.prefill_count || 0;
}

function setChatMeta(el, met) {
  if (!met || !met.tokens_per_s) return;
  let s = Math.round(met.tokens_per_s) + " tok/s";
  if (met.prefill_tokens_per_s)
    s += " · prefill " + Math.round(met.prefill_tokens_per_s) + " tok/s";
  if (met.ttft_s != null) s += " · ttft " + Number(met.ttft_s).toFixed(1) + " s";
  let warn = false;
  if (met.num_ctx && promptTokens(met) > 0) {
    const used = promptTokens(met) + (met.eval_count || 0);
    s += " · ctx " + used + "/" + met.num_ctx +
         " (" + Math.round((100 * used) / met.num_ctx) + " %)";
    if (used >= met.num_ctx) {
      s += " · ⚠ contexte dépassé — le serveur tronque le début de l'historique";
      warn = true;
    } else if (used >= 0.85 * met.num_ctx) {
      s += " · ⚠ contexte presque plein";
      warn = true;
    }
  }
  if (met.thinking_s || met.thinking_chars) {
    // eval_count mixes reasoning and answer tokens; split it by the char
    // share of each phase so the durations come with a ~token estimate.
    const total = (met.thinking_chars || 0) + (met.content_chars || 0);
    const share = total ? (met.content_chars || 0) / total : 1;
    const writeTok = Math.round((met.eval_count || 0) * share);
    const thinkTok = (met.eval_count || 0) - writeTok;
    const rate = (tok, sec) => sec > 0 ? ", " + Math.round(tok / sec) + " tok/s" : "";
    s += " · réflexion " + Number(met.thinking_s || 0).toFixed(1) +
         " s (~" + thinkTok + " tok" + rate(thinkTok, met.thinking_s) +
         ") · rédaction " + Number(met.writing_s || 0).toFixed(1) +
         " s (~" + writeTok + " tok" + rate(writeTok, met.writing_s) + ")";
  }
  if (met.done_reason === "length") {
    s += " · ⚠ réponse coupée (limite de génération)";
    warn = true;
  }
  el.textContent = s;
  el.classList.toggle("chat-warn", warn);
}

// ---- Live status line (state · elapsed · ≈tok/s) ----

let STATUS = null;        // status line of the open chat, set by renderChat
let STREAM_ABORT = null;  // AbortController of the stream in flight

function agentStatus() {
  const el = h("div", { class: "chat-status" });
  el.hidden = true;
  let label = "", t0 = 0, chars = 0, timer = null;
  // ctxBase/ctxMax: tokens already in the window before this turn and the
  // session's num_ctx; renderChat sets them so the line can show live usage.
  // charsPerTok comes from the session's calibration (measured chars/token,
  // ~2.65 on French chats) -- the old fixed /3 underestimated tokens, so the
  // displayed ctx % lagged reality.
  const self = { el, ctxBase: 0, ctxMax: 0, charsPerTok: 3 };
  const draw = () => {
    const dt = (Date.now() - t0) / 1000;
    const dur = dt < 60 ? Math.floor(dt) + " s"
      : Math.floor(dt / 60) + " min " + Math.floor(dt % 60) + " s";
    const speed = chars && dt > 2
      ? " · ≈ " + Math.round(chars / self.charsPerTok / dt) + " tok/s" : "";
    let ctx = "";
    if (self.ctxMax) {
      const used = self.ctxBase + Math.round(chars / self.charsPerTok);
      const pct = Math.round((100 * used) / self.ctxMax);
      // >=100 % is real: the window overflows mid-turn and the server silently
      // drops the oldest tokens (where the system prompt lives).
      ctx = " · ctx ~" + pct + " %" + (pct >= 95 ? " ⚠ saturé" : "");
    }
    el.textContent = "⏳ " + label + " — " + dur + speed + ctx;
  };
  self.start = (l) => {
    label = l; t0 = Date.now(); chars = 0;
    el.hidden = false; el.className = "chat-status";
    clearInterval(timer); timer = setInterval(draw, 1000); draw();
  };
  self.set = (l) => { label = l; draw(); };
  self.feed = (n) => { chars += n; };
  self.stop = (text, cls) => {
    clearInterval(timer); timer = null;
    if (text) { el.textContent = text; el.className = "chat-status " + cls; }
    else el.hidden = true;
  };
  return self;
}

// Why the agent stopped, in words the operator can act on (TODO chat #6).
const STOPPED_TEXTS = {
  iteration_limit: "⛔ Agent arrêté : limite d'outils pour ce message. " +
    "Envoie « continue » pour reprendre.",
  repetition: "⛔ Génération interrompue : le modèle tournait en boucle " +
    "(sortie dégénérée, contexte probablement saturé). La réponse partielle " +
    "est conservée — reformule ou réduis la tâche.",
};

// ---- NDJSON stream renderer (chat and agent) ----

async function streamChat(url, body, log) {
  // The session this stream belongs to: an approval panel spawned mid-stream
  // must bind to it at creation (reading .chat-content from the document raced
  // renders and produced null or a stale id -> POST /api/chats//approve).
  const sessionId = (url.match(/\/api\/chats\/(c_\w+)\//) || [])[1];
  STREAM_ABORT = new AbortController();
  // Before the first delta nothing has been "thought" yet: the time goes to
  // loading the model and evaluating the prompt. The first event switches
  // the label to the real phase.
  if (STATUS) STATUS.start("chargement modèle + prompt");
  let resp;
  try {
    // body === null: attaching to a turn already running server-side (after a
    // refresh). Same NDJSON, same renderer below -- only the verb differs.
    resp = await fetch(url, body === null ? {
      method: "GET",
      signal: STREAM_ABORT.signal,
    } : {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Factory-Token": TOKEN },
      body: JSON.stringify(body),
      signal: STREAM_ABORT.signal,
    });
  } catch (e) {
    if (STATUS) STATUS.stop("⚠ " + e.message, "chat-error");
    throw e;
  }
  if (!resp.ok) {
    const data = await resp.json();
    if (STATUS) STATUS.stop("⚠ " + data.error, "chat-error");
    throw new Error(data.error + ": " + data.detail);
  }
  // Autoscroll, batched. Writing log.scrollTop forces a synchronous layout of
  // the whole log, and feed() ran it once per streamed delta: measured
  // 2026-07-22 at 11.2 ms per delta on a 4.3k-node session (Playwright, real
  // studio). At 60 tok/s that is ~66 % of the main thread spent in layout, and
  // it is why every click in the studio felt slow while a reply streamed.
  // Reading scrollHeight per delta was NOT the cost -- measured identical with
  // the read removed; the write is. One rAF per frame instead: 11.2 ms -> 0.
  //
  // Stickiness now comes from a scroll listener rather than a per-delta
  // measurement, which also gives the magnet behaviour: scrolling up detaches,
  // coming back to the bottom re-attaches.
  // Seeded from where the operator actually IS, not hardcoded true: an agent
  // turn is a chain of streamChat calls (one per tool round-trip), and starting
  // each one "sticky" yanked a reader who had scrolled up back to the bottom
  // at every tool call.
  let sticky = log.scrollHeight - log.scrollTop - log.clientHeight < 48;
  const onScroll = () => {
    sticky = log.scrollHeight - log.scrollTop - log.clientHeight < 48;
  };
  log.addEventListener("scroll", onScroll, { passive: true });
  let scrollQueued = false;
  const stick = () => {
    if (!sticky || scrollQueued) return;
    scrollQueued = true;
    requestAnimationFrame(() => {
      scrollQueued = false;
      if (sticky) log.scrollTop = log.scrollHeight;
    });
  };

  let reply = null;
  let chip = null;
  let stripBlocks = null;
  const activity = activityLine(log);
  log.append(activity.el);
  const bubble = () => {
    if (!reply) {
      reply = chatBubble("assistant", "", undefined, Date.now() / 1000);
      stripBlocks = callBlockStripper();
      log.append(reply.bubble);
    }
    return reply;
  };
  // A bubble stops growing at a tool call, an approval gate, or the end of the
  // turn: that is when its raw text becomes Markdown. One parse per bubble.
  const seal = () => {
    if (reply && reply.text.textContent) mdRender(reply.text, reply.text.textContent);
  };
  const feed = (line) => {
    if (!line.trim()) return;
    const msg = JSON.parse(line);
    if (msg.thinking) {
      const r = bubble();
      r.think.hidden = false;
      r.thinkText.textContent += msg.thinking;
      activity.show("think", "reflechit…", "");
      if (STATUS) { STATUS.set("réflexion"); STATUS.feed(msg.thinking.length); }
    } else if (msg.chunk) {
      const r = bubble();
      activity.show("pencil", "redige la reponse", "");
      r.text.textContent += stripBlocks(msg.chunk);
      if (STATUS) { STATUS.set("rédaction"); STATUS.feed(msg.chunk.length); }
    } else if (msg.tool_call) {
      seal();
      const d = describeCall(msg.tool_call.name, msg.tool_call.arguments);
      activity.show(d.icon, d.verb, d.target);
      chip = toolChip(msg.tool_call.name, msg.tool_call.arguments, "",
        Date.now() / 1000);
      chip.open = false;
      log.append(chip);
      reply = null;
      if (STATUS) STATUS.set("outil : " + msg.tool_call.name);
    } else if (msg.tool_result) {
      if (chip) chip.querySelector(".chat-tool-out").textContent =
        msg.tool_result.output;
      chip = null;
      if (STATUS) STATUS.set("réflexion");
    } else if (msg.context_warning) {
      // Contexte presque plein : afficher une alerte explicite avec barre de progression
      const w = msg.context_warning;
      const pct = Math.round(100 * (w.used || 0) / (w.num_ctx || 1));
      const color = pct >= 95 ? "#ff4444" : pct >= 80 ? "#ffaa00" : "#88ccff";
      const bar = h("div", { class: "chat-context-bar" },
        h("div", { class: "chat-context-bar-bg" },
          h("div", { class: "chat-context-bar-fill", style: "width:" + pct + "%;background:" + color })),
        h("span", { text: (pct >= 95 ? "🔴" : pct >= 80 ? "🟡" : "🔵") + " Contexte : " + pct + "% (" + w.used + "/" + w.num_ctx + " tok, " + (w.evicted || 0) + " expulsés)" }));
      log.append(bar);
    } else if (msg.notice) {
      log.append(h("div", { class: "chat-notice", text: "⚠ " + msg.notice }));
    } else if (msg.compacting) {
      if (STATUS) STATUS.set("compactage du contexte");
    } else if (msg.approval_needed) {
      seal();
      log.append(approvalPanel(msg.approval_needed, log, sessionId));
      reply = null;
      if (STATUS) STATUS.stop("⏸ en attente d'approbation", "chat-paused");
    } else if (msg.done) {
      activity.flush();
      activity.el.remove();
      seal();
      if (reply) setChatMeta(reply.meta, msg);
      if (STATUS) {
        if (msg.stopped) STATUS.stop(
          STOPPED_TEXTS[msg.stopped] || "⛔ arrêté : " + msg.stopped,
          "chat-stopped");
        else STATUS.stop("✓ terminé" + (msg.tokens_per_s
          ? " · " + Math.round(msg.tokens_per_s) + " tok/s" : ""), "chat-done");
      }
    } else if (msg.error) {
      activity.flush();
      activity.el.remove();
      const r = bubble();
      seal();
      r.meta.textContent = "erreur : " + msg.error;
      r.meta.classList.add("chat-error");
      if (STATUS) STATUS.stop("⚠ erreur : " + msg.error, "chat-error");
    }
    stick();
  };
  try {
    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      const lines = buf.split("\n");
      buf = lines.pop();
      lines.forEach(feed);
    }
    feed(buf);
  } catch (e) {
    if (e.name === "AbortError") {
      if (STATUS) STATUS.stop("⏹ arrêté par l'opérateur — le partiel est sauvegardé",
        "chat-stopped");
      return;
    }
    throw e;
  } finally {
    STREAM_ABORT = null;
    log.removeEventListener("scroll", onScroll);
  }
}

// ---- Agent mode helpers ----

// Epoch seconds -> local wall-clock; every chat/log line carries one so a
// session can be read as a timeline (and session-audit can cite times).
function fmtTs(ts) {
  return ts ? new Date(ts * 1000).toLocaleTimeString("fr-FR") : "";
}

// One live line instead of a stack of blobs. Rewritten in place, at most ten
// times a second: rendering per delta is the cost pattern already measured at
// 11.5 ms/token (see chatBubble). The end of a turn forces a last paint, so a
// late update is never swallowed by the window.
function activityLine(log) {
  const ico = h("span", { class: "activity-ico" });
  const label = h("span", { class: "activity-label" });
  // Hidden until the first event: an empty line still takes its padding, and
  // `:empty` never matches a node that always holds its two spans.
  const el = h("div", { class: "activity", hidden: true }, ico, label);
  let pending = null, queued = false, lastPaint = 0;

  const paint = () => {
    queued = false;
    lastPaint = Date.now();
    if (!pending) return;
    el.hidden = false;
    ico.replaceChildren(icon(pending.icon));
    label.textContent = [pending.verb, pending.target]
      .filter(Boolean).join(" ");
    // Saying what happens now only helps if it sits where the eye is: append
    // moves the node, so the line stays under the bubble it describes.
    if (el.parentNode === log && el.nextSibling) log.append(el);
    pending = null;
  };
  const schedule = () => {
    if (queued) return;
    const wait = Math.max(0, 100 - (Date.now() - lastPaint));
    queued = true;
    setTimeout(paint, wait);
  };
  return {
    el,
    show(iconName, verb, target) {
      pending = { icon: iconName, verb, target };
      schedule();
    },
    flush() { if (pending) paint(); },
  };
}

function toolChip(name, args, output, ts) {
  const when = fmtTs(ts);
  const summaryText = "▸ " + (when ? when + " · " : "") + name + " " +
    JSON.stringify(args || {}).slice(0, 80);
  return h("details", { class: "chat-tool" },
    h("summary", { text: summaryText }),
    h("pre", { class: "chat-tool-out", text: output || "…" }));
}

function approvalPanel(call, log, sessionId) {
  const args = call.arguments || {};
  let detail;
  if (call.name === "run_command") detail = "$ " + (args.command || "");
  else if (call.name === "edit_file")
    detail = args.path + "\n--- old\n" + (args.old || "") +
             "\n+++ new\n" + (args.new || "");
  else if (call.name === "write_file")
    detail = args.path + "\n" + (args.content || "");
  else detail = JSON.stringify(args, null, 2);
  const panel = h("div", { class: "chat-approval" },
    h("b", { text: "L'agent veut exécuter : " + call.name }),
    h("pre", { text: detail }));
  const decide = (approved) => async () => {
    panel.querySelectorAll("button").forEach((b) => { b.disabled = true; });
    try { await streamChat("/api/chats/" + sessionId + "/approve",
      { approved }, log); panel.remove(); } catch (e) { showError(e); }
  };
  panel.append(h("div", { class: "buttons" },
    h("button", { text: "Approuver", onclick: decide(true) }),
    h("button", { class: "ghost", text: "Refuser", onclick: decide(false) })));
  return panel;
}

// ---- Markdown rendering (parser: /static/markdown.js) ----

// The XML call block a model writes in prose is streamed as content deltas and
// the stored message is cleaned of it (parse_text_tool_calls). The live bubble
// must match the stored text, or it shows raw <function=...> tags the reload
// hides. Stateful because a block can span several streamed chunks.
function callBlockStripper() {
  let open = "";   // "<function=" or "<tool_call>" while inside a block
  return (text) => {
    if (!text) return text;
    if (open) {
      const close = open === "<function=" ? "</function>" : "</tool_call>";
      const end = text.indexOf(close);
      if (end < 0) return "";
      open = "";
      return text.slice(end + close.length);
    }
    const fi = text.indexOf("<function=");
    const ti = text.indexOf("<tool_call>");
    let cut = -1, closer = "";
    if (fi >= 0 && (ti < 0 || fi < ti)) { cut = fi; closer = "</function>"; }
    else if (ti >= 0) { cut = ti; closer = "</tool_call>"; }
    if (cut < 0) return text;
    const end = text.indexOf(closer, cut);
    if (end < 0) {
      open = closer === "</function>" ? "<function=" : "<tool_call>";
      return text.slice(0, cut);
    }
    return text.slice(0, cut) + text.slice(end + closer.length);
  };
}

function mdSpans(el, spans) {
  for (const s of spans) {
    if (s.t === "code") el.append(h("code", { text: s.v }));
    else if (s.t === "strong") el.append(h("strong", { text: s.v }));
    else el.append(document.createTextNode(s.v));
  }
  return el;
}

// Builds elements, never innerHTML: this text is written by a model and read
// from files on disk, so it must never be able to become markup.
function mdRender(el, src) {
  el.textContent = "";
  for (const b of mdParse(src)) {
    if (b.type === "code") {
      el.append(h("pre", { class: "chat-code" }, h("code", { text: b.text })));
    } else if (b.type === "heading") {
      // A bubble is not a page: # is the biggest thing IN it, not on screen.
      el.append(mdSpans(h("h" + Math.min(b.level + 2, 6)), b.spans));
    } else if (b.type === "list") {
      const list = h(b.ordered ? "ol" : "ul");
      for (const item of b.items) list.append(mdSpans(h("li"), item));
      el.append(list);
    } else {
      el.append(mdSpans(h("p"), b.spans));
    }
  }
  // Raw text needs pre-wrap to keep its newlines; rendered blocks bring their
  // own spacing and would double it.
  el.classList.add("md");
}

function chatBubble(role, content, thinking, ts) {
  const meta = h("div", { class: "chat-meta" });
  const think = h("details", { class: "chat-think" },
    h("summary", { text: "Réflexion" }),
    h("div", { class: "chat-think-text", text: thinking || "" }));
  think.hidden = !thinking;
  const body = h("div", { class: "chat-text", text: content });
  // A stored message is complete, so it is formatted on sight. A streaming one
  // is created empty and stays raw text until the turn ends: re-parsing per
  // delta is the cost pattern that made the studio slow (11.5 ms/token), and
  // half-written Markdown has no meaning anyway.
  if (content) mdRender(body, content);
  // L'horodatage est le VOISIN du texte, pas un flottant au-dessus de lui :
  // une bulle se dimensionne au plus juste, donc un flottant à droite ne
  // tenait jamais à côté d'un prompt court et le poussait à la ligne. Tous les
  // prompts s'affichaient sur deux lignes, « ok » compris.
  const row = h("div", { class: "chat-row" }, body,
    h("span", { class: "chat-ts", text: fmtTs(ts) }));
  const bubble = h("div", { class: "chat-msg " + role }, think, row, meta);
  return { bubble, text: body, meta,
           think, thinkText: think.querySelector(".chat-think-text") };
}

// Deux listes plutôt qu'une. À dix-huit modèles, un menu plat oblige à
// connaître son id par cœur pour retrouver le sien : on choisit le fournisseur,
// puis le modèle. La règle de routage vient du serveur (`route`, `route_label`)
// et n'est pas redécoupée ici -- un parseur d'id dupliqué en JS est exactement
// ce que le sigil `@` a été conçu pour éviter.
function modelPicker(models, { value = "", barred = () => false,
                               onchange = null } = {}) {
  const routes = [...new Map(models.map((m) => [m.route, m.route_label]))];
  const routeSel = h("select", { class: "pick-route",
    title: "fournisseur" }, ...routes.map(
      ([r, l]) => h("option", { value: r, text: l })));
  const modelSel = h("select", { class: "pick-model", title: "modèle" });
  const current = models.find((m) => m.name === value);
  if (routes.length) routeSel.value = current ? current.route : routes[0][0];

  const fill = (want) => {
    const rows = models.filter((m) => m.route === routeSel.value);
    modelSel.replaceChildren(...rows.map((m) => {
      const off = barred(m);
      const opt = h("option", { value: m.name, title: m.name,
        text: (m.label || m.name) + (off ? " — sans outils" : "") });
      if (off) opt.disabled = true;   // le serveur le refuse aussi
      return opt;
    }));
    const pick = rows.find((m) => m.name === want && !barred(m))
      || rows.find((m) => !barred(m));
    if (pick) modelSel.value = pick.name;
  };
  fill(value);
  routeSel.addEventListener("change", () => {
    fill(null);
    if (onchange) onchange(modelSel.value);
  });
  if (onchange) {
    modelSel.addEventListener("change", () => onchange(modelSel.value));
  }
  return { routeSel, modelSel,
           refresh: () => fill(modelSel.value),
           get value() { return modelSel.value; } };
}

async function renderChat(sessionId) {
  const [{ sessions }, runtimeCat, { toolsets }, runtime, projectList]
    = await Promise.all(
      [api("/api/chats"), runtimeModels(), api("/api/toolsets"),
       api("/api/runtime"), api("/api/projects").catch(() => ({ projects: [] }))]);
  const models = runtimeCat.models;
  const installed = models.map((m) => m.name);

  const kindSel = h("select", { class: "chat-new-kind" },
    h("option", { value: "chat", text: "chat" }),
    h("option", { value: "agent", text: "agent" }));
  const modeSel = h("select", { class: "chat-new-mode", hidden: "" },
    h("option", { value: "approve", text: "approbation" }),
    h("option", { value: "auto", text: "auto" }));
  const toolsetSel = h("select", { class: "chat-new-toolset", hidden: "" },
    ...toolsets.map((t) => h("option", { value: t, text: t })));
  toolsetSel.value = "base";
  // Un projet est un NOM : le serveur le résout en chemin. Le champ workspace
  // libre reste, pour ce qui n'est pas déclaré dans factory.toml.
  const projSel = h("select", { class: "chat-new-project", hidden: "",
      title: "projet (optionnel)" },
    h("option", { value: "", text: "— sans projet —" }),
    ...(projectList.projects || []).map(
      (p) => h("option", { value: p, text: p })));
  const wsInput = h("input", { class: "chat-new-ws", hidden: "",
    placeholder: "workspace (défaut : la factory)" });
  // Le modèle n'était PAS choisi à la création : la session partait sur le
  // premier de la liste, alphabétiquement.
  const newPick = modelPicker(models, {
    barred: (m) => kindSel.value === "agent" && m.tools === false });
  kindSel.addEventListener("change", () => {
    const agent = kindSel.value === "agent";
    modeSel.hidden = !agent;
    toolsetSel.hidden = !agent;
    projSel.hidden = !agent;
    wsInput.hidden = !agent || !!projSel.value;
    newPick.refresh();
  });
  projSel.addEventListener("change", () => {
    wsInput.hidden = kindSel.value !== "agent" || !!projSel.value;
  });
  const createSession = async () => {
    const model = newPick.value;
    if (!model) {
      showError(new Error(kindSel.value === "agent"
        ? "Aucun modèle capable d'appeler des outils chez ce fournisseur : change de fournisseur, ou mets tools = true sur une entrée [llama_server.models] de factory.toml."
        : "Aucun modèle : ajoute une entrée [llama_server.models] dans factory.toml."));
      return;
    }
    const body = { model };
    if (kindSel.value === "agent") {
      body.kind = "agent";
      body.mode = modeSel.value;
      body.toolset = toolsetSel.value;
      if (projSel.value) body.project = projSel.value;
      else if (wsInput.value.trim()) body.workspace = wsInput.value.trim();
    }
    try {
      const s = await api("/api/chats", { method: "POST",
        body: JSON.stringify(body) });
      location.hash = "chat/" + s.session_id;
    } catch (e) { showError(e); }
  };
  // Le paramétrage venait AVANT le bouton : sept contrôles à traverser pour
  // ouvrir une session, alors que neuf fois sur dix les valeurs par défaut
  // conviennent. On demande d'abord, on règle ensuite. (Dicté le 29/07.)
  const params = h("div", { class: "chat-new-params", hidden: "" },
    kindSel, newPick.routeSel, newPick.modelSel, modeSel, toolsetSel,
    projSel, wsInput,
    h("div", { class: "chat-new-actions" },
      h("button", { text: "Créer", onclick: createSession }),
      h("button", { class: "ghost", text: "Annuler", onclick: () => {
        params.hidden = true; openBtn.hidden = false;
      } })));
  const openBtn = h("button", { class: "chat-new-open", onclick: () => {
    params.hidden = false; openBtn.hidden = true;
  } }, icon("plus"), h("span", { text: "Nouvelle session" }));
  const newForm = h("div", { class: "chat-new" }, openBtn, params);

  const sessionRow = (s) => h("div", {
    class: "chat-session" + (s.session_id === sessionId ? " current" : ""),
    onclick: () => { location.hash = "chat/" + s.session_id; } },
    h("span", { class: "chat-title", title: s.title || "",
      text: (s.kind === "agent" ? "⚙ " : "") + (s.title || "(vide)") }),
    h("span", { class: "chat-acts" },
      h("button", { class: "ghost", text: "✎", title: "Renommer",
        onclick: async (ev) => {
          ev.stopPropagation();
          // Pré-rempli avec le nom courant : renommer, c'est presque toujours
          // corriger le nom auto, pas en écrire un de zéro. Vider le champ le
          // rend à la machine.
          const next = window.prompt(
            "Nom de la session (vide = nom auto-généré)", s.title || "");
          if (next === null) return;
          try {
            await api("/api/chats/" + s.session_id + "/title", { method: "POST",
              body: JSON.stringify({ title: next }) });
            tick();
          } catch (e) { showError(e); }
        } }),
      h("button", { class: "ghost", text: "×", title: "Supprimer",
        onclick: async (ev) => {
          ev.stopPropagation();
          try {
            await api("/api/chats/" + s.session_id + "/delete", { method: "POST",
              body: JSON.stringify({}) });
            if (s.session_id === sessionId) location.hash = "chat";
            else tick();
          } catch (e) { showError(e); }
        } })));

  // Groupé par projet, l'ordre des groupes suivant la session la plus récente
  // de chacun : le projet sur lequel on travaille reste en haut sans qu'on ait
  // à le choisir. `sessions` arrive déjà trié par date décroissante.
  const groups = new Map();
  for (const s of sessions) {
    const key = s.project || "Hors projet";
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(s);
  }
  const list = h("div", { class: "chat-sessions" },
    ...[...groups].map(([name, rows]) => h("div", { class: "chat-group" },
      h("div", { class: "chat-group-head label-caps" },
        h("span", { class: "chat-group-name", text: name }),
        h("span", { class: "chat-group-count", text: String(rows.length) })),
      ...rows.map(sessionRow))));

  // Injecter la liste des sessions dans la bannière (déjà dans le HTML).
  // Le bloc « nouvelle session » est un frère de la liste, pas son premier
  // enfant : dans la liste, il défilait hors de vue dès quelques sessions.
  const sessionsListEl = document.getElementById("chat-sessions-list");
  if (sessionsListEl) {
    sessionsListEl.replaceChildren(
      h("div", { class: "chat-new-block" }, newForm), list);
  }

  let right;
  let scrollBtn = null;
  let logEl = null;
  STATUS = null;
  // Hoisted: the plan slot below lives outside the else branch but needs the
  // session kind. `const session` inside the else was invisible there and every
  // renderChat() died on "session is not defined" (défaut 1, 2026-08-19).
  let session = null;
  if (!sessionId) {
    right = h("div", { class: "chat-main" },
      h("p", { class: "empty", text: installed.length
        ? "Choisis une session ou crées-en une nouvelle."
        : "Aucun modèle : ajoute une entrée [llama_server.models] dans factory.toml." }));
  } else {
    session = await api("/api/chats/" + sessionId);
    let modeCtl = "";
    const agentSession = session.kind === "agent";
    const pick = modelPicker(models, {
      value: session.model,
      barred: (m) => agentSession && m.tools === false,
      onchange: async (name) => {
        if (name === session.model) return;
        try {
          await api("/api/chats/" + sessionId + "/model", { method: "POST",
            body: JSON.stringify({ model: name }) });
          session.model = name;
        } catch (e) { showError(e); }
      } });
    const model = pick.modelSel;
    if (session.kind === "agent") {
      // Deux états, donc un interrupteur dessiné plutôt qu'une liste de deux
      // lignes : le bouclier retient chaque appel, l'éclair les laisse passer.
      // Le libellé reste dans le `title` -- un pictogramme seul est une devinette.
      let mode = session.mode || "approve";
      modeCtl = h("button", { class: "icon-btn mode-btn" });
      const paint = () => {
        modeCtl.classList.toggle("mode-auto", mode === "auto");
        modeCtl.title = mode === "auto"
          ? "Mode auto : l'agent exécute sans demander. Cliquer pour repasser en approbation."
          : "Mode approbation : chaque appel d'outil attend ton feu vert. Cliquer pour passer en auto.";
        modeCtl.setAttribute("aria-label", modeCtl.title);
        modeCtl.replaceChildren(icon(mode === "auto" ? "bolt" : "shield"));
      };
      paint();
      modeCtl.addEventListener("click", async () => {
        const next = mode === "auto" ? "approve" : "auto";
        try {
          await api("/api/chats/" + sessionId + "/mode", { method: "POST",
            body: JSON.stringify({ mode: next }) });
          mode = next;
          paint();
        } catch (e) { showError(e); }
      });
    }
    let log;
    // While a turn runs server-side, everything the store holds past its mark
    // belongs to that turn and will come back through the replay: drawing it
    // here too would double the tool chips of an agent turn.
    const stored = session.running
      ? session.messages.slice(0, session.turn_starts_at)
      : session.messages;
    if (session.kind === "agent") {
      log = h("div", { class: "chat-log" });
      for (const turn of groupTurns(stored)) {
        if (turn.lead) log.append(chatBubble("user", turn.lead.content,
          undefined, turn.lead.ts).bubble);
        const recapText = formatRecap(turn);
        // Never folded: replies, approvals and errors. Folding hides
        // chatter, never something that asks the operator for a decision.
        const folded = [], plain = [];
        for (const m of turn.items) {
          if (m.role === "assistant" && !m.content && !m.thinking) continue;
          (m.role === "tool" && !m.error ? folded : plain).push(m);
        }
        if (recapText && folded.length) {
          const det = h("details", { class: "recap" },
            h("summary", {},
              icon("chevron"), h("span", { text: recapText })));
          for (const m of folded) {
            det.append(toolChip(m.tool_name || "tool", null, m.content, m.ts));
          }
          log.append(det);
        }
        for (const m of plain) {
          if (m.role === "tool") {  // a tool that failed: shown, not folded
            log.append(toolChip(m.tool_name || "tool", null, m.content, m.ts));
            continue;
          }
          const b = chatBubble(m.role, m.content, m.thinking, m.ts);
          setChatMeta(b.meta, m.metrics);
          if (m.error) {
            b.meta.textContent = "erreur : " + m.error;
            b.meta.classList.add("chat-error");
          }
          log.append(b.bubble);
        }
      }
      if ((session.pending_calls || []).length) {
        // In auto mode nothing awaits the operator: the queue is a paused
        // turn (iteration cap) that the next message resumes.
        if (session.mode === "approve")
          log.append(approvalPanel(session.pending_calls[0], log, sessionId));
        else {
          const notice = h("div", { class: "chat-notice" });
          const badge = h("span", { class: "wait-badge-pulse",
            text: "⏸ " + session.pending_calls.length + " en attente" });
          notice.append(badge);
          notice.appendChild(document.createTextNode(" — envoie « continue » pour reprendre."));
          log.append(notice);
        }
      }
    } else {
      log = h("div", { class: "chat-log" }, ...stored.map((m) => {
        const b = chatBubble(m.role, m.content, m.thinking, m.ts);
        setChatMeta(b.meta, m.metrics);
        if (m.error) {
          b.meta.textContent = "erreur : " + m.error;
          b.meta.classList.add("chat-error");
        }
        return b.bubble;
      }));
    }
    STATUS = agentStatus();
    // Live context estimate: what the window already holds (last turn's real
    // token counts) plus the streamed chars of the current turn.
    const lastMet = [...session.messages].reverse()
      .find((m) => m.metrics && promptTokens(m.metrics) > 0);
    STATUS.ctxBase = lastMet
      ? promptTokens(lastMet.metrics) + (lastMet.metrics.eval_count || 0)
      : 0;
    STATUS.ctxMax = session.num_ctx || 65536;
    if (session.calibration && session.calibration.tokens > 0)
      STATUS.charsPerTok = session.calibration.chars / session.calibration.tokens;
    const ctxSel = h("select", { class: "chat-ctx",
      title: "fenêtre de contexte (num_ctx)" },
      ...[8192, 16384, 32768, 65536, 131072].map((n) =>
        h("option", { value: String(n), text: n / 1024 + "k ctx" })));
    ctxSel.value = String(session.num_ctx || 65536);
    ctxSel.addEventListener("change", async () => {
      try {
        await api("/api/chats/" + sessionId + "/num_ctx", { method: "POST",
          body: JSON.stringify({ num_ctx: Number(ctxSel.value) }) });
        STATUS.ctxMax = Number(ctxSel.value);
      } catch (e) { showError(e); }
    });
    const input = h("textarea", { class: "chat-input", rows: 4,
      placeholder: "Ton message… (Entrée = envoyer, Maj+Entrée = retour ligne)" });
    const sendBtn = h("button", { text: "Envoyer" });
    const stopBtn = h("button", { class: "danger", text: "■ Stop",
      title: "Interrompre l'agent (le travail partiel est sauvegardé)",
      // The turn no longer dies with the connection, so aborting the fetch
      // would just blind the viewer while the model kept going: ask the
      // server. The stream then ends on its own with the saved partial.
      onclick: async () => {
        stopBtn.disabled = true;
        try {
          await api("/api/chats/" + sessionId + "/stop",
            { method: "POST", body: "{}" });
        } catch (e) { showError(e); }
      } });
    stopBtn.hidden = true;
    const send = async () => {
      const content = input.value.trim();
      if (!content || sendBtn.disabled) return;
      input.value = "";
      sendBtn.disabled = true;
      stopBtn.hidden = false;
      log.append(chatBubble("user", content, undefined, Date.now() / 1000).bubble);
      // Scroll sticky : ne scroll vers le bas que si l'utilisateur était déjà en bas
      const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 50;
      if (atBottom) log.scrollTop = log.scrollHeight;
      try {
        await streamChat("/api/chats/" + sessionId + "/messages", { content }, log);
      } catch (e) { showError(e); }
      sendBtn.disabled = false;
      stopBtn.hidden = true;
      stopBtn.disabled = false;
      input.focus();
    };
    sendBtn.addEventListener("click", send);
    // A turn started before this page load -- a refresh, a navigation, another
    // tab -- is still running server-side. Attach to it as a viewer instead of
    // rendering a frozen transcript.
    if (session.running) {
      sendBtn.disabled = true;
      stopBtn.hidden = false;
      streamChat("/api/chats/" + sessionId + "/stream", null, log)
        .catch(showError)
        .then(() => {
          sendBtn.disabled = false;
          stopBtn.hidden = true;
          stopBtn.disabled = false;
        });
    }
    input.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" && !ev.shiftKey) { ev.preventDefault(); send(); }
    });
    const isLoaded = runtime.loaded === session.model;
    // « non chargé » s'affichait aussi sur un modèle cloud, qui n'a pas de VRAM
    // à occuper : la pastille annonçait un coût de chargement qui n'existe pas.
    const isCloudModel = !!(models.find((m) => m.name === session.model) || {}).cloud;
    const loadedDot = isCloudModel ? null : h("span", {
      class: "chat-loaded " + (isLoaded ? "on" : "off"),
      title: isLoaded ? "modèle en VRAM — réponse immédiate"
        : "modèle non chargé — le premier message paiera le chargement",
      text: isLoaded ? "● chargé" : "○ non chargé" });
    const card = h("div", { class: "model-card-slot", hidden: "" });
    const cardBtn = iconBtn("gear", "Fiche technique du modèle", () => {
      card.hidden = !card.hidden;
      cardBtn.classList.toggle("on", !card.hidden);
      if (!card.hidden) {
        card.replaceChildren(modelCard(
          models.find((m) => m.name === pick.value),
          runtimeCat.ctx_local, session.num_ctx));
      }
    });
    // Un modèle local absent de la VRAM se chargeait au premier message, sans
    // que rien ne le propose : l'opérateur payait le chargement en croyant que
    // la session ramait. Le bouton n'existe que là où il veut dire quelque
    // chose -- un modèle cloud n'a rien à charger.
    const loadBtn = (!isCloudModel && !isLoaded)
      ? iconBtn("download", "Charger " + session.model + " en VRAM maintenant",
          async (ev) => {
            const btn = ev.currentTarget;
            btn.disabled = true;
            try {
              await api("/api/runtime/load", { method: "POST",
                body: JSON.stringify({ model: session.model }) });
              tick();
            } catch (e) { showError(e); btn.disabled = false; }
          })
      : null;
    // Un chemin absolu est illisible et change de machine ; le nom du projet
    // est ce que l'opérateur a écrit dans factory.toml. Le chemin reste, dans
    // l'infobulle, pour qui a besoin de vérifier où ça écrit vraiment.
    const meta = sessions.find((s) => s.session_id === sessionId) || {};
    const wsTag = session.workspace
      ? h("span", { class: "chat-ws", title: session.workspace },
          icon(meta.project ? "projects" : "file"),
          h("span", { text: meta.project || "hors projet" }))
      : null;
    const banner = h("div", { class: "banner" }, h("label", { text: "Modèle" }),
        pick.routeSel, model, cardBtn,
        loadedDot, loadBtn, ctxSel,
        modeCtl, wsTag);
    right = h("div", { class: "chat-main" },
      banner,
      card,
      log,
      STATUS.el,
      h("div", { class: "chat-compose" }, input, stopBtn, sendBtn));
    // Drag-n-drop: on an agent session the file lands in
    // <workspace>/uploads/ and the composer references it; a plain chat has
    // no workspace, so the file's text is inlined into the composer instead.
    const dropFile = (file) => new Promise((resolve, reject) => {
      const rd = new FileReader();
      const mention = (text) => {
        input.value += (input.value && !input.value.endsWith("\n") ? "\n" : "")
          + text;
      };
      rd.onerror = () => reject(new Error("lecture impossible : " + file.name));
      if (session.kind === "agent") {
        rd.onload = async () => {
          try {
            const res = await api("/api/chats/" + sessionId + "/upload", {
              method: "POST",
              body: JSON.stringify({ name: file.name,
                content_b64: String(rd.result).split(",")[1] || "" }) });
            mention("Fichier déposé : " + res.path + " (" + res.bytes
              + " octets)\n");
            resolve();
          } catch (e) { reject(e); }
        };
        rd.readAsDataURL(file);
      } else {
        rd.onload = () => {
          mention("Contenu de " + file.name + " :\n```\n" + rd.result
            + "\n```\n");
          resolve();
        };
        rd.readAsText(file);
      }
    });
    right.addEventListener("dragover", (ev) => {
      ev.preventDefault();
      right.classList.add("dropping");
    });
    right.addEventListener("dragleave", () => right.classList.remove("dropping"));
    right.addEventListener("drop", async (ev) => {
      ev.preventDefault();
      right.classList.remove("dropping");
      for (const f of ev.dataTransfer.files) {
        try { await dropFile(f); } catch (e) { showError(e); }
      }
      input.focus();
    });
    // Le log n'est pas encore dans le document ici : hors du DOM, scrollHeight
    // vaut 0 et le « scroll en bas » initial ne partait jamais. On le garde
    // sous la main et on le pose après l'insertion (voir plus bas).
    logEl = log;

    // Bouton flottant pour revenir en bas du chat. La flèche est un vrai
    // pictogramme : sans enfant, le bouton n'était qu'un rond jaune vide.
    scrollBtn = h("button", { class: "scroll-btn", title: "Retour en bas",
      "aria-label": "Retour en bas", hidden: "",
      onclick: () => { log.scrollTop = log.scrollHeight; } });
    scrollBtn.append(icon("arrow-down"));
    log.addEventListener("scroll", () => {
      const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 50;
      if (!atBottom && scrollBtn.hidden) {
        scrollBtn.hidden = false;
        scrollBtn.classList.add("visible");
      } else if (atBottom && !scrollBtn.hidden) {
        scrollBtn.classList.remove("visible");
        setTimeout(() => { scrollBtn.hidden = true; }, 200);
      }
    });
  }

  // planSlot lives in #plan-slot-container (outside #main)
  const chatContent = h("div", { class: "chat-content", "data-session": sessionId || "",
    "data-kind": (session && session.kind) || "chat" },
    right,
    scrollBtn);
  // .banner-main is a sibling of .content-area, never inside #main: just
  // replace the content area without touching the banner.
  MAIN.replaceChildren(chatContent);

  // Ouvrir une session, c'est arriver sur le dernier message : maintenant que
  // le log est dans le document, ses dimensions existent et le saut porte.
  // Deux fois : au premier passage la mise en page n'est pas finie (polices,
  // blocs markdown), et le saut tombait 90 px trop haut -- assez pour que le
  // bouton « retour en bas » s'affiche sur une session qu'on vient d'ouvrir.
  if (logEl) {
    const toBottom = () => { logEl.scrollTop = logEl.scrollHeight; };
    toBottom();
    requestAnimationFrame(toBottom);
  }

  // Create and append plan-slot into its dedicated container. A chat session
  // has no plan: the slot would only be refreshed by /trace polls that say
  // "Aucun plan posé" forever (audit 2026-08-19, défaut 3).
  const planContainer = document.getElementById("plan-slot-container");
  if (planContainer) {
    if (sessionId && session && session.kind === "agent") {
      let planSlot = planContainer.querySelector(".plan-slot");
      if (!planSlot) {
        planSlot = h("div", { class: "plan-slot" });
        planContainer.appendChild(planSlot);
      }
      refreshPlanPanel(sessionId, planSlot);
    } else {
      // Revenu à la liste ou session chat : le plan de la session précédente
      // n'a plus d'objet.
      planContainer.replaceChildren();
    }
  }
  return false;
}

// Le panneau se rafraichit seul: render() court-circuite la vue chat pour ne
// pas rejouer le transcript a chaque tick, donc le plan et le contexte seraient
// figes si on attendait un re-rendu complet.
async function refreshPlanPanel(sessionId, slot) {
  if (!slot || !sessionId) return;
  const trace = await fetchTrace(sessionId);
  if (!trace) return;
  panel = planPanel(trace);
  const placeholder = h("div", { class: "empty" }, "En attente de phases…");
  slot.replaceChildren(panel || placeholder);
}

// ---- Router + polling ----

let pollTimer = null;

async function render() {
  const cur = location.hash.replace(/^#/, "") || "jobs";
  if (cur === "delegate" && MAIN.querySelector(".test-src")) {
    pollTimer = setTimeout(render, 10000);
    return;
  }
  // Ne restaurer le <nav> que quand on quitte réellement la vue chat.
  const wasChat = !!MAIN.querySelector(".chat-content");
  const isChat = cur.split("/")[0] === "chat";
  // Un stream mute le chat en direct : tout re-render reconstruirait le DOM en
  // plein milieu et effacerait le composeur ET la bulle en cours de rédaction
  // (plaintes 2026-08-19). Pendant un stream, render ne fait rien du tout.
  if (STREAM_ABORT && isChat && wasChat) {
    pollTimer = setTimeout(render, 3000);
    return;
  }
  if (wasChat && !isChat) {
    const navEl = document.querySelector("nav");
    if (navEl) navEl.hidden = false;
    // Les deux colonnes du chat vivent hors de #main : personne ne les
    // effaçait en changeant d'onglet, et la liste des sessions restait
    // plantée à côté des jobs. Vidées, les colonnes se referment (`:empty`).
    for (const id of ["chat-sessions-list", "plan-slot-container"]) {
      const el = document.getElementById(id);
      if (el) el.replaceChildren();
    }
  }
  if (isChat && wasChat) {
    const chatContent = MAIN.querySelector(".chat-content");
    if (chatContent && chatContent.dataset.session === (cur.split("/")[1] || "")) {
      // A chat session has no plan: keep the 3 s timer (it also keeps the
      // re-render cadence) but skip the /trace poll (audit 2026-08-19, défaut 3).
      if (chatContent.dataset.kind === "agent") {
        const planSlot = document.querySelector(".plan-slot");
        if (planSlot) refreshPlanPanel(chatContent.dataset.session, planSlot);
      }
      pollTimer = setTimeout(render, 3000);
      return;
    }
  }

  const hash = location.hash.replace(/^#/, "") || "jobs";
  const [view, jobId] = hash.split("/");
  document.querySelectorAll("nav a").forEach((a) =>
    a.classList.toggle("current", a.dataset.view === view));
  // Notifications de fin de job depuis n'importe quelle page.
  // Le try/catch est structurel : sans lui, un /api/jobs en erreur faisait
  // remonter une exception hors de tout garde-fou, render() s'arrêtait avant
  // d'afficher quoi que ce soit et le polling ne se replanifiait jamais.
  try { trackJobStatuses((await api("/api/jobs")).jobs); }
  catch (e) { /* le suivi des notifications ne doit rien casser */ }
  let keepPolling = false;
  try {
    if (view === "jobs" && jobId) keepPolling = await renderJob(jobId);
    else if (view === "delegate") keepPolling = await renderDelegate();
    else if (view === "dashboard") keepPolling = await renderDashboard();
    else if (view === "runtime") keepPolling = await renderRuntime();
    else if (view === "chat") keepPolling = await renderChat(jobId);
    else if (view === "projects") keepPolling = await renderProjects();
    else if (view === "benchmarks") keepPolling = await renderBenchmarks();
    else keepPolling = await renderJobs();
  } catch (e) { showError(e); }
  clearTimeout(pollTimer);
  pollTimer = setTimeout(render, keepPolling ? 2000 : 10000);
}

function tick() { clearTimeout(pollTimer); render(); }

window.addEventListener("hashchange", tick);
tick();
