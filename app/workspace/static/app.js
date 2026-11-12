/* Ordixa local workspace.
   Plain JavaScript with no build step and no third-party code. Every value
   from a file is put on the page as text (textContent), never as HTML. */
"use strict";

const TOKEN = document.querySelector('meta[name="ordixa-token"]').content;
const main = document.getElementById("main");

const TASKS = {
  clean: {
    name: "Tidy up a spreadsheet",
    kind: "Tidy up a spreadsheet",
    verb: "Tidying up your file",
    desc: "Fix stray spaces, placeholder text, and mixed formats in one export, and set aside the rows that need a person to look at them.",
    gets: "You get a clean CSV and a list of every row held back, with the reason.",
  },
  compare: {
    name: "Find what changed",
    kind: "Find what changed",
    verb: "Comparing the two versions",
    desc: "Compare two versions of the same list, such as last week's and this week's inventory, matched by a code like the SKU.",
    gets: "You get added, removed, and changed records, and a change report workbook.",
  },
  reconcile: {
    name: "Match payments to orders",
    kind: "Match payments to orders",
    verb: "Matching payments to orders",
    desc: "Check every order against the payments and refunds you received, to the cent.",
    gets: "You get each order marked paid, part-paid, unpaid, or overpaid, and an exceptions workbook.",
  },
};
const ROLES = {
  clean: ["input"],
  compare: ["before", "after"],
  reconcile: ["orders", "payments", "customers"],
};
const ROLE_TEXT = {
  input: { label: "Your spreadsheet", hint: "The CSV or Excel export you want to tidy up." },
  before: { label: "Earlier version", hint: "For example, last week's export." },
  after: { label: "Later version", hint: "The newer export of the same list." },
  orders: { label: "Orders", hint: "Your store or invoicing export. If you have one file per month, add them all." },
  payments: { label: "Payments", hint: "Your card processor or bank export, including refunds." },
  customers: { label: "Customers", hint: "Adds customer names to the results.", optional: true },
};
// reconciliation fields in the order a person reads an export
const FIELD_ORDER = {
  orders: ["order_id", "date", "total", "customer_id", "currency"],
  payments: ["payment_id", "order_id", "amount", "transaction_type", "status", "payment_date", "currency"],
  customers: ["customer_id", "name", "email", "phone"],
};
const STEPS = [
  ["files", "Add files"],
  ["columns", "Choose columns"],
  ["check", "Check and run"],
  ["results", "Results"],
];
const TYPE_HELP = {
  text: "Kept as written, with spaces at the ends removed.",
  identifier: "Kept exactly, including leading zeros, like SKU 000130.",
  decimal: "Read exactly to the cent; never rounded.",
  integer: "Digits only, such as a quantity.",
  date: "Written as year-month-day in the results.",
};
const TRANSACTION_TARGETS = [
  ["", "Not set (rows held back)"],
  ["payment", "Payment"],
  ["refund", "Refund"],
  ["adjustment", "Adjustment (signed)"],
];
const STATUS_TARGETS = [
  ["", "Not set (rows held back)"],
  ["paid", "Paid"],
  ["partial", "Partly paid"],
  ["pending", "Pending"],
  ["refunded", "Refunded"],
  ["exclude", "Leave out (no money moved)"],
];

const state = {
  status: null,
  view: null,
  descriptions: {},
  values: {},
  rows: null,
  check: null,
  checking: false,
  job: null,
  run: null,
  verify: null,
  tab: null,
  filter: null,
  page: 0,
  uploads: {},
  previews: {},
};

/* small helpers */

function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") el.className = value;
    else if (key === "style") el.style.cssText = value;
    else if (key === "value") el.value = value;
    else if (key === "checked") el.checked = Boolean(value);
    else if (key === "selected") el.selected = Boolean(value);
    else if (key.startsWith("on") && typeof value === "function") el.addEventListener(key.slice(2), value);
    else el.setAttribute(key, value === true ? "" : String(value));
  }
  put(el, children);
  return el;
}

function put(el, ...children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

function clear(el) {
  while (el.firstChild) el.firstChild.remove();
  return el;
}

function plural(count, one, many) {
  return `${Number(count).toLocaleString()} ${count === 1 ? one : many || one + "s"}`;
}

function bytes(size) {
  if (size === null || size === undefined) return "";
  if (size < 1024) return `${size} bytes`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / 1024 / 1024).toFixed(1)} MB`;
}

function when(text) {
  if (!text) return "";
  const date = new Date(text);
  return Number.isNaN(date.getTime()) ? text : date.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

function announce(text) {
  const live = document.getElementById("live");
  live.textContent = "";
  setTimeout(() => { live.textContent = text; }, 30);
}

function toast(text, bad) {
  document.querySelectorAll(".toast").forEach((el) => el.remove());
  const el = h("div", { class: bad ? "toast bad" : "toast", role: bad ? "alert" : "status" }, text);
  document.body.append(el);
  setTimeout(() => el.remove(), bad ? 7000 : 3500);
}

function note(kind, ...content) {
  return h("div", { class: `note ${kind}` }, h("div", null, ...content));
}

async function api(method, path, body) {
  const init = { method, headers: { "X-Ordixa-Token": TOKEN }, credentials: "same-origin" };
  if (body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  let response;
  try {
    response = await fetch(`/api${path}`, init);
  } catch {
    throw Object.assign(new Error("The workspace is not responding. Check that Ordixa is still running in its terminal window."), { status: 0 });
  }
  let data = null;
  try { data = await response.json(); } catch { data = null; }
  if (!response.ok) {
    const error = new Error((data && data.error) || `The request failed (${response.status}).`);
    error.status = response.status;
    error.data = data;
    throw error;
  }
  return data;
}

function go(path) {
  if (location.hash === `#${path}`) route();
  else location.hash = path;
}

function sessionPath(step, extra) {
  return `/s/${state.view.session.id}/${step}${extra ? "/" + extra : ""}`;
}

function sessionTitle(session) {
  return session.title || TASKS[session.workflow].name;
}

function filesReady(view) {
  const required = view.session.workflow === "reconcile" ? ["orders", "payments"] : ROLES[view.session.workflow];
  return required.every((role) => view.files[role].length && view.files[role].every((f) => !f.errors.length));
}

/* routing */

window.addEventListener("hashchange", route);
window.addEventListener("DOMContentLoaded", async () => {
  try {
    state.status = await api("GET", "/status");
  } catch (error) {
    put(clear(main), note("bad", error.message));
    return;
  }
  route();
});

async function route() {
  const parts = location.hash.replace(/^#\/?/, "").split("/").filter(Boolean);
  try {
    if (parts[0] === "s" && parts[1]) await showSession(parts[1], parts[2] || "files", parts[3]);
    else await showHome();
  } catch (error) {
    if (error.status === 404) {
      toast("That piece of work was not found.", true);
      go("/");
      return;
    }
    put(clear(main), note("bad", error.message), h("p", { class: "actions" }, h("a", { class: "button", href: "#/" }, "Back to start")));
  }
  main.focus({ preventScroll: true });
}

/* home */

async function showHome() {
  document.title = "Ordixa Workspace";
  state.view = null;
  const [sessions, samples] = await Promise.all([api("GET", "/sessions"), api("GET", "/samples")]);
  const tasks = Object.entries(TASKS).map(([workflow, task]) =>
    h("button", { class: "task", type: "button", onclick: () => startTask(workflow) },
      h("span", { class: "task-name" }, task.name),
      h("span", null, task.desc),
      h("span", { class: "task-gets" }, task.gets)));

  const recent = sessions.length
    ? h("table", { class: "recent" },
        h("thead", null, h("tr", null, h("th", null, "Work"), h("th", null, "Files"), h("th", null, "Results"), h("th", null, "Last changed"), h("th", null, h("span", { class: "live" }, "Actions")))),
        h("tbody", null, sessions.map((item) =>
          h("tr", null,
            h("td", null, h("a", { href: `#/s/${item.id}/${item.runs ? "results" : "files"}` }, item.title || TASKS[item.workflow].name),
              h("div", { class: "small muted" }, TASKS[item.workflow].kind)),
            h("td", null, plural(item.files, "file")),
            h("td", null, item.runs ? plural(item.runs, "result") : h("span", { class: "muted" }, "None yet"),
              item.incomplete ? h("div", null, h("span", { class: "chip warn" }, "Interrupted run")) : null),
            h("td", null, when(item.updated_at)),
            h("td", { class: "end" },
              h("a", { class: "button small", href: `#/s/${item.id}/${item.runs ? "results" : "files"}` }, "Open"), " ",
              h("button", { class: "button small danger", type: "button", onclick: () => removeSession(item) }, "Remove"))))))
    : h("p", { class: "empty" }, "Nothing here yet. Choose a task above, or try one with sample data.");

  put(clear(main),
    h("h1", { class: "title" }, "What would you like to do?"),
    h("p", { class: "lede" }, "Choose a task, add your exports, and Ordixa shows you what it will do before it changes anything. Your original files are never modified."),
    h("div", { class: "tasks" }, tasks),
    h("section", { class: "section" },
      h("div", { class: "section-head" }, h("h2", null, "Try it with sample data first"),
        h("p", { class: "small muted" }, "These are fictional businesses. No real customer data is included.")),
      h("div", { class: "panel samples" }, samples.map((sample) =>
        h("div", { class: "sample" },
          h("div", null, h("h3", null, sample.title), h("p", { class: "muted small" }, sample.description)),
          h("button", { class: "button", type: "button", onclick: () => openSample(sample) }, "Open sample"))))),
    h("section", { class: "section" },
      h("div", { class: "section-head" }, h("h2", null, "Your work")),
      recent),
    h("p", { class: "section small muted" }, "Files you add and every result are kept in ", h("span", { class: "mono" }, state.status.data_dir), " on this computer. Remove a piece of work to delete its files and results."));
}

async function startTask(workflow) {
  try {
    const view = await api("POST", "/sessions", { workflow });
    state.view = view;
    go(`/s/${view.session.id}/files`);
  } catch (error) { toast(error.message, true); }
}

async function openSample(sample) {
  try {
    const view = await api("POST", `/samples/${sample.id}`);
    state.view = view;
    toast(`Opened ${sample.title.replace(/^Sample: /, "the sample: ")}`);
    go(`/s/${view.session.id}/files`);
  } catch (error) { toast(error.message, true); }
}

async function removeSession(item) {
  const name = item.title || TASKS[item.workflow].name;
  if (!window.confirm(`Remove "${name}"?\n\nIts files, settings, and results are deleted from this computer. This cannot be undone.`)) return;
  try {
    await api("DELETE", `/sessions/${item.id}`);
    toast(`Removed "${name}"`);
    showHome();
  } catch (error) { toast(error.message, true); }
}

/* session shell */

async function showSession(id, step, extra) {
  if (!state.view || state.view.session.id !== id) {
    state.descriptions = {};
    state.values = {};
    state.rows = null;
    state.check = null;
    state.run = null;
    state.previews = {};
  }
  state.view = await api("GET", `/sessions/${id}`);
  if (state.view.job) state.job = state.view.job;
  const view = state.view;
  document.title = `${sessionTitle(view.session)} · Ordixa Workspace`;

  const content = h("div", { class: "content" });
  const available = {
    files: true,
    columns: filesReady(view),
    check: filesReady(view) && Boolean(view.session.settings),
    results: view.runs.length > 0,
  };
  if (!available[step]) step = "files";
  const rail = h("nav", { class: "rail", "aria-label": "Steps" },
    h("a", { class: "back", href: "#/" }, "← All work"),
    h("p", { class: "session-name" }, sessionTitle(view.session)),
    h("p", { class: "session-kind" }, TASKS[view.session.workflow].kind),
    h("ol", { class: "steps" }, STEPS.map(([key, label]) => {
      const current = key === step;
      const done = !current && ((key === "files" && available.columns) || (key === "columns" && available.check) || (key === "check" && available.results));
      const cls = [current ? "current" : "", done ? "done" : ""].join(" ").trim();
      return h("li", null, available[key]
        ? h("a", { href: `#${`/s/${id}/${key}`}`, class: cls || null, "aria-current": current ? "step" : null }, label)
        : h("span", { class: "step" }, label));
    })));
  put(clear(main), h("div", { class: "session" }, rail, content));

  if (step === "files") renderFiles(content);
  else if (step === "columns") await renderColumns(content);
  else if (step === "check") await renderCheck(content);
  else await renderResults(content, extra || view.runs[0].run);
}

function head(title, text) {
  return h("div", { class: "content-head" }, h("h1", { class: "title" }, title), text ? h("p", { class: "lede" }, text) : null);
}

/* step 1: files */

function renderFiles(content) {
  const view = state.view;
  const workflow = view.session.workflow;
  const intro = {
    clean: "Add the export you want to tidy up. Ordixa only reads it; the original is never changed.",
    compare: "Add two versions of the same list. Ordixa matches records by a code you choose, never by row position.",
    reconcile: "Add your orders and the payments you received. Customers are optional.",
  }[workflow];
  const slots = ROLES[workflow].map((role) => fileSlot(role));
  const notes = view.notes.map((text) => note("info", text));
  const ready = filesReady(view);
  put(content,
    head("Add your files", intro),
    notes.length ? h("div", { class: "stack", style: "margin-bottom:16px" }, notes) : null,
    h("div", { class: "slots" }, slots),
    h("div", { class: "actions" },
      h("a", { class: "button", href: "#/" }, "Back"),
      h("span", { class: "spacer" }),
      ready ? null : h("span", { class: "small muted" }, "Add the required files to continue."),
      h("button", { class: "button primary", type: "button", disabled: !ready, onclick: toColumns }, "Next: choose columns")));
}

function fileSlot(role) {
  const view = state.view;
  const text = ROLE_TEXT[role];
  const files = view.files[role];
  const multiple = Object.prototype.hasOwnProperty.call(view.limits.files, role);
  const canAdd = multiple ? files.length < view.limits.files[role] : true;
  const slot = h("section", { class: "slot", "aria-labelledby": `slot-${role}` },
    h("div", { class: "slot-head" },
      h("h2", { id: `slot-${role}` }, text.label, " ", text.optional ? h("span", { class: "chip quiet" }, "Optional") : null),
      h("p", { class: "small muted" }, text.hint)));
  const list = h("div", { class: "files" });
  for (const file of files) list.append(fileRow(role, file));
  const pending = state.uploads[role];
  if (pending) list.append(uploadRow(role, pending));
  slot.append(list);
  if (canAdd && !pending) {
    const label = files.length && !multiple ? "Choose a different file" : files.length ? "Add another file" : "Choose a file";
    slot.append(dropZone(role, label, multiple));
  }
  return slot;
}

function files(role) {
  return state.view.files[role].length;
}

function dropZone(role, label, multiple) {
  const input = h("input", { type: "file", accept: ".csv,.xlsx", multiple: multiple || null, "aria-label": `${label}: ${ROLE_TEXT[role].label}` });
  input.addEventListener("change", () => { addFiles(role, [...input.files]); input.value = ""; });
  const zone = h("label", { class: files(role) ? "drop compact" : "drop" },
    input,
    h("strong", null, label),
    h("span", { class: "small muted" }, `CSV or Excel (.xlsx), up to ${bytes(state.view.limits.bytes)}. Or drop it here.`));
  zone.addEventListener("dragover", (event) => { event.preventDefault(); zone.classList.add("over"); });
  zone.addEventListener("dragleave", () => zone.classList.remove("over"));
  zone.addEventListener("drop", (event) => {
    event.preventDefault();
    zone.classList.remove("over");
    const dropped = [...event.dataTransfer.files];
    addFiles(role, multiple ? dropped : dropped.slice(0, 1));
  });
  return zone;
}

function fileRow(role, file) {
  const meta = [];
  if (file.rows !== null) meta.push(plural(file.rows, "row"));
  if (file.columns) meta.push(plural(file.columns, "column"));
  meta.push(bytes(file.bytes));
  const key = `${role}:${file.id}`;
  const row = h("div", { class: "file" },
    h("div", null,
      h("div", { class: "file-name" }, file.original_name),
      h("div", { class: "file-meta" }, meta.join(", "))),
    h("div", { class: "actions", style: "margin:0" },
      file.errors.length ? null : h("button", { class: "button small", type: "button", "aria-expanded": state.previews[key] ? "true" : "false", onclick: () => togglePreview(role, file) }, state.previews[key] ? "Hide preview" : "Preview"),
      h("button", { class: "button small danger", type: "button", onclick: () => removeFile(role, file) }, "Remove")),
    file.errors.length || file.warnings.length
      ? h("div", { class: "messages" },
          file.errors.map((text) => note("bad", h("strong", null, "This file can't be used: "), text, ". ", h("span", { class: "fix" }, "Check it opens in Excel, then export it again as CSV or .xlsx."))),
          file.warnings.map((text) => note("warn", capital(text))))
      : null);
  if (state.previews[key]) row.append(h("div", { class: "messages" }, previewPanel(role, file)));
  return row;
}

function capital(text) {
  return text ? text.charAt(0).toUpperCase() + text.slice(1) : text;
}

function uploadRow(role, pending) {
  return h("div", { class: "file" },
    h("div", null,
      h("div", { class: "file-name" }, pending.name),
      h("div", { class: "file-meta" }, `Adding… ${pending.percent}% of ${bytes(pending.size)}`),
      h("div", { class: "progress", role: "progressbar", "aria-valuemin": 0, "aria-valuemax": 100, "aria-valuenow": pending.percent }, h("span", { style: `width:${pending.percent}%` }))),
    h("button", { class: "button small", type: "button", onclick: () => pending.xhr.abort() }, "Cancel"));
}

async function addFiles(role, files) {
  for (const file of files) {
    const ok = await addFile(role, file);
    if (!ok) break;
  }
}

function addFile(role, file) {
  const view = state.view;
  if (!/\.(csv|xlsx)$/i.test(file.name)) {
    toast(`${file.name} is not a CSV or .xlsx file. Older .xls workbooks can be re-saved as .xlsx from Excel.`, true);
    return Promise.resolve(false);
  }
  if (file.size > view.limits.bytes) {
    toast(`${file.name} is larger than ${bytes(view.limits.bytes)}, the most Ordixa reads at once. Export a shorter date range or split the file.`, true);
    return Promise.resolve(false);
  }
  if (file.size === 0) {
    toast(`${file.name} is empty.`, true);
    return Promise.resolve(false);
  }
  return new Promise((resolve) => {
    const xhr = new XMLHttpRequest();
    const pending = { name: file.name, size: file.size, percent: 0, xhr };
    state.uploads[role] = pending;
    rerender();
    xhr.open("PUT", `/api/sessions/${view.session.id}/files/${role}?name=${encodeURIComponent(file.name)}`);
    xhr.setRequestHeader("X-Ordixa-Token", TOKEN);
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.upload.addEventListener("progress", (event) => {
      if (!event.lengthComputable) return;
      pending.percent = Math.min(99, Math.round((event.loaded / event.total) * 100));
      const bar = document.querySelector(".progress > span");
      if (bar) bar.style.width = `${pending.percent}%`;
      const label = document.querySelector(".progress")?.previousElementSibling;
      if (label) label.textContent = `Adding… ${pending.percent}% of ${bytes(pending.size)}`;
    });
    const done = (ok, message) => {
      delete state.uploads[role];
      if (message) toast(message, !ok);
      rerender();
      resolve(ok);
    };
    xhr.addEventListener("load", async () => {
      let data = null;
      try { data = JSON.parse(xhr.responseText); } catch { data = null; }
      if (xhr.status >= 200 && xhr.status < 300 && data) {
        state.view = data.view;
        state.descriptions = {};
        state.rows = null;
        state.check = null;
        if (!state.view.session.title) {
          const title = file.name.replace(/\.(csv|xlsx)$/i, "");
          try { state.view = await api("PATCH", `/sessions/${state.view.session.id}`, { title }); } catch { /* the title is optional */ }
        }
        announce(`${file.name} added`);
        done(true, data.file.errors.length ? `${file.name} was added but can't be read. See the message below it.` : `Added ${file.name}`);
      } else {
        done(false, (data && data.error) || `${file.name} could not be added.`);
      }
    });
    xhr.addEventListener("error", () => done(false, "The file could not be added. Check that Ordixa is still running."));
    xhr.addEventListener("abort", () => done(false, `Stopped adding ${file.name}. Nothing was kept.`));
    xhr.send(file);
  });
}

async function removeFile(role, file) {
  if (!window.confirm(`Remove ${file.original_name} from this work? The copy Ordixa kept is deleted; your original file is not affected.`)) return;
  try {
    state.view = await api("DELETE", `/sessions/${state.view.session.id}/files/${role}/${file.id}`);
    state.descriptions = {};
    state.rows = null;
    state.check = null;
    toast(`Removed ${file.original_name}`);
    rerender();
  } catch (error) { toast(error.message, true); }
}

async function describe(role, file) {
  const key = `${role}:${file.id}`;
  if (!state.descriptions[key]) {
    state.descriptions[key] = await api("GET", `/sessions/${state.view.session.id}/files/${role}/${file.id}`);
  }
  return state.descriptions[key];
}

async function togglePreview(role, file) {
  const key = `${role}:${file.id}`;
  if (state.previews[key]) {
    delete state.previews[key];
    rerender();
    return;
  }
  try {
    await describe(role, file);
    state.previews[key] = true;
    rerender();
  } catch (error) { toast(error.message, true); }
}

function previewPanel(role, file) {
  const description = state.descriptions[`${role}:${file.id}`];
  if (!description) return null;
  const kinds = {
    identifier: "codes", digits: "numbers", number: "numbers", amount: "amounts", date: "dates",
    "date text": "dates", text: "text", email: "emails", phone: "phone numbers", mixed: "mixed", empty: "empty",
  };
  const numeric = numericColumns(description.header, description.preview);
  return h("div", { class: "stack" },
    h("p", { class: "small muted" }, `First ${description.preview.length} of ${plural(description.rows || 0, "row")}, exactly as stored in the file.`),
    h("div", { class: "scroll tall" },
      h("table", { class: "data" },
        h("thead", null, h("tr", null, description.header.map((name, index) => {
          const column = description.columns.find((c) => c.position === index + 1);
          return h("th", { scope: "col" }, name || `(column ${index + 1})`, column ? h("div", { class: "small", style: "font-weight:400;opacity:.8" }, kinds[column.basic_type] || column.basic_type) : null);
        }))),
        h("tbody", null, description.preview.map((cells) => h("tr", null, cells.map((cell, index) => h("td", { class: numeric[index] ? "num" : null, title: cell }, cell))))))));
}

function looksNumeric(text) {
  return /^[-(]?[$€£¥]?\d[\d,]*(\.\d+)?\)?$/.test(String(text || "").trim());
}

/* right-aligns a column only when it holds amounts or counts: every filled
   cell is a number, none has a leading zero, and the header isn't a code */
function numericColumns(header, rows) {
  return header.map((name, index) => {
    if (/(^|[^a-z])(sku|id|code|ref|key|number|no)([^a-z]|$)/i.test(name)) return false;
    let seen = false;
    for (const row of rows) {
      const cell = String(row[index] || "").trim();
      if (!cell) continue;
      if (!looksNumeric(cell) || /^0\d/.test(cell)) return false;
      seen = true;
    }
    return seen;
  });
}

async function toColumns() {
  if (!state.view.session.settings) {
    try {
      const suggestion = await api("GET", `/sessions/${state.view.session.id}/suggestion`);
      await api("PUT", `/sessions/${state.view.session.id}/settings`, { settings: suggestion.settings });
      state.questions = suggestion.questions;
    } catch (error) {
      toast(error.message, true);
      return;
    }
  }
  go(sessionPath("columns"));
}

/* step 2: columns */

async function renderColumns(content) {
  const view = state.view;
  const settings = view.session.settings;
  const workflow = view.session.workflow;
  const loading = h("p", { class: "muted" }, "Reading your files…");
  put(content, loading);
  const descriptions = {};
  for (const role of ROLES[workflow]) {
    const first = view.files[role][0];
    if (first) descriptions[role] = await describe(role, first);
  }
  loading.remove();
  state.settings = settings;
  if (workflow === "reconcile") renderReconcileEditor(content, descriptions);
  else renderTableEditor(content, descriptions[ROLES[workflow][0]], descriptions);
}

function sampleValues(description, source) {
  const index = description.header.indexOf(source);
  if (index < 0) return [];
  const seen = [];
  for (const row of description.preview) {
    const cell = (row[index] || "").trim();
    if (cell && !seen.includes(cell)) seen.push(cell);
    if (seen.length >= 3) break;
  }
  return seen;
}

function uniqueName(source, taken) {
  let name = source.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 60);
  if (!name || !/^[a-z]/.test(name)) name = name ? `column_${name}` : "column";
  let candidate = name;
  let number = 2;
  while (taken.has(candidate)) candidate = `${name}_${number++}`;
  taken.add(candidate);
  return candidate;
}

function buildRows(settings, header) {
  const columns = settings.columns || {};
  const key = (settings.table && settings.table.key) || [];
  const ignore = (settings.compare && settings.compare.ignore) || [];
  const rows = [];
  const used = new Set();
  for (const [name, column] of Object.entries(columns)) {
    rows.push({
      keep: true, name, source: column.source, type: column.type || "text",
      required: Boolean(column.required), whitespace: column.whitespace || "trim",
      key: key.includes(name), compare: !ignore.includes(name), missing: !header.includes(column.source),
    });
    used.add(column.source);
  }
  const taken = new Set(rows.map((row) => row.name));
  for (const source of header) {
    if (!source || used.has(source)) continue;
    rows.push({ keep: false, name: uniqueName(source, taken), source, type: "text", required: false, whitespace: "trim", key: false, compare: true, missing: false });
  }
  return rows;
}

function rowsToSettings() {
  const settings = state.settings;
  const columns = {};
  const key = [];
  const ignore = [];
  for (const row of state.rows) {
    if (!row.keep) continue;
    const column = { source: row.source, type: row.type, required: row.required || row.key };
    if (row.type === "text" && row.whitespace === "collapse") column.whitespace = "collapse";
    columns[row.name] = column;
    if (row.key) key.push(row.name);
    else if (!row.compare) ignore.push(row.name);
  }
  settings.columns = columns;
  settings.table = settings.table || {};
  settings.table.key = key;
  if (!settings.table.blank_values) settings.table.blank_values = [];
  if (!settings.table.date_formats) settings.table.date_formats = [];
  if (settings.workflow.kind === "compare") settings.compare = { ...(settings.compare || {}), ignore };
  const money = Object.values(columns).some((c) => c.type === "decimal");
  if (money && !settings.amounts) settings.amounts = defaultAmounts();
  if (!money) delete settings.amounts;
  return settings;
}

function defaultAmounts() {
  return { currency: "USD", currency_symbols: ["$"], thousands_separator: ",", decimal_separator: ".", max_decimal_places: 2, negative_styles: ["minus"] };
}

function renderTableEditor(content, description, descriptions) {
  const settings = state.settings;
  const compare = settings.workflow.kind === "compare";
  if (!state.rows) state.rows = buildRows(settings, description.header);
  const editor = h("div");
  put(content,
    head("Choose the columns to keep",
      compare
        ? "Pick the columns to compare, say what kind of value each one holds, and choose the code that identifies each record in both files."
        : "Pick the columns to keep, and say what kind of value each one holds. Ordixa uses this to tidy values and to spot the rows it can't use."),
    editor);
  drawTableEditor(editor, description, compare, descriptions);
}

function drawTableEditor(editor, description, compare, descriptions) {
  const settings = state.settings;
  const rows = state.rows;
  const redraw = () => { rowsToSettings(); clear(editor); drawTableEditor(editor, description, compare, descriptions); };
  const kept = rows.filter((row) => row.keep);
  const typeOptions = Object.entries(state.status.type_labels);

  const body = rows.map((row) => {
    const samples = sampleValues(description, row.source);
    const nameInput = h("input", { type: "text", value: row.name, "aria-label": `Name in results for ${row.source}`, disabled: !row.keep || null, spellcheck: "false" });
    nameInput.addEventListener("input", () => {
      row.name = nameInput.value.trim();
      nameInput.classList.toggle("invalid", !/^[a-z][a-z0-9_]{0,63}$/.test(row.name));
      rowsToSettings();
    });
    nameInput.addEventListener("change", redraw);
    const select = h("select", { "aria-label": `Kind of value in ${row.source}`, disabled: !row.keep || null },
      typeOptions.map(([value, label]) => h("option", { value, selected: value === row.type }, label)));
    select.addEventListener("change", () => {
      row.type = select.value;
      if (row.key && !["text", "identifier"].includes(row.type)) row.key = false;
      redraw();
    });
    const option = (label, field, extra) => {
      const box = h("input", { type: "checkbox", checked: row[field], disabled: !row.keep || (extra && extra.disabled) || null });
      box.addEventListener("change", () => {
        row[field] = box.checked;
        if (field === "key" && box.checked) { row.type = row.type === "text" ? "text" : "identifier"; row.required = true; }
        redraw();
      });
      return h("label", { class: "check sub" }, box, h("span", null, label));
    };
    const keep = h("input", { type: "checkbox", checked: row.keep, "aria-label": `Keep ${row.source}` });
    keep.addEventListener("change", () => {
      row.keep = keep.checked;
      if (!row.keep) row.key = false;
      redraw();
    });
    return h("tr", { class: row.keep ? null : "off" },
      h("td", { class: "center keep", "data-label": "Keep" }, h("label", { class: "check" }, keep)),
      h("td", { class: "from", "data-label": "Column in your file" },
        h("div", { class: "source" }, row.source),
        samples.length ? h("div", { class: "samples-line", title: samples.join(", ") }, `e.g. ${samples.join(", ")}`) : h("div", { class: "samples-line" }, "Empty in the first rows"),
        row.missing ? note("bad", `Not in ${description.file}.`) : null),
      h("td", { class: "name", "data-label": "Name in results" }, nameInput),
      h("td", { class: "kind", "data-label": "Kind of value" }, select, row.keep ? h("div", { class: "small muted sub" }, TYPE_HELP[row.type]) : null),
      h("td", { class: "rules", "data-label": "Rules" }, !row.keep ? h("span", { class: "small muted" }, "Left out of the results") : [
        option("Identifies each record", "key", { disabled: !["text", "identifier"].includes(row.type) }),
        row.key ? null : option("Must have a value", "required"),
        compare && !row.key ? option("Compare changes", "compare") : null,
        row.type === "text" ? h("label", { class: "check sub" }, (() => {
          const box = h("input", { type: "checkbox", checked: row.whitespace === "collapse", disabled: !row.keep || null });
          box.addEventListener("change", () => { row.whitespace = box.checked ? "collapse" : "trim"; redraw(); });
          return box;
        })(), h("span", null, "Also tidy double spaces")) : null]));
  });

  const keyRows = kept.filter((row) => row.key);
  const notes = [];
  if (!kept.length) notes.push(note("bad", "Choose at least one column to keep."));
  if (compare && !keyRows.length) notes.push(note("bad", h("strong", null, "Choose the column that identifies each record. "), "Ordixa matches the two files by it, such as a SKU or product code. Tick ", h("em", null, "Identifies each record"), " on that column."));
  if (!compare && !keyRows.length) notes.push(note("info", "Tip: if one column identifies each record, such as a SKU, tick ", h("em", null, "Identifies each record"), ". Ordixa then holds back rows that share a code but disagree, instead of keeping both."));
  const personal = description.columns.filter((c) => c.personal && kept.some((row) => row.source === c.name));
  for (const column of personal) notes.push(note("warn", `${column.name} looks like it holds email addresses or phone numbers. It will be in the results; untick it if you don't need it.`));
  const dropped = rows.filter((row) => !row.keep).length;

  put(editor,
    h("div", { class: "panel" },
      h("div", { class: "section-head" },
        h("h2", null, "Columns"),
        h("p", { class: "small muted" }, `${plural(kept.length, "column")} kept${dropped ? `, ${dropped} left out of the results` : ""}. Columns you leave out are never copied anywhere.`)),
      notes.length ? h("div", { style: "margin-bottom:16px" }, notes) : null,
      h("div", { class: "mapping-wrap" },
        h("table", { class: "mapping" },
          h("thead", null, h("tr", null, h("th", null, "Keep"), h("th", null, "Column in your file"), h("th", null, "Name in results"), h("th", null, "Kind of value"), h("th", null, "Rules"))),
          h("tbody", null, body)))),
    valueSettings(kept, description, redraw),
    profileActions(() => rowsToSettings()));
}

function valueSettings(kept, description, redraw) {
  const settings = state.settings;
  const table = settings.table;
  const panels = [];
  const dates = kept.filter((row) => row.type === "date");
  if (dates.length) {
    const hints = description.columns.filter((c) => dates.some((row) => row.source === c.name));
    panels.push(dateFormatPanel(table, "date_formats", hints, redraw, dates.map((r) => r.source).join(", ")));
  }
  if (kept.some((row) => row.type === "decimal")) panels.push(moneyPanel(redraw));
  panels.push(blankPanel(table, redraw));
  return h("div", { class: "section" }, h("h2", { style: "margin-bottom:12px" }, "How values are written"), h("div", { class: "settings-grid" }, panels));
}

function dateFormatPanel(target, field, hints, redraw, columnsText) {
  const chosen = target[field] || [];
  const found = new Set(hints.flatMap((hint) => hint.date_formats || []));
  const ambiguous = hints.flatMap((hint) => hint.ambiguous_date_formats || []);
  const known = state.status.date_formats;
  const labels = { ...known };
  for (const format of chosen) if (!labels[format]) labels[format] = format;
  const box = (format) => {
    const input = h("input", { type: "checkbox", checked: chosen.includes(format) });
    input.addEventListener("change", () => {
      const list = new Set(target[field] || []);
      if (input.checked) list.add(format); else list.delete(format);
      target[field] = Object.keys(labels).filter((name) => list.has(name));
      redraw();
    });
    return h("label", { class: "check" }, input, h("span", null, labels[format], found.has(format) ? h("span", { class: "chip info", style: "margin-left:6px" }, "in your file") : null));
  };
  const both = ambiguous.some(([us, eu]) => chosen.includes(us) && chosen.includes(eu));
  const open = ambiguous.filter(([us, eu]) => !chosen.includes(us) && !chosen.includes(eu));
  return h("div", { class: "panel" },
    h("h3", null, "Dates"),
    h("p", { class: "small muted" }, `Tick every way dates are written in ${columnsText}. Ordixa never guesses.`),
    open.length ? note("warn", h("strong", null, "Some dates could be read two ways. "), "In 03/04/2026, is 03 the month or the day? Tick only the one your system uses.") : null,
    both ? note("bad", "Month-first and day-first are both ticked, so a date like 03/04/2026 can't be read. Keep only one.") : null,
    !chosen.length ? note("bad", "Tick at least one date format.") : null,
    h("div", { class: "formats" }, Object.keys(labels).filter((format) => found.has(format) || chosen.includes(format)).map(box)),
    h("details", { class: "more" }, h("summary", null, "Other date formats"),
      h("div", { class: "formats" }, Object.keys(labels).filter((format) => !found.has(format) && !chosen.includes(format)).map(box))));
}

function moneyPanel(redraw) {
  const settings = state.settings;
  const amounts = settings.amounts || (settings.amounts = defaultAmounts());
  const currency = h("input", { type: "text", value: amounts.currency, maxlength: 3, style: "max-width:7em;text-transform:uppercase", spellcheck: "false" });
  currency.addEventListener("input", () => {
    amounts.currency = currency.value.trim().toUpperCase();
    currency.classList.toggle("invalid", !/^[A-Z]{3}$/.test(amounts.currency));
  });
  const symbols = h("input", { type: "text", value: (amounts.currency_symbols || []).join(" "), style: "max-width:10em", spellcheck: "false" });
  symbols.addEventListener("input", () => { amounts.currency_symbols = symbols.value.split(/\s+/).filter(Boolean); });
  const styleValue = amounts.decimal_separator === "," ? (amounts.thousands_separator === "." ? "eu" : "eu-space") : amounts.thousands_separator === "" ? "plain" : "us";
  const style = h("select", null,
    h("option", { value: "us", selected: styleValue === "us" }, "1,234.56"),
    h("option", { value: "eu", selected: styleValue === "eu" }, "1.234,56"),
    h("option", { value: "eu-space", selected: styleValue === "eu-space" }, "1 234,56"),
    h("option", { value: "plain", selected: styleValue === "plain" }, "1234.56 (no thousands separator)"));
  style.addEventListener("change", () => {
    const pick = { us: [",", "."], eu: [".", ","], "eu-space": [" ", ","], plain: ["", "."] }[style.value];
    amounts.thousands_separator = pick[0];
    amounts.decimal_separator = pick[1];
  });
  const negative = (value, label) => {
    const input = h("input", { type: "checkbox", checked: (amounts.negative_styles || []).includes(value) });
    input.addEventListener("change", () => {
      const list = new Set(amounts.negative_styles || []);
      if (input.checked) list.add(value); else list.delete(value);
      amounts.negative_styles = ["minus", "parentheses"].filter((name) => list.has(name));
      if (!amounts.negative_styles.length) { amounts.negative_styles = ["minus"]; redraw(); }
    });
    return h("label", { class: "check" }, input, h("span", null, label));
  };
  return h("div", { class: "panel" },
    h("h3", null, "Money"),
    h("p", { class: "small muted" }, "Amounts are read exactly, to the cent, and never rounded. Anything that doesn't fit is held back."),
    h("div", { class: "fields", style: "margin-top:12px" },
      h("label", { class: "field" }, "Currency code", h("span", { class: "hint" }, "Three letters, such as USD."), currency),
      h("label", { class: "field" }, "Currency symbols in the file", h("span", { class: "hint" }, "Removed before reading, such as $. Separate several with spaces."), symbols),
      h("label", { class: "field" }, "Number style", style),
      h("div", { class: "field" }, "Negative amounts are written as", negative("minus", "-12.00"), negative("parentheses", "(12.00)"))));
}

function blankPanel(table, redraw) {
  const tokens = table.blank_values || (table.blank_values = []);
  const input = h("input", { type: "text", placeholder: "Add, e.g. N/A", maxlength: 32, style: "max-width:12em", spellcheck: "false" });
  const add = () => {
    const value = input.value.trim();
    if (value && !tokens.includes(value)) { tokens.push(value); redraw(); }
    input.value = "";
  };
  input.addEventListener("keydown", (event) => { if (event.key === "Enter") { event.preventDefault(); add(); } });
  return h("div", { class: "panel" },
    h("h3", null, "Placeholder text that means empty"),
    h("p", { class: "small muted" }, "Cells holding exactly one of these are treated as blank. A required column that is blank holds its row back."),
    h("div", { class: "tokens" },
      tokens.length ? tokens.map((token) => h("span", { class: "token" }, token,
        h("button", { type: "button", "aria-label": `Remove ${token}`, onclick: () => { table.blank_values = tokens.filter((t) => t !== token); redraw(); } }, "×"))) : h("span", { class: "small muted" }, "None"),
      input,
      h("button", { class: "button small", type: "button", onclick: add }, "Add")));
}

function profileActions(sync) {
  const settings = state.settings;
  const meta = settings.profile || (settings.profile = {});
  const name = h("input", { type: "text", value: meta.name || "", spellcheck: "false", maxlength: 64 });
  name.addEventListener("input", () => {
    meta.name = name.value.trim();
    name.classList.toggle("invalid", !/^[a-z0-9][a-z0-9-]{0,63}$/.test(meta.name));
  });
  const fileInput = h("input", { type: "file", accept: ".toml", class: "live" });
  fileInput.addEventListener("change", () => importProfile(fileInput.files[0]));
  return h("div", null,
    h("div", { class: "panel section" },
      h("h2", null, "Save these settings"),
      h("p", { class: "small muted" }, "Settings are saved with this work. You can also keep them as a file to reuse next month, or to run the same thing from the command line."),
      h("div", { class: "settings-grid", style: "margin-top:12px;align-items:end" },
        h("label", { class: "field" }, "Name", h("span", { class: "hint" }, "Lowercase letters, numbers, and hyphens."), name),
        h("div", { class: "actions", style: "margin:0" },
          h("button", { class: "button", type: "button", onclick: async () => { sync(); if (await saveSettings()) window.location.href = `/api/sessions/${state.view.session.id}/settings.toml`; } }, "Download settings file"),
          h("label", { class: "button" }, fileInput, "Load a settings file")))),
    h("div", { class: "actions" },
      h("a", { class: "button", href: `#${sessionPath("files")}` }, "Back"),
      h("span", { class: "spacer" }),
      h("button", { class: "button primary", type: "button", onclick: async () => { sync(); if (await saveSettings()) go(sessionPath("check")); } }, "Next: check")));
}

async function saveSettings() {
  try {
    await api("PUT", `/sessions/${state.view.session.id}/settings`, { settings: state.settings });
    state.view.session.settings = state.settings;
    state.check = null;
    return true;
  } catch (error) {
    toast(error.message, true);
    return false;
  }
}

async function importProfile(file) {
  if (!file) return;
  try {
    const response = await fetch(`/api/sessions/${state.view.session.id}/settings/import`, {
      method: "POST", headers: { "X-Ordixa-Token": TOKEN, "Content-Type": "application/toml" }, body: await file.text(), credentials: "same-origin",
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error);
    state.view.session.settings = data.settings;
    state.rows = null;
    state.check = null;
    toast(`Loaded settings from ${file.name}`);
    rerender();
  } catch (error) { toast(error.message || "That file could not be loaded.", true); }
}

/* reconciliation mapping */

function renderReconcileEditor(content, descriptions) {
  const editor = h("div");
  put(content,
    head("Match up your columns", "Tell Ordixa which column holds each piece of information. Suggestions are filled in from your column names; check each one."),
    editor);
  drawReconcileEditor(editor, descriptions);
}

function drawReconcileEditor(editor, descriptions) {
  const settings = state.settings;
  const redraw = () => { clear(editor); drawReconcileEditor(editor, descriptions); };
  settings.datasets = settings.datasets || {};
  for (const role of ROLES.reconcile) {
    if (descriptions[role] && !settings.datasets[role]) settings.datasets[role] = role === "customers" ? { required: false, columns: {} } : { columns: {} };
    if (!descriptions[role] && role === "customers") delete settings.datasets.customers;
  }
  const panels = ROLES.reconcile.filter((role) => descriptions[role]).map((role) => datasetPanel(role, descriptions[role], redraw));
  const amounts = settings.amounts || (settings.amounts = defaultAmounts());
  settings.reconciliation = settings.reconciliation || { missing_customer: "warning" };
  const missing = h("select", null,
    h("option", { value: "warning", selected: settings.reconciliation.missing_customer !== "error" }, "Still check the order, and note it"),
    h("option", { value: "error", selected: settings.reconciliation.missing_customer === "error" }, "Hold the order back"));
  missing.addEventListener("change", () => { settings.reconciliation.missing_customer = missing.value; });
  put(editor,
    panels,
    h("div", { class: "section" }, h("h2", { style: "margin-bottom:12px" }, "How values are written"),
      h("div", { class: "settings-grid" },
        moneyPanel(redraw),
        identifierPanel(redraw),
        descriptions.customers ? h("div", { class: "panel" }, h("h3", null, "Orders whose customer is missing"),
          h("p", { class: "small muted" }, "When an order's customer number isn't in the customer file:"),
          h("div", { style: "margin-top:10px" }, missing)) : null)),
    profileActions(() => {}));
  void amounts;
}

function headerSelect(header, current, label, onChange) {
  const select = h("select", { "aria-label": label },
    h("option", { value: "", selected: !current }, "Not in this file"),
    header.filter(Boolean).map((name) => h("option", { value: name, selected: name === current }, name)));
  select.addEventListener("change", () => onChange(select.value));
  return select;
}

function datasetPanel(role, description, redraw) {
  const dataset = state.settings.datasets[role];
  dataset.columns = dataset.columns || {};
  const fields = state.status.dataset_fields[role];
  const labels = state.status.field_labels;
  const known = [...fields.required, ...fields.optional];
  const order = FIELD_ORDER[role].filter((field) => known.includes(field)).concat(known.filter((field) => !FIELD_ORDER[role].includes(field)));
  const rows = order.map((field) => {
    const required = fields.required.includes(field);
    const current = dataset.columns[field];
    const set = (value) => {
      if (value) dataset.columns[field] = value; else delete dataset.columns[field];
      redraw();
    };
    let control;
    if (field === "name") {
      const parts = Array.isArray(current) ? current : current ? [current] : [];
      const update = (index, value) => {
        const next = [...parts];
        next[index] = value;
        const clean = next.filter(Boolean);
        set(clean.length > 1 ? clean : clean[0] || "");
      };
      control = h("div", { class: "pair" },
        headerSelect(description.header, parts[0], "Customer name, first part", (v) => update(0, v)),
        headerSelect(description.header, parts[1], "Customer name, second part (optional)", (v) => update(1, v)));
    } else {
      control = headerSelect(description.header, current, labels[field], set);
    }
    const sources = Array.isArray(current) ? current : current ? [current] : [];
    const samples = sources.flatMap((source) => sampleValues(description, source)).slice(0, 3);
    return h("div", { class: "field-row" },
      h("div", { class: "label" }, labels[field], required ? h("span", { class: "req" }, "Required") : null),
      h("div", null, control,
        samples.length ? h("div", { class: "samples-line small", style: "margin-top:4px" }, `e.g. ${samples.join(", ")}`) : null,
        required && !current ? note("bad", "Choose the column that holds this.") : null));
  });

  const extras = [];
  if (dataset.columns.date || dataset.columns.payment_date) {
    const sources = [dataset.columns.date, dataset.columns.payment_date].filter(Boolean);
    const hints = description.columns.filter((c) => sources.includes(c.name));
    extras.push(dateFormatPanel(dataset, "date_formats", hints, redraw, sources.join(", ")));
  }
  if (role === "payments") extras.push(...paymentPanels(dataset, description, redraw));
  return h("section", { class: "panel section" },
    h("div", { class: "section-head" }, h("h2", null, ROLE_TEXT[role].label),
      h("p", { class: "small muted" }, `From ${description.file}${state.view.files[role].length > 1 ? ` and ${plural(state.view.files[role].length - 1, "other file")}` : ""}`)),
    h("div", { class: "fields" }, rows),
    extras.length ? h("div", { class: "settings-grid section", style: "margin-top:24px" }, extras) : null);
}

function paymentPanels(dataset, description, redraw) {
  const panels = [];
  const maps = dataset.value_maps || (dataset.value_maps = {});
  for (const [field, title, targets] of [["transaction_type", "Kinds of transaction", TRANSACTION_TARGETS], ["status", "Payment statuses", STATUS_TARGETS]]) {
    const column = dataset.columns[field];
    if (!column) continue;
    const cacheKey = `payments:${column}`;
    const values = state.values[cacheKey];
    if (!values) {
      api("GET", `/sessions/${state.view.session.id}/values?role=payments&column=${encodeURIComponent(column)}`)
        .then((data) => { state.values[cacheKey] = data.values; redraw(); })
        .catch((error) => toast(error.message, true));
    }
    const mapping = maps[field] || {};
    const excluded = dataset.excluded_statuses || [];
    const lookup = (value) => {
      if (field === "status" && excluded.some((v) => v.toLowerCase() === value.toLowerCase())) return "exclude";
      const hit = Object.entries(mapping).find(([source]) => source.toLowerCase() === value.toLowerCase());
      return hit ? hit[1] : "";
    };
    const unset = (values || []).filter((item) => !lookup(item.value));
    panels.push(h("div", { class: "panel" },
      h("h3", null, title),
      h("p", { class: "small muted" }, `What each value in ${column} means.`),
      !values ? h("p", { class: "muted small" }, "Reading values…")
        : h("div", { class: "values" }, values.map((item) => {
            const select = h("select", { "aria-label": `Meaning of ${item.value}` },
              targets.map(([value, label]) => h("option", { value, selected: value === lookup(item.value) }, label)));
            select.addEventListener("change", () => {
              const next = { ...(maps[field] || {}) };
              for (const source of Object.keys(next)) if (source.toLowerCase() === item.value.toLowerCase()) delete next[source];
              let list = (dataset.excluded_statuses || []).filter((v) => v.toLowerCase() !== item.value.toLowerCase());
              if (select.value === "exclude") list.push(item.value);
              else if (select.value) next[item.value] = select.value;
              if (Object.keys(next).length) maps[field] = next; else delete maps[field];
              if (field === "status") { if (list.length) dataset.excluded_statuses = list; else delete dataset.excluded_statuses; }
              redraw();
            });
            return h("div", { class: "value-row" },
              h("div", { class: "value" }, item.value, h("span", { class: "count" }, plural(item.count, "row"))),
              select);
          })),
      unset.length ? note("warn", `${plural(unset.length, "value")} not set yet. Rows with them are held back with a reason.`) : null));
  }
  const sign = h("select", null,
    h("option", { value: "positive", selected: dataset.refund_amounts !== "negative" }, "As positive amounts, such as 12.00"),
    h("option", { value: "negative", selected: dataset.refund_amounts === "negative" }, "As negative amounts, such as -12.00"));
  sign.addEventListener("change", () => { dataset.refund_amounts = sign.value; });
  panels.push(h("div", { class: "panel" }, h("h3", null, "Refunds"),
    h("p", { class: "small muted" }, "How refunds appear in the payments file:"),
    h("div", { style: "margin-top:10px" }, sign)));
  return panels;
}

function identifierPanel(redraw) {
  const settings = state.settings;
  const ids = settings.identifiers || (settings.identifiers = {});
  const families = [["order_id", "Order numbers"], ["payment_id", "Payment IDs"], ["customer_id", "Customer numbers"]];
  return h("div", { class: "panel" },
    h("h3", null, "Matching IDs"),
    h("p", { class: "small muted" }, "Payments are matched to orders only by an exact order number. Nothing is matched by amount or date."),
    h("div", { class: "fields", style: "margin-top:12px" }, families.map(([field, label]) => {
      const mode = ids[field] || "numeric";
      const select = h("select", null,
        h("option", { value: "text", selected: mode === "text" }, "Exactly as written (keeps letters and leading zeros)"),
        h("option", { value: "numeric", selected: mode === "numeric" }, "As numbers (007 and 7 are the same)"));
      select.addEventListener("change", () => { ids[field] = select.value; redraw(); });
      const prefixes = mode === "numeric" ? Object.entries(settings.datasets).filter(([, dataset]) => dataset.columns && dataset.columns[field]).map(([role, dataset]) => {
        const input = h("input", { type: "text", value: (dataset.id_prefixes || {})[field] || "", style: "max-width:8em", spellcheck: "false", placeholder: "none" });
        input.addEventListener("input", () => {
          const value = input.value.trim();
          dataset.id_prefixes = dataset.id_prefixes || {};
          if (value) dataset.id_prefixes[field] = value; else delete dataset.id_prefixes[field];
          if (!Object.keys(dataset.id_prefixes).length) delete dataset.id_prefixes;
        });
        return h("label", { class: "field", style: "font-weight:400" }, `Remove this prefix in ${ROLE_TEXT[role].label.toLowerCase()}, such as # or PMT-`, input);
      }) : [];
      return h("div", { class: "field" }, h("span", null, label), select, prefixes);
    })));
}

/* step 3: check and run */

async function renderCheck(content) {
  const view = state.view;
  const box = h("div");
  put(content, head("Check and run", "Ordixa reads your files with these settings and reports anything it can't use, before writing anything."), box);
  if (state.job && state.job.session_id === view.session.id && !["completed", "failed", "cancelled"].includes(state.job.state)) {
    drawCheck(box);
    watchJob(box);
    return;
  }
  await runCheck(box);
}

async function runCheck(box) {
  state.checking = true;
  put(clear(box), h("div", { class: "panel running" }, h("div", { class: "spinner", "aria-hidden": "true" }), h("p", null, "Checking your files…")));
  try {
    state.check = await api("POST", `/sessions/${state.view.session.id}/check`, { settings: state.view.session.settings });
  } catch (error) {
    state.check = { readiness: "blocked", problems: [{ level: "blocking", message: error.message }], preflight: null };
  }
  state.checking = false;
  drawCheck(box);
}

function drawCheck(box) {
  const check = state.check || { readiness: "blocked", problems: [], preflight: null };
  const view = state.view;
  const workflow = view.session.workflow;
  const level = check.readiness;
  const headline = {
    ready: ["✓", "Everything looks right.", "You can run it now."],
    review: ["!", "Ready to run, with a few things to look at.", "None of these stop the run. Rows Ordixa can't use are held back and listed with a reason."],
    blocked: ["×", "Not ready yet.", "Fix the items below, then check again."],
  }[level];
  const problems = (check.problems || []).map((item) =>
    note(item.level === "blocking" ? "bad" : "warn", item.message, " ", h("a", { href: `#${sessionPath(item.where && item.where.startsWith("files") ? "files" : "columns")}` }, item.where && item.where.startsWith("files") ? "Go to files" : "Change settings")));
  const report = check.preflight;
  const findings = report ? report.findings.map((item) =>
    note(item.level === "blocking" ? "bad" : item.level === "warning" ? "warn" : "info",
      capital(item.message), item.fix ? h("span", { class: "fix" }, item.fix) : null)) : [];

  const job = state.job && state.job.session_id === view.session.id ? state.job : null;
  const running = job && !["completed", "failed", "cancelled"].includes(job.state);
  const runButton = h("button", { class: "button primary", type: "button", disabled: level === "blocked" || running || null, onclick: () => startRun(box) }, workflow === "reconcile" ? "Run reconciliation" : workflow === "compare" ? "Run comparison" : "Run cleaning");

  put(clear(box),
    h("div", { class: `panel readiness ${level}` },
      h("div", { class: "badge", "aria-hidden": "true" }, headline[0]),
      h("div", null, h("h2", null, headline[1]), h("p", { class: "muted" }, headline[2]))),
    view.incomplete.length ? note("warn", h("strong", null, "An earlier run was stopped before it finished. "),
      "It left an unfinished folder, which is not a result. ",
      h("button", { class: "button small", type: "button", onclick: clearIncomplete }, "Remove it")) : null,
    problems.length ? h("div", { class: "section" }, h("h2", { style: "margin-bottom:12px" }, "Settings to fix"), problems) : null,
    report ? h("div", { class: "section" }, h("h2", { style: "margin-bottom:12px" }, "What Ordixa will do"), previewCounts(workflow, report)) : null,
    findings.length ? h("div", { class: "section" }, h("h2", { style: "margin-bottom:12px" }, "Worth knowing"), findings)
      : level === "review" ? h("div", { class: "section" }, note("warn", "Some rows will be held back. After the run, each one is listed with the reason and how to fix it.")) : null,
    h("div", { class: "job-area" }, job ? jobPanel(job, box) : null),
    h("div", { class: "actions" },
      h("a", { class: "button", href: `#${sessionPath("columns")}` }, "Back to settings"),
      h("button", { class: "button", type: "button", disabled: running || null, onclick: () => runCheck(box) }, "Check again"),
      h("span", { class: "spacer" }),
      runButton));
}

function countGrid(items) {
  return h("div", { class: "counts" }, items.filter(Boolean).map(([n, label]) => h("div", null, h("div", { class: "n" }, Number(n).toLocaleString()), h("div", { class: "l" }, label))));
}

function previewCounts(workflow, report) {
  if (workflow === "reconcile") {
    const rows = report.rows;
    if (!rows) return note("info", "Row checks run once the files can be read.");
    const sc = rows.status_counts || {};
    return h("div", { class: "stack" },
      Object.entries(rows.counts).map(([role, c]) => h("div", null,
        h("h3", null, ROLE_TEXT[role].label),
        countGrid([[c.rows, "rows read"], [c.accepted, "can be used"], [c.rejected, "held back"], c.excluded ? [c.excluded, "left out on purpose"] : null, c.accepted_with_warnings ? [c.accepted_with_warnings, "kept with a warning"] : null]))),
      h("div", null, h("h3", null, "Expected order status"), countGrid([[rows.orders, "orders to check"], [sc.paid || 0, "paid"], [sc.partial || 0, "part-paid"], [sc.unpaid || 0, "unpaid"], [sc.overpaid || 0, "overpaid"]])));
  }
  const parts = report.inputs.filter((entry) => entry.counts).map((entry) => h("div", null,
    h("h3", null, `${ROLE_TEXT[entry.role].label}: ${entry.file}`),
    countGrid([[entry.counts.rows_read, "rows read"], [entry.counts.rows_written, "can be used"], [entry.counts.rows_rejected, "held back"], [entry.counts.duplicate_rows_removed, "exact copies removed"]])));
  if (report.comparison) {
    const c = report.comparison;
    parts.push(h("div", null, h("h3", null, "Changes"), countGrid([[c.added, "added"], [c.removed, "removed"], [c.modified, "changed"], [c.unchanged, "unchanged"], [c.ambiguous_keys + c.invalid_keys, "can't be compared"]])));
    if (report.assessment) parts.push(assessmentNote(report.assessment));
  }
  return h("div", { class: "stack" }, parts);
}

function assessmentNote(assessment) {
  const kind = { complete: "ok", complete_with_warnings: "warn", incomplete: "bad" }[assessment.level];
  const title = {
    complete: "Complete: every record was compared.",
    complete_with_warnings: "Complete, with things to note.",
    incomplete: "Incomplete: some records or columns could not be compared, so “unchanged” does not cover them.",
  }[assessment.level];
  return note(kind, h("strong", null, title),
    assessment.findings.length ? h("ul", { style: "margin:6px 0 0;padding-left:18px" }, assessment.findings.map((item) => h("li", null, capital(item.message)))) : null);
}

async function clearIncomplete() {
  try {
    const result = await api("DELETE", `/sessions/${state.view.session.id}/incomplete`);
    toast(`Removed ${plural(result.removed, "unfinished folder")}`);
    state.view = await api("GET", `/sessions/${state.view.session.id}`);
    rerender();
  } catch (error) { toast(error.message, true); }
}

async function startRun(box) {
  try {
    const response = await api("POST", `/sessions/${state.view.session.id}/runs`, { settings: state.view.session.settings });
    state.job = response.job;
    state.check = response.check;
    announce("Run started");
    drawCheck(box);
    watchJob(box);
  } catch (error) {
    if (error.data && error.data.check) { state.check = error.data.check; drawCheck(box); }
    toast(error.message, true);
  }
}

function jobPanel(job, box) {
  const task = TASKS[job.workflow];
  if (job.state === "queued" || job.state === "running" || job.state === "cancelling") {
    return h("div", { class: "panel running section" },
      h("div", { class: "spinner", "aria-hidden": "true" }),
      h("div", { style: "flex:1" },
        h("h2", null, job.state === "cancelling" ? "Stopping…" : `${task.verb}…`),
        h("p", { class: "muted small job-time" }, `${Math.round(job.seconds)} seconds. Results appear only once everything is written and checked.`)),
      h("button", { class: "button danger", type: "button", disabled: job.state === "cancelling" || null, onclick: () => cancelJob(job, box) }, "Stop"));
  }
  if (job.state === "cancelled") {
    return h("div", { class: "section" }, note("info", h("strong", null, "Stopped. "), job.note || "Nothing was saved, and your files are unchanged."));
  }
  if (job.state === "failed") {
    return h("div", { class: "section" }, note("bad", h("strong", null, job.headline || "The run did not finish."), h("span", { class: "fix" }, job.advice), job.note ? h("span", { class: "fix" }, job.note) : null),
      job.messages.length ? h("details", { class: "more" }, h("summary", null, "Details from Ordixa"), h("pre", { class: "mono", style: "white-space:pre-wrap" }, job.messages.join("\n"))) : null);
  }
  return null;
}

let jobTimer = null;
function watchJob(box) {
  clearTimeout(jobTimer);
  const tick = async () => {
    const job = state.job;
    if (!job) return;
    try {
      state.job = await api("GET", `/jobs/${job.id}`);
    } catch (error) {
      state.job = { ...job, state: "failed", headline: "Lost contact with the run.", advice: error.message, messages: [] };
    }
    const current = state.job;
    if (!document.body.contains(box)) return;
    if (current.state === "completed") {
      toast(current.note || "Finished. Your results are ready.");
      announce("Finished");
      state.run = null;
      go(sessionPath("results", current.run));
      return;
    }
    if (["failed", "cancelled"].includes(current.state)) {
      state.view = await api("GET", `/sessions/${state.view.session.id}`);
      drawCheck(box);
      announce(current.state === "cancelled" ? "Run stopped" : "Run failed");
      return;
    }
    const time = box.querySelector(".job-time");
    if (time) time.textContent = `${Math.round(current.seconds)} seconds. Results appear only once everything is written and checked.`;
    else drawCheck(box);
    jobTimer = setTimeout(tick, 700);
  };
  jobTimer = setTimeout(tick, 400);
}

async function cancelJob(job, box) {
  try {
    state.job = await api("POST", `/jobs/${job.id}/cancel`);
    state.job.state = "cancelling";
    drawCheck(box);
  } catch (error) { toast(error.message, true); }
}

/* step 4: results */

async function renderResults(content, runName) {
  const id = state.view.session.id;
  if (!state.run || state.run.run !== runName) {
    state.run = await api("GET", `/sessions/${id}/runs/${runName}`);
    state.verify = null;
    state.tab = null;
    state.filter = null;
    state.page = 0;
  }
  const run = state.run;
  const tabs = resultTabs(run);
  if (!state.tab) state.tab = tabs[0].key;
  const verifyBox = h("span", { class: "verify" }, state.verify ? verifyBadge(state.verify) : h("span", { class: "chip quiet" }, "Checking the result files…"));
  put(content,
    head("Results", `${TASKS[run.workflow].kind}, finished ${when(run.finished_at)}.`),
    verdict(run, verifyBox),
    attention(run),
    h("section", { class: "section" }, h("h2", { style: "margin-bottom:12px" }, "Look through the results"), dataTabs(run, tabs)),
    h("div", { class: "two section" }, downloads(run), history(run)),
    technical(run));
  if (!state.verify) {
    try {
      state.verify = await api("GET", `/sessions/${id}/runs/${runName}/verify`);
    } catch (error) {
      state.verify = { passed: false, checks: [{ check: error.message, passed: false }] };
    }
    put(clear(verifyBox), verifyBadge(state.verify));
    const list = document.querySelector(".verify-list");
    if (list) put(clear(list), verifyList(state.verify));
  }
}

function verifyBadge(result) {
  return result.passed
    ? h("span", { class: "chip ok" }, `✓ Verified: all ${result.checks.length} file checks passed`)
    : h("span", { class: "chip bad" }, `${result.checks.filter((c) => !c.passed).length} file check(s) failed: don't use these results`);
}

function verifyList(result) {
  return h("ul", { class: "checks" }, result.checks.map((item) => h("li", null, h("span", { class: item.passed ? "yes" : "no", "aria-label": item.passed ? "passed" : "failed" }, item.passed ? "✓" : "×"), capital(item.check))));
}

function strip(parts) {
  const total = parts.reduce((sum, [n]) => sum + n, 0) || 1;
  return [
    h("div", { class: "strip", role: "img", "aria-label": parts.filter(([n]) => n).map(([n, , label]) => `${n} ${label}`).join(", ") },
      parts.filter(([n]) => n > 0).map(([n, kind]) => h("span", { class: kind, style: `flex:${n / total}` }))),
    h("ul", { class: "legend" }, parts.map(([n, kind, label]) => h("li", { class: kind }, h("strong", null, Number(n).toLocaleString()), " ", label))),
  ];
}

function figure(n) {
  return h("span", { class: "figure" }, Number(n).toLocaleString());
}

function verdict(run, verifyBox) {
  const c = run.counts;
  let sentence;
  let bars;
  let extra = null;
  if (run.workflow === "clean") {
    const held = c.rows_rejected;
    sentence = [figure(c.rows_written), ` of ${Number(c.rows_read).toLocaleString()} rows are clean and ready to use.`, held ? [" ", figure(held), ` ${held === 1 ? "needs" : "need"} a person to look at ${held === 1 ? "it" : "them"}.`] : " Nothing was held back."];
    bars = strip([[c.rows_written, "ok", "ready to use"], [c.rows_rejected, "bad", "held back"], [c.duplicate_rows_removed, "quiet", "exact copies removed"]]);
  } else if (run.workflow === "compare") {
    const unclassified = c.ambiguous_keys + c.invalid_keys;
    sentence = [figure(c.modified), ` ${c.modified === 1 ? "record" : "records"} changed, `, figure(c.added), " added and ", figure(c.removed), ` removed. ${Number(c.unchanged).toLocaleString()} unchanged.`];
    bars = strip([[c.unchanged, "ok", "unchanged"], [c.modified, "amber", "changed"], [c.added, "info", "added"], [c.removed, "bad", "removed"], [unclassified, "quiet", "couldn't be compared"]]);
    extra = run.assessment ? h("div", { class: "assessment" }, assessmentNote(run.assessment)) : null;
  } else {
    const t = run.totals;
    const s = t.status_counts;
    sentence = [figure(t.orders), " orders checked: ", figure(s.paid), " paid in full, ", figure(s.partial), " part-paid, ", figure(s.unpaid), " unpaid, and ", figure(s.overpaid), " overpaid."];
    bars = strip([[s.paid, "ok", "paid"], [s.partial, "amber", "part-paid"], [s.unpaid, "bad", "unpaid"], [s.overpaid, "info", "overpaid"]]);
  }
  const held = run.workflow === "reconcile" ? run.totals.rejected_rows : null;
  const box = h("section", { class: "verdict", "aria-label": "Summary" },
    h("p", { class: "sentence" }, sentence),
    bars,
    extra,
    h("div", { class: "verdict-meta" }, verifyBox,
      held ? h("span", null, `${plural(held, "source row")} held back`) : null,
      h("details", { class: "more", style: "margin:0" }, h("summary", null, "What was checked"), h("div", { class: "verify-list" }, state.verify ? verifyList(state.verify) : h("p", { class: "small muted" }, "Checking…")))));
  if (run.workflow !== "reconcile") return box;
  const t = run.totals;
  const cur = run.currency;
  const line = (label, value, strong) => h("tr", { class: strong ? "strong" : null }, h("td", null, label), h("td", { class: "amount" }, `${value} ${cur}`));
  return h("div", { class: "two" }, box,
    h("section", { class: "panel" }, h("h2", null, "Money"),
      h("table", { class: "totals" }, h("tbody", null,
        line("Value of orders checked", t.order_value),
        line("Payments received", t.payments_received),
        line("Refunds given", t.refunds),
        line("Adjustments", t.adjustments),
        line("Net received", t.net_received, true),
        line("Still owed on orders", t.outstanding_balance, true),
        line("Paid more than owed", t.overpayments))),
      h("p", { class: "small muted", style: "margin-top:10px" }, "Exact to the cent, straight from the run record.")));
}

function issuesFile(run) {
  return (run.outputs.find((o) => /(issues|rejected_rows)\.csv$/.test(o.file)) || {}).file;
}

function attention(run) {
  const groups = run.attention.filter((g) => !["unmapped_columns"].includes(g.code));
  if (!groups.length) return h("section", { class: "section" }, note("ok", h("strong", null, "Nothing needs your attention. "), "Every row was used."));
  const chip = (outcome) => {
    const kind = { Rejected: "bad", "Kept with warning": "warn", "Removed duplicate": "quiet", "Excluded by profile": "quiet", Note: "info" }[outcome] || "quiet";
    const label = { Rejected: "Held back", "Kept with warning": "Kept, check it", "Removed duplicate": "Removed copy", "Excluded by profile": "Left out", Note: "Note" }[outcome] || outcome;
    return h("span", { class: `chip ${kind}` }, label);
  };
  return h("section", { class: "section" },
    h("div", { class: "section-head" }, h("h2", null, "Needs your attention"), h("p", { class: "small muted" }, "Grouped by reason. Fix these in the original export and run again.")),
    h("div", { class: "panel" }, h("ul", { class: "attention" }, groups.map((group) =>
      h("li", null,
        h("span", { class: "rows" }, Number(group.rows).toLocaleString()),
        h("div", null, chip(group.outcome), " ", h("strong", null, group.what || group.code.replace(/_/g, " ")),
          group.fix ? h("div", { class: "small muted" }, group.fix) : null),
        h("button", { class: "button small", type: "button", onclick: () => showIssueRows(group.code) }, "Show rows"))))));
}

function showIssueRows(code) {
  const run = state.run;
  const tab = resultTabs(run).find((t) => t.file === issuesFile(run));
  state.tab = tab.key;
  state.filter = { column: "code", value: code };
  state.page = 0;
  const area = document.querySelector(".data-area");
  if (area) {
    const replacement = dataTabs(run, resultTabs(run));
    area.replaceWith(replacement);
    replacement.scrollIntoView({ behavior: "smooth", block: "start" });
  }
}

function resultTabs(run) {
  const rows = run.artifact_rows;
  if (run.workflow === "clean") {
    return [
      { key: "clean", label: "Clean data", file: "cleaned_data.csv", count: rows["cleaned_data.csv"] },
      { key: "issues", label: "Held back and removed", file: "cleaning_issues.csv", count: rows["cleaning_issues.csv"] },
    ];
  }
  if (run.workflow === "compare") {
    return [
      { key: "changed", label: "Changed values", file: "modified_values.csv", count: rows["modified_values.csv"] },
      { key: "added", label: "Added", file: "added_records.csv", count: rows["added_records.csv"] },
      { key: "removed", label: "Removed", file: "removed_records.csv", count: rows["removed_records.csv"] },
      { key: "issues", label: "Couldn't be compared", file: "comparison_issues.csv", count: rows["comparison_issues.csv"] },
    ];
  }
  const t = run.totals;
  return [
    { key: "exceptions", label: "Orders to follow up", file: "reconciliation_ledger.csv", count: t.orders - t.status_counts.paid, exclude: { column: "financial_status", value: "paid" } },
    { key: "ledger", label: "All orders", file: "reconciliation_ledger.csv", count: rows["reconciliation_ledger.csv"] },
    { key: "issues", label: "Held back rows", file: "rejected_rows.csv", count: rows["rejected_rows.csv"] },
  ];
}

function dataTabs(run, tabs) {
  const area = h("div", { class: "data-area" });
  const current = tabs.find((t) => t.key === state.tab) || tabs[0];
  const bar = h("div", { class: "tabs", role: "tablist" }, tabs.map((tab) =>
    h("button", { type: "button", role: "tab", "aria-selected": tab.key === current.key ? "true" : "false", onclick: () => {
      state.tab = tab.key; state.filter = null; state.page = 0;
      area.replaceWith(dataTabs(run, tabs));
    } }, tab.label, h("span", { class: "count" }, `(${Number(tab.count).toLocaleString()})`))));
  const body = h("div", { role: "tabpanel" }, h("p", { class: "muted small" }, "Loading…"));
  put(area, bar, body);
  loadTable(run, current, body);
  return area;
}

async function loadTable(run, tab, body) {
  const limit = 100;
  const params = new URLSearchParams({ offset: String(state.page * limit), limit: String(limit) });
  const filter = state.filter || tab.exclude;
  if (filter) {
    params.set("column", filter.column);
    params.set("value", filter.value);
    if (!state.filter && tab.exclude) params.set("exclude", "true");
  }
  let data;
  try {
    data = await api("GET", `/sessions/${state.view.session.id}/runs/${run.run}/table/${tab.file}?${params}`);
  } catch (error) {
    put(clear(body), note("bad", error.message));
    return;
  }
  const pages = Math.max(1, Math.ceil(data.total / limit));
  const numeric = numericColumns(data.header, data.rows);
  const filterNote = state.filter
    ? note("info", `Showing rows with reason “${state.filter.value.replace(/_/g, " ")}”. `, h("button", { class: "button link small", type: "button", onclick: () => { state.filter = null; state.page = 0; loadTable(run, tab, body); } }, "Show all"))
    : null;
  if (!data.total) {
    put(clear(body), filterNote, h("p", { class: "empty" }, tab.key === "issues" ? "No rows here: nothing was held back." : "No rows here."));
    return;
  }
  put(clear(body),
    filterNote,
    h("div", { class: "scroll tall" },
      h("table", { class: "data" },
        h("thead", null, h("tr", null, data.header.map((name, index) => h("th", { scope: "col", class: numeric[index] ? "num" : null }, name)))),
        h("tbody", null, data.rows.map((cells) => h("tr", null, cells.map((cell, index) => h("td", { class: numeric[index] ? "num" : null, title: cell.length > 40 ? cell : null }, cell))))))),
    h("div", { class: "pager" },
      h("span", null, `Rows ${data.offset + 1}–${data.offset + data.rows.length} of ${data.total.toLocaleString()}`),
      h("button", { class: "button small", type: "button", disabled: state.page === 0 || null, onclick: () => { state.page -= 1; loadTable(run, tab, body); } }, "Previous"),
      h("button", { class: "button small", type: "button", disabled: state.page + 1 >= pages || null, onclick: () => { state.page += 1; loadTable(run, tab, body); } }, "Next")));
}

function downloads(run) {
  const base = `/api/sessions/${state.view.session.id}/runs/${run.run}`;
  return h("section", { class: "panel" },
    h("h2", null, "Download"),
    h("p", { class: "small muted" }, "These are the exact files the run wrote and verified. Spreadsheet formulas in your data are neutralized so they can't run when opened."),
    h("div", { class: "actions", style: "margin-top:14px" },
      h("a", { class: "button primary", href: `${base}/download.zip`, download: "" }, "Download everything (.zip)"),
      run.profile_saved ? h("a", { class: "button", href: `${base}/download/profile.toml`, download: "" }, "Settings used (.toml)") : null),
    h("ul", { class: "downloads", style: "margin-top:16px" }, [...run.outputs].sort((a, b) => rank(a.file) - rank(b.file)).map((item) =>
      h("li", null,
        h("div", null, h("div", { class: "file-name" }, item.file), h("div", { class: "small muted" }, item.description, ` ${bytes(item.bytes)}.`)),
        h("a", { class: "button small", href: `${base}/download/${item.file}`, download: "" }, "Download")))));
}

function rank(file) {
  if (file.endsWith(".xlsx")) return 0;
  if (file === "manifest.json") return 3;
  return /issues|rejected/.test(file) ? 2 : 1;
}

function history(run) {
  const runs = state.view.runs.filter((r) => !r.unreadable);
  return h("section", { class: "panel" },
    h("h2", null, "Runs"),
    h("p", { class: "small muted" }, "Each run is kept separately. Running again never overwrites an earlier result."),
    h("ul", { class: "downloads", style: "margin-top:12px" }, runs.map((item) =>
      h("li", null,
        h("div", null, h("div", { class: "file-name" }, when(item.finished_at)), h("div", { class: "small muted" }, item.status === "completed" ? "Every row used" : "Some rows held back")),
        item.run === run.run ? h("span", { class: "chip quiet" }, "Showing") : h("a", { class: "button small", href: `#${sessionPath("results", item.run)}` }, "View")))),
    h("div", { class: "actions" },
      h("a", { class: "button", href: `#${sessionPath("columns")}` }, "Change settings"),
      h("a", { class: "button", href: `#${sessionPath("files")}` }, "Use new files")));
}

function technical(run) {
  const folder = `${state.status.data_dir}/sessions/${state.view.session.id}/runs/${run.run}`;
  return h("details", { class: "more section" }, h("summary", null, "Technical details for your records"),
    h("div", { class: "panel" },
      h("table", { class: "totals" }, h("tbody", null,
        [["Run", run.run], ["Folder on this computer", folder], ["Settings", `${run.profile.name} ${run.profile.version}`], ["Settings checksum (SHA-256)", run.profile.sha256],
          ["Result fingerprint", run.fingerprint], ["Ordixa version", `${run.code_version.ordixa}${run.code_version.git_commit ? ` (${run.code_version.git_commit.slice(0, 12)})` : ""}`]]
          .map(([k, v]) => h("tr", null, h("td", null, k), h("td", { class: "mono" }, v))),
        run.inputs.map((item) => h("tr", null, h("td", null, `Input ${item.file} (${plural(item.data_rows, "row")})`), h("td", { class: "mono" }, item.sha256))))),
      h("p", { class: "small muted", style: "margin-top:12px" }, "The same check can be run from a terminal with python -m app.ordixa verify --run followed by the folder above.")));
}

/* redraw the current screen without losing the place */

function rerender() {
  const y = window.scrollY;
  route().then(() => window.scrollTo(0, y));
}
