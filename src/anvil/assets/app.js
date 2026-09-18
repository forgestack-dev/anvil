// Read-only view of the local run server. Ledger text is untrusted operator and
// agent content, so every value reaches the page through textContent; nothing
// here builds markup from a string.
"use strict";

const runsList = document.getElementById("runs");
const moreButton = document.getElementById("more");
const heading = document.getElementById("detail-heading");
const facts = document.getElementById("facts");
const runError = document.getElementById("run-error");
const live = document.getElementById("live");
const panels = {
  tasks: document.getElementById("panel-tasks"),
  spend: document.getElementById("panel-spend"),
  events: document.getElementById("panel-events"),
};
const tabs = [...document.querySelectorAll('[role="tab"]')];

let selected = null;
let stream = null;
let refreshTimer = null;
let nextRunCursor = null;
let apiVersion = null;

function element(tag, text, attributes) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = String(text);
  for (const [name, value] of Object.entries(attributes || {})) {
    if (value !== null && value !== undefined) node.setAttribute(name, String(value));
  }
  return node;
}

function replace(parent, children) {
  parent.replaceChildren(...children);
}

async function api(path) {
  const response = await fetch(path, { headers: { Accept: "application/json" } });
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || response.statusText);
  if (body.api_version !== undefined) {
    if (apiVersion !== null && body.api_version !== apiVersion) {
      // The contract changed under us; stop rather than reinterpret old rows.
      setLive("error", "server contract changed — reload");
      throw new Error("api_version changed");
    }
    apiVersion = body.api_version;
  }
  return body;
}

function setLive(state, label) {
  live.dataset.live = state;
  live.textContent = label;
}

// -- money and counts -------------------------------------------------------
// An estimate is never rendered as a plain number, and absent accounting is
// never rendered as zero. Both would read as certainty the ledger does not have.

const KINDS = {
  provider_reported_estimate: "provider estimate",
  configured_price_estimate: "priced from config",
  not_started: "not started",
  unknown: "unknown",
};

function money(value, kind) {
  if (value === null || value === undefined) {
    return element("span", kind === "not_started" ? "—" : "unknown", { class: "unknown" });
  }
  const node = element("span", "$" + Number(value).toFixed(4));
  if (kind) node.title = KINDS[kind] || kind;
  return node;
}

function tokens(value) {
  if (value === null || value === undefined) return element("span", "unknown", { class: "unknown" });
  return element("span", Number(value).toLocaleString());
}

function stateCell(value) {
  return element("td", value, { class: "state", "data-state": value });
}

function elapsed(from, to) {
  const start = Date.parse(from), end = to ? Date.parse(to) : Date.now();
  if (Number.isNaN(start) || Number.isNaN(end)) return "unknown";
  const seconds = Math.max(0, Math.round((end - start) / 1000));
  if (seconds < 60) return seconds + "s";
  if (seconds < 3600) return Math.floor(seconds / 60) + "m " + (seconds % 60) + "s";
  return Math.floor(seconds / 3600) + "h " + Math.floor((seconds % 3600) / 60) + "m";
}

// -- run list ---------------------------------------------------------------

function started(iso) {
  const at = Date.parse(iso);
  if (Number.isNaN(at)) return "unknown";
  const d = new Date(at), pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ` +
         `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

// The cursor's ordering is frozen by the read contract, so the newest-first
// ordering a reader wants is applied here over the rows already loaded. A run
// whose ledger could not be read carries no start time and sorts last.
function byNewest(a, b) {
  const at = Date.parse(a.created_at || ""), bt = Date.parse(b.created_at || "");
  if (Number.isNaN(at) && Number.isNaN(bt)) return a.run_id < b.run_id ? -1 : 1;
  if (Number.isNaN(at)) return 1;
  if (Number.isNaN(bt)) return -1;
  return bt - at;
}

function runItem(run) {
  const item = element("li", null, { role: "option", "aria-selected": String(run.run_id === selected) });
  item.dataset.runId = run.run_id;
  item.append(element("div", run.run_id, { class: "id" }));
  if (run.unreadable) {
    item.append(element("div", "unreadable ledger", { class: "meta unknown" }));
    return item;
  }
  const done = (run.task_counts && run.task_counts.done) || 0;
  const status = element("span", run.status, { class: "state", "data-state": run.status });
  const meta = element("div", null, { class: "meta" });
  meta.append(status, document.createTextNode(` · ${done}/${run.task_total} accepted · `),
              element("span", started(run.created_at), { class: "when" }));
  item.append(meta);
  return item;
}

let loadedRuns = [];

async function loadRuns(append) {
  const query = append && nextRunCursor ? `?after=${encodeURIComponent(nextRunCursor)}` : "";
  const page = await api("/api/runs" + query);
  loadedRuns = append ? loadedRuns.concat(page.items) : page.items.slice();
  loadedRuns.sort(byNewest);
  replace(runsList, loadedRuns.map(runItem));
  nextRunCursor = page.next_after;
  moreButton.hidden = page.next_after === null;
  if (!append && !selected && loadedRuns.length) select(loadedRuns[0].run_id);
}

// -- detail -----------------------------------------------------------------

function fact(label, value) {
  const group = document.createElement("div");
  group.append(element("dt", label), element("dd", value));
  return group;
}

async function loadSummary(runId) {
  const summary = await api(`/api/runs/${encodeURIComponent(runId)}`);
  heading.textContent = summary.run_id;
  const counts = summary.task_counts || {};
  const parts = Object.keys(counts).sort().map((k) => `${counts[k]} ${k}`).join(", ");
  replace(facts, [
    fact("status", summary.status),
    fact("branch", summary.branch),
    fact("repo", summary.repo),
    fact("saved state", summary.run_dir || "unknown"),
    fact("started", started(summary.created_at)),
    fact("base", summary.base_sha.slice(0, 12)),
    fact("tasks", `${counts.done || 0}/${summary.task_total} accepted` + (parts ? ` (${parts})` : "")),
    fact("elapsed", elapsed(summary.created_at, summary.updated_at)),
  ]);
  runError.textContent = summary.error || "";
  runError.hidden = !summary.error;
  return summary;
}

// Waves are the dependency structure rendered as tables rather than a graph:
// section 11 of the dashboard plan requires a table alternative, and with no
// graph there is nothing for the table to be an alternative to.
function waves(tasks) {
  const byId = new Map(tasks.map((task) => [task.id, task]));
  const depth = new Map();
  const of = (task, seen) => {
    if (depth.has(task.id)) return depth.get(task.id);
    if (seen.has(task.id)) return 0;
    seen.add(task.id);
    const parents = (task.depends_on || []).map((id) => byId.get(id)).filter(Boolean);
    const value = parents.length ? 1 + Math.max(...parents.map((p) => of(p, seen))) : 0;
    depth.set(task.id, value);
    return value;
  };
  const grouped = new Map();
  for (const task of tasks) {
    const key = of(task, new Set());
    if (!grouped.has(key)) grouped.set(key, []);
    grouped.get(key).push(task);
  }
  return [...grouped.entries()].sort((a, b) => a[0] - b[0]);
}

function taskTable(label, tasks) {
  const table = element("table");
  table.append(element("caption", label));
  const head = document.createElement("tr");
  for (const name of ["Ticket", "State", "Depends on", "Attempt"]) head.append(element("th", name));
  table.append(element("thead").appendChild(head).parentNode);
  const body = element("tbody");
  for (const task of tasks) {
    const row = document.createElement("tr");
    row.append(element("td", task.id, { class: "mono" }));
    row.append(stateCell(task.status));
    row.append(element("td", (task.depends_on || []).join(", ") || "—", { class: "mono" }));
    row.append(element("td", task.attempt_id ? task.attempt_id.slice(0, 8) : "—", { class: "mono" }));
    body.append(row);
  }
  table.append(body);
  return table;
}

async function loadTasks(runId) {
  const page = await api(`/api/runs/${encodeURIComponent(runId)}/tasks?limit=500`);
  if (!page.items.length) return replace(panels.tasks, [element("p", "No tickets.", { class: "empty" })]);
  const nodes = waves(page.items).map(([index, tasks]) => {
    const wrap = element("div", null, { class: "wave" });
    wrap.append(taskTable(`Wave ${index + 1} · ${tasks.length} ticket(s)`, tasks));
    return wrap;
  });
  if (page.next_after !== null) nodes.push(element("p", "More tickets not shown.", { class: "note" }));
  replace(panels.tasks, nodes);
}

async function loadSpend(runId) {
  const page = await api(`/api/runs/${encodeURIComponent(runId)}/telemetry?limit=500`);
  if (!page.items.length) return replace(panels.spend, [element("p", "No attempts yet.", { class: "empty" })]);
  const table = element("table");
  table.append(element("caption", "Per attempt, worker and review combined"));
  const head = document.createElement("tr");
  for (const name of ["Ticket", "State", "Model", "In", "Out", "Cost", "Duration", "Outcome"]) {
    head.append(element("th", name, name === "Ticket" || name === "State" ? null : { class: "num" }));
  }
  table.append(element("thead").appendChild(head).parentNode);
  const body = element("tbody");
  for (const record of page.items) {
    const row = document.createElement("tr");
    const [input, output] = ["input_tokens", "output_tokens"].map((field) =>
      record.invocations.reduce((sum, i) => {
        if (sum === null) return null;
        if (i.cost_kind === "not_started") return sum;
        return i[field] == null ? null : sum + i[field];
      }, 0));
    const model = record.invocations.map((i) => i.reported_model).find(Boolean)
      || (record.decision && record.decision.model) || null;
    row.append(element("td", record.task_id, { class: "mono" }));
    row.append(stateCell(record.status));
    row.append(element("td", model || "unknown", model ? { class: "mono" } : { class: "unknown" }));
    row.append(element("td").appendChild(tokens(input)).parentNode);
    row.append(element("td").appendChild(tokens(output)).parentNode);
    const kinds = record.invocations.map((i) => i.cost_kind);
    row.append(element("td").appendChild(money(record.cost_usd, kinds.find((k) => k !== "not_started")))
      .parentNode);
    row.append(element("td", record.duration_seconds ? record.duration_seconds.toFixed(1) + "s" : "—"));
    row.append(element("td", record.failure_category === "none" ? record.evaluation
      : `${record.evaluation} · ${record.failure_category}`));
    for (const cell of row.querySelectorAll("td")) {
      if (["3", "4", "5", "6"].includes(String([...row.children].indexOf(cell)))) cell.classList.add("num");
    }
    body.append(row);
  }
  table.append(body);
  const total = element("p", null, { class: "note" });
  total.append(document.createTextNode("Known cost "));
  total.append(money(page.known_cost_usd, null));
  total.append(document.createTextNode(
    `. ${page.cost_complete ? "Accounting complete for every attempt" :
      "Accounting incomplete: some attempts report unknown cost, which is excluded above"}. ` +
    `Figures are ${page.cost_kind}. ${page.evaluation_note}`));
  replace(panels.spend, [table, total]);
}

async function loadEvents(runId) {
  const page = await api(`/api/runs/${encodeURIComponent(runId)}/events?limit=200`);
  const table = element("table");
  table.append(element("caption", "Most recent ledger events"));
  const head = document.createElement("tr");
  for (const name of ["#", "Time", "Kind", "Ticket", "From", "To"]) head.append(element("th", name));
  table.append(element("thead").appendChild(head).parentNode);
  const body = element("tbody");
  for (const event of page.items.slice().reverse()) {
    const row = document.createElement("tr");
    row.append(element("td", event.id, { class: "num" }));
    row.append(element("td", (event.created_at || "").slice(11, 19), { class: "mono" }));
    const kind = event.details && event.details.message_kind
      ? `${event.kind}: ${event.details.message_kind}` : event.kind;
    row.append(element("td", kind));
    row.append(element("td", event.task_id || "—", { class: "mono" }));
    row.append(element("td", event.from_status || "—"));
    row.append(stateCell(event.to_status));
    body.append(row);
  }
  table.append(body);
  replace(panels.events, [element("div", null, { class: "events" })]);
  panels.events.firstChild.append(table);
}

// -- selection, tabs, live updates ------------------------------------------

function refresh(runId) {
  return Promise.all([loadSummary(runId), loadTasks(runId), loadSpend(runId), loadEvents(runId)])
    .catch((error) => setLive("error", error.message));
}

function scheduleRefresh(runId) {
  // The stream says something changed; the pages say what it now is. Coalesce,
  // so a burst of events costs one round of reads rather than one each.
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(() => { refresh(runId); loadRuns(false); }, 400);
}

function listen(runId) {
  if (stream) stream.close();
  stream = new EventSource(`/api/runs/${encodeURIComponent(runId)}/stream`);
  stream.addEventListener("meta", (event) => { apiVersion = JSON.parse(event.data).api_version; });
  stream.addEventListener("end", () => {
    stream.close();
    setLive("idle", "run complete");
    scheduleRefresh(runId);
  });
  stream.onopen = () => setLive("live", "live");
  stream.onmessage = () => scheduleRefresh(runId);
  stream.onerror = () => {
    if (stream.readyState !== EventSource.CLOSED) setLive("idle", "reconnecting");
  };
}

async function select(runId) {
  selected = runId;
  for (const item of runsList.children) {
    item.setAttribute("aria-selected", String(item.dataset.runId === runId));
  }
  await refresh(runId);
  listen(runId);
}

function showTab(id) {
  for (const tab of tabs) {
    const active = tab.id === id;
    tab.setAttribute("aria-selected", String(active));
    tab.tabIndex = active ? 0 : -1;
    document.getElementById(tab.getAttribute("aria-controls")).hidden = !active;
  }
}

runsList.addEventListener("click", (event) => {
  const item = event.target.closest("li");
  if (item) select(item.dataset.runId);
});

runsList.addEventListener("keydown", (event) => {
  const items = [...runsList.children];
  const index = items.findIndex((item) => item.dataset.runId === selected);
  if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    event.preventDefault();
    const next = items[Math.min(items.length - 1, Math.max(0, index + (event.key === "ArrowDown" ? 1 : -1)))];
    if (next) select(next.dataset.runId);
  }
});

for (const tab of tabs) {
  tab.addEventListener("click", () => showTab(tab.id));
  tab.addEventListener("keydown", (event) => {
    const step = event.key === "ArrowRight" ? 1 : event.key === "ArrowLeft" ? -1 : 0;
    if (!step) return;
    event.preventDefault();
    const next = tabs[(tabs.indexOf(tab) + step + tabs.length) % tabs.length];
    next.focus();
    showTab(next.id);
  });
}

moreButton.addEventListener("click", () => loadRuns(true));

api("/health")
  .then((health) => {
    document.getElementById("origin").textContent =
      `${health.state_dir} · read-only · api v${health.api_version}`;
    return loadRuns(false);
  })
  .catch((error) => setLive("error", error.message));
