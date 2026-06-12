/* sde-deepagent mission control — vanilla JS SPA over the REST + SSE API */

const $ = (sel, el = document) => el.querySelector(sel);
const main = $("#main");
const KNOWN_AGENTS = ["orchestrator", "explorer", "coder", "tester", "reviewer"];

/* ---------- api ---------- */

let AUTH = sessionStorage.getItem("auth_token") || "";

function authHeaders(extra = {}) {
  return AUTH ? { ...extra, Authorization: `Bearer ${AUTH}` } : extra;
}

/* Append the token to SSE URLs — EventSource can't set headers. */
function withToken(url) {
  if (!AUTH) return url;
  return url + (url.includes("?") ? "&" : "?") + "token=" + encodeURIComponent(AUTH);
}

function promptForToken() {
  const t = window.prompt("API token (auth is enabled on this server):");
  if (t) { AUTH = t.trim(); sessionStorage.setItem("auth_token", AUTH); }
  return !!t;
}

async function api(path, opts = {}, _retried = false) {
  const res = await fetch(path, {
    ...opts,
    headers: authHeaders({ "Content-Type": "application/json", ...(opts.headers || {}) }),
  });
  if (res.status === 401 && !_retried && promptForToken()) {
    return api(path, opts, true);
  }
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch {}
    throw new Error(detail);
  }
  return res.json();
}

function toast(msg, cls = "") {
  const el = document.createElement("div");
  el.className = `toast ${cls}`;
  el.textContent = msg;
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), 4500);
}

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function ago(ts) {
  if (!ts) return "–";
  const s = Math.max(0, (Date.now() / 1000) - ts);
  if (s < 60) return `${s | 0}s ago`;
  if (s < 3600) return `${(s / 60) | 0}m ago`;
  if (s < 86400) return `${(s / 3600) | 0}h ago`;
  return new Date(ts * 1000).toLocaleString();
}

function clock(ts) {
  return new Date(ts * 1000).toLocaleTimeString([], { hour12: false });
}

const STATUS_COLOR = {
  queued: "var(--cyan)", running: "var(--amber)", completed: "var(--green)",
  failed: "var(--red)", cancelled: "var(--ink-faint)",
  awaiting_approval: "var(--violet)",
};

/* Build <optgroup> dropdown options from /api/models.
   `selected` is kept even if it's not in the curated list (custom yaml entries). */
function modelOptions(catalog, selected, emptyLabel) {
  let html = emptyLabel != null
    ? `<option value="" ${!selected ? "selected" : ""}>${esc(emptyLabel)}</option>` : "";
  let found = !selected;
  for (const [provider, info] of Object.entries(catalog)) {
    const label = provider + (info.configured ? "" : " (no API key)");
    html += `<optgroup label="${esc(label)}">`;
    for (const m of info.models) {
      if (m === selected) found = true;
      html += `<option value="${esc(m)}" ${m === selected ? "selected" : ""}>${esc(m.split(":")[1])}</option>`;
    }
    html += `</optgroup>`;
  }
  if (!found && selected)
    html += `<optgroup label="custom"><option value="${esc(selected)}" selected>${esc(selected)}</option></optgroup>`;
  return html;
}

/* ---------- global SSE: stats + live list refresh ---------- */

let globalES = null;
function connectGlobal() {
  globalES = new EventSource(withToken("/api/stream"));
  globalES.onopen = () => $("#conn").classList.add("on");
  globalES.onerror = () => $("#conn").classList.remove("on");
  globalES.onmessage = (e) => {
    const ev = JSON.parse(e.data);
    if (ev.kind === "status") {
      refreshStats();
      if (location.hash === "" || location.hash === "#/" ) renderTasks(false);
    }
  };
}

async function refreshStats() {
  try {
    const s = await api("/api/stats");
    for (const k of ["queued", "running", "completed", "failed"])
      $(`#st-${k}`).textContent = s[k] ?? 0;
    const spend = $("#st-spend");
    let text = `$${(s.spend_today_usd ?? 0).toFixed(2)}`;
    if (s.daily_budget_usd > 0) text += ` / $${s.daily_budget_usd.toFixed(0)}`;
    spend.textContent = text + (s.budget_paused ? " ⏸" : "");
    spend.style.color = s.budget_paused ? "var(--red)"
      : (s.daily_budget_usd > 0 && s.spend_today_usd >= 0.8 * s.daily_budget_usd)
        ? "var(--amber)" : "";
  } catch {}
}

async function refreshHealth() {
  try {
    const h = await api("/api/health");
    const el = $("#st-memory");
    el.textContent = h.memory ? "on" : "off";
    el.style.color = h.memory ? "var(--green)" : "var(--ink-faint)";
  } catch {}
}

/* ---------- router ---------- */

let taskES = null;
function teardown() {
  if (taskES) { taskES.close(); taskES = null; }
}

window.addEventListener("hashchange", route);

function setNav(name) {
  document.querySelectorAll(".nav a").forEach((a) =>
    a.classList.toggle("active", a.dataset.route === name));
}

function route() {
  teardown();
  const h = location.hash.replace(/^#\/?/, "");
  if (h.startsWith("task/")) return renderTaskDetail(h.slice(5));
  if (h === "new") { setNav("new"); return renderNewTask(); }
  if (h === "repos") { setNav("repos"); return renderRepos(); }
  if (h === "agents") { setNav("agents"); return renderAgents(); }
  if (h === "chat") { setNav("chat"); return renderChat(); }
  if (h === "resources") { setNav("resources"); return renderResources(); }
  setNav("tasks");
  renderTasks(true);
}

/* ---------- view: task list ---------- */

async function renderTasks(full) {
  let tasks;
  try { tasks = await api("/api/tasks"); } catch (e) { return toast(e.message, "err"); }
  if (location.hash !== "" && location.hash !== "#/") return; // user navigated away
  const rows = tasks.map((t, i) => `
    <div class="task-row" style="--status-color:${STATUS_COLOR[t.status]}; animation-delay:${Math.min(i * 30, 300)}ms"
         onclick="location.hash='#/task/${t.id}'">
      <span class="task-id">${t.id}</span>
      <span class="task-title">${esc(t.title)}</span>
      <div class="task-right">
        <span class="badge ${t.status}">${t.status}</span>
        <span class="task-time">${ago(t.created_at)}</span>
      </div>
      <div class="task-meta">
        ${t.repo ? `<span class="chip repo">${esc(t.repo)}</span>` : ""}
        <span class="chip src">${esc(t.source)}</span>
        ${t.branch ? `<span class="chip">${esc(t.branch)}</span>` : ""}
        ${t.pr_url ? `<a href="${esc(t.pr_url)}" target="_blank" onclick="event.stopPropagation()">PR ↗</a>` : ""}
      </div>
    </div>`).join("");
  main.innerHTML = `
    <div class="view-head">
      <h1 class="view-title">Tasks</h1>
      <span class="view-sub">${tasks.length} total</span>
      <span class="spacer"></span>
      <button class="btn sm" onclick="location.hash='#/new'">+ New task</button>
    </div>
    ${tasks.length ? `<div class="task-rows">${rows}</div>` : `
      <div class="empty">
        <div class="big">NO TASKS YET</div>
        Create one here, or send a message via Telegram / Slack / Linear.
      </div>`}`;
}

/* ---------- view: task detail ---------- */

const AGENT_CLASS = (a) => KNOWN_AGENTS.includes(a) ? a : "other";

function renderEvent(ev) {
  const c = ev.content || {};
  let body = "";
  if (ev.kind === "message") body = `<div class="ev-text">${esc(c.text)}</div>`;
  else if (ev.kind === "log") body = `<div class="ev-text">${esc(c.text)}</div>`;
  else if (ev.kind === "status")
    body = `<div class="ev-text" style="color:${STATUS_COLOR[c.status] || "var(--ink)"}">▶ ${esc(c.status)}${c.error ? ` — ${esc(c.error)}` : ""}${c.usage?.cost_usd != null ? ` · $${c.usage.cost_usd.toFixed(4)} (${((c.usage.input_tokens + c.usage.output_tokens) / 1000).toFixed(0)}k tok)` : ""}${c.pr_url ? ` · <a href="${esc(c.pr_url)}" target="_blank">PR ↗</a>` : ""}</div>`
      + (c.summary ? `<details class="tool-out"><summary>final summary</summary><pre>${esc(c.summary)}</pre></details>` : "");
  else if (ev.kind === "tool_call")
    body = `<div class="tool-call">$ <span class="tn">${esc(c.name)}</span>(<span class="ta">${esc(JSON.stringify(c.args)).slice(1, 400)}</span>)</div>`;
  else if (ev.kind === "tool_result")
    body = `<details class="tool-out"><summary>↳ ${esc(c.name || "result")}${c.truncated ? " (truncated)" : ""}</summary><pre>${esc(c.output)}</pre></details>`;
  else if (ev.kind === "pr_opened")
    body = `<div class="ev-text">⎇ PR opened: <a href="${esc(c.url)}" target="_blank">${esc(c.url)}</a></div>`;
  else if (ev.kind === "approval_request")
    body = `<div class="ev-text" style="color:var(--violet)">⏸ approval requested: ${esc(c.title || "")}</div>`
      + (c.diff_stat ? `<details class="tool-out"><summary>diff stat</summary><pre>${esc(c.diff_stat)}</pre></details>` : "");
  else if (ev.kind === "todos") return ""; // rendered in the todos panel
  else body = `<div class="ev-text">${esc(JSON.stringify(c))}</div>`;
  return `
    <div class="ev kind-${ev.kind}" data-evid="${ev.id}">
      <div class="ev-meta">
        <span class="ev-agent ${AGENT_CLASS(ev.agent)}">${esc(ev.agent)}</span>
        ${clock(ev.ts)}
      </div>
      <div class="ev-body">${body}</div>
    </div>`;
}

function renderTodos(todos) {
  const box = $("#todos");
  if (!box) return;
  if (!todos || !todos.length) { box.style.display = "none"; return; }
  box.style.display = "";
  $("#todo-list").innerHTML = todos.map((t) => {
    const status = t.status || "pending";
    const tick = status === "completed" ? "[x]" : status === "in_progress" ? "[~]" : "[ ]";
    return `<div class="todo ${status}"><span class="tick">${tick}</span><span>${esc(t.content || t.title || "")}</span></div>`;
  }).join("");
}

async function renderTaskDetail(id) {
  setNav("tasks");
  let t;
  try { t = await api(`/api/tasks/${id}`); } catch (e) { return toast(e.message, "err"); }
  main.innerHTML = `
    <div class="view-head">
      <h1 class="view-title">Task ${esc(t.id)}</h1>
      <span class="spacer"></span>
      <button class="btn ghost sm" onclick="location.hash='#/'">← back</button>
      ${t.status === "completed" && t.branch
        ? `<button class="btn ghost sm" id="btn-revise">✎ revise</button>` : ""}
      ${["queued", "running", "awaiting_approval"].includes(t.status)
        ? `<button class="btn danger sm" id="btn-cancel">✕ cancel</button>` : ""}
    </div>
    <div id="approval-zone"></div>
    <div id="revise-zone"></div>
    <div class="detail-head" id="detail-head" style="--status-color:${STATUS_COLOR[t.status]}">
      <div class="detail-title">${esc(t.title)} <span class="badge ${t.status}" id="d-badge">${t.status}</span></div>
      <div class="detail-grid">
        <span>repo <b id="d-repo">${esc(t.repo || "auto")}</b></span>
        ${t.parent_id ? `<span>revises <b><a href="#/task/${esc(t.parent_id)}">${esc(t.parent_id)}</a></b></span>` : ""}
        <span>source <b>${esc(t.source)}</b></span>
        <span>branch <b id="d-branch">${esc(t.branch || "–")}</b></span>
        <span>created <b>${ago(t.created_at)}</b></span>
        ${t.model ? `<span>model <b>${esc(t.model)}</b></span>` : ""}
        ${t.budget_usd ? `<span>budget <b>$${t.budget_usd.toFixed(2)}</b></span>` : ""}
        <span>cost <b id="d-cost">${t.cost_usd != null ? `$${t.cost_usd.toFixed(4)}` : "–"}</b></span>
      </div>
      <div class="detail-desc">${esc(t.description)}</div>
      <div id="d-pr">${t.pr_url ? `<a class="pr-link" href="${esc(t.pr_url)}" target="_blank">⎇ View pull request ↗</a>` : ""}</div>
      <div id="d-err">${t.error ? `<div class="error-box">${esc(t.error)}</div>` : ""}</div>
    </div>
    <div class="todos" id="todos" style="display:none">
      <div class="todos-title">└─ plan</div>
      <div id="todo-list"></div>
    </div>
    ${t.status === "running" ? `
    <form class="steer-bar" id="steer-form">
      <input id="steer-input" placeholder="Steer the running agent — e.g. 'also handle the empty-list case'" autocomplete="off">
      <button class="btn sm" type="submit">⤳ send</button>
    </form>` : ""}
    <div class="view-sub" style="margin-bottom:10px">agent trace ${t.status === "running" ? "· live" : ""}</div>
    <div class="timeline" id="timeline"></div>`;

  const cancelBtn = $("#btn-cancel");
  if (cancelBtn) cancelBtn.onclick = async () => {
    try { await api(`/api/tasks/${id}/cancel`, { method: "POST" }); toast("cancel requested", "ok"); }
    catch (e) { toast(e.message, "err"); }
  };

  const steerForm = $("#steer-form");
  if (steerForm) steerForm.onsubmit = async (e) => {
    e.preventDefault();
    const inp = $("#steer-input"), msg = inp.value.trim();
    if (!msg) return;
    inp.value = "";
    try {
      await api(`/api/tasks/${id}/steer`, { method: "POST",
        body: JSON.stringify({ message: msg }) });
      toast("steer sent — agent receives it at its next check", "ok");
    } catch (e) { toast(e.message, "err"); }
  };

  const reviseBtn = $("#btn-revise");
  if (reviseBtn) reviseBtn.onclick = () => {
    $("#revise-zone").innerHTML = `
      <div class="panel" style="max-width:none;margin-bottom:18px">
        <h3>revise this task (updates the same branch/PR)</h3>
        <div class="field"><label>What should change?</label>
          <textarea id="rv-desc" placeholder="Review feedback: rename the helper, handle the empty-list case, add a test for…"></textarea></div>
        <button class="btn" id="rv-go">▶ Queue revision</button>
      </div>`;
    $("#rv-go").onclick = async () => {
      const desc = $("#rv-desc").value.trim();
      if (!desc) return toast("describe the revision first", "err");
      try {
        const rt = await api("/api/tasks", { method: "POST", body: JSON.stringify({
          title: `Revise: ${t.title}`.slice(0, 200), description: desc,
          parent_id: t.id }) });
        toast(`revision ${rt.id} queued`, "ok");
        location.hash = `#/task/${rt.id}`;
      } catch (e) { toast(e.message, "err"); }
    };
  };

  const renderApprovalPanel = (proposal) => {
    $("#approval-zone").innerHTML = `
      <div class="approval-panel">
        <h4>⏸ awaiting your approval — nothing has been pushed</h4>
        <div><b>${esc(proposal.title || t.title)}</b></div>
        ${proposal.diff_stat ? `<pre>${esc(proposal.diff_stat)}</pre>` : ""}
        ${proposal.summary ? `<details class="tool-out"><summary>agent summary</summary><pre>${esc(proposal.summary)}</pre></details>` : ""}
        <div class="approval-actions">
          <button class="btn" id="ap-yes">✓ Approve &amp; ship</button>
          <button class="btn danger" id="ap-no">✕ Reject</button>
        </div>
      </div>`;
    $("#ap-yes").onclick = async () => {
      $("#ap-yes").disabled = true;
      try {
        const r = await api(`/api/tasks/${id}/approve`, { method: "POST" });
        toast(r.pr_url ? `shipped — ${r.pr_url}` : "shipped (branch pushed)", "ok");
        renderTaskDetail(id);
      } catch (e) { toast(e.message, "err"); $("#ap-yes").disabled = false; }
    };
    $("#ap-no").onclick = async () => {
      try { await api(`/api/tasks/${id}/reject`, { method: "POST" }); renderTaskDetail(id); }
      catch (e) { toast(e.message, "err"); }
    };
  };
  if (t.status === "awaiting_approval") {
    try {
      const evs = await api(`/api/tasks/${id}/events`);
      const prop = evs.reverse().find((e) => e.kind === "approval_request");
      renderApprovalPanel(prop ? prop.content : {});
    } catch { renderApprovalPanel({}); }
  }

  const timeline = $("#timeline");
  let lastId = 0;
  const append = (ev, flash) => {
    if (ev.id <= lastId) return;
    lastId = ev.id;
    if (ev.kind === "todos") return renderTodos(ev.content.todos);
    const html = renderEvent(ev);
    if (!html) return;
    timeline.insertAdjacentHTML("beforeend", html);
    if (flash) {
      const node = timeline.lastElementChild;
      node.classList.add("flash");
      node.scrollIntoView({ block: "nearest", behavior: "smooth" });
    }
    if (ev.kind === "status") updateHead(ev.content);
  };
  const updateHead = (c) => {
    if (c.usage?.cost_usd != null && $("#d-cost"))
      $("#d-cost").textContent = `$${c.usage.cost_usd.toFixed(4)}`;
    if (!c.status) return;
    const badge = $("#d-badge");
    if (badge) { badge.className = `badge ${c.status}`; badge.textContent = c.status; }
    $("#detail-head").style.setProperty("--status-color", STATUS_COLOR[c.status]);
    if (c.pr_url) $("#d-pr").innerHTML =
      `<a class="pr-link" href="${esc(c.pr_url)}" target="_blank">⎇ View pull request ↗</a>`;
    if (c.error) $("#d-err").innerHTML = `<div class="error-box">${esc(c.error)}</div>`;
    if (["completed", "failed", "cancelled"].includes(c.status) && cancelBtn) cancelBtn.remove();
  };

  // replay history, then go live over SSE
  try { (await api(`/api/tasks/${id}/events`)).forEach((ev) => append(ev, false)); }
  catch (e) { toast(e.message, "err"); }
  timeline.lastElementChild?.scrollIntoView({ block: "nearest" });

  taskES = new EventSource(withToken(`/api/tasks/${id}/stream?after=${lastId}`));
  taskES.onmessage = (e) => append(JSON.parse(e.data), true);
}

/* ---------- view: new task ---------- */

async function renderNewTask() {
  let repos = {}, catalog = {};
  try { [repos, catalog] = await Promise.all([api("/api/repos"), api("/api/models")]); }
  catch {}
  const names = Object.keys(repos);
  main.innerHTML = `
    <div class="view-head"><h1 class="view-title">New task</h1></div>
    <div class="panel">
      <h3>dispatch work to the agent</h3>
      <div class="field">
        <label>Title</label>
        <input id="f-title" placeholder="Fix flaky login test" autofocus>
      </div>
      <div class="field">
        <label>Description</label>
        <textarea id="f-desc" placeholder="Full task details: what to change, acceptance criteria, links…"></textarea>
      </div>
      <div class="field-row">
        <div class="field">
          <label>Codebase</label>
          <select id="f-repo">
            <option value="">auto-detect from task</option>
            ${names.map((n) => `<option>${esc(n)}</option>`).join("")}
          </select>
          ${names.length ? "" : `<div class="hint">no codebases registered yet → <a href="#/repos">add one</a></div>`}
        </div>
        <div class="field">
          <label>Model override <span style="text-transform:none">(optional)</span></label>
          <select id="f-model">${modelOptions(catalog, null, "default from agent config")}</select>
        </div>
      </div>
      <div class="field" style="max-width:240px">
        <label>Budget USD <span style="text-transform:none">(optional)</span></label>
        <input id="f-budget" type="number" min="0" step="0.5" placeholder="no cap">
        <div class="hint">run aborts if LLM spend exceeds this</div>
      </div>
      <button class="btn" id="f-submit">▶ Queue task</button>
    </div>`;
  $("#f-submit").onclick = async () => {
    const title = $("#f-title").value.trim();
    if (!title) return toast("title required", "err");
    $("#f-submit").disabled = true;
    try {
      const t = await api("/api/tasks", {
        method: "POST",
        body: JSON.stringify({
          title,
          description: $("#f-desc").value.trim() || title,
          repo: $("#f-repo").value || null,
          model: $("#f-model").value || null,
          budget_usd: parseFloat($("#f-budget").value) || null,
        }),
      });
      toast(`task ${t.id} queued`, "ok");
      location.hash = `#/task/${t.id}`;
    } catch (e) { toast(e.message, "err"); $("#f-submit").disabled = false; }
  };
}

/* ---------- view: repos ---------- */

async function renderRepos() {
  let repos = {};
  try { repos = await api("/api/repos"); } catch (e) { return toast(e.message, "err"); }
  const cards = Object.entries(repos).map(([name, r], i) => `
    <div class="card" style="animation-delay:${i * 40}ms">
      <h4>${esc(name)} <button class="btn danger sm" data-del="${esc(name)}">del</button></h4>
      <div class="url">${esc(r.url)}</div>
      ${r.description ? `<div class="desc">${esc(r.description)}</div>` : ""}
      <div class="kv"><b>branch</b> ${esc(r.default_branch)}</div>
      ${r.setup ? `<div class="kv"><b>setup</b> ${esc(r.setup)}</div>` : ""}
      ${r.test ? `<div class="kv"><b>test</b> ${esc(r.test)}</div>` : ""}
      ${(r.context || []).length ? `<div class="kv"><b>docs</b> ${esc(r.context.join(", "))}</div>` : ""}
      ${r.sandbox || r.approval ? `<div style="margin-top:8px;display:flex;gap:6px;flex-wrap:wrap">
        ${r.sandbox ? `<span class="chip" style="color:var(--green);border-color:rgba(43,217,124,.4)">⊟ sandboxed${r.sandbox_network ? `:${esc(r.sandbox_network)}` : ""}</span>` : ""}
        ${r.approval === "required" ? `<span class="chip" style="color:var(--violet);border-color:rgba(180,140,255,.4)">⏸ approval</span>` : ""}
        ${r.approval === "auto" ? `<span class="chip">auto-ship</span>` : ""}
      </div>` : ""}
    </div>`).join("");
  main.innerHTML = `
    <div class="view-head">
      <h1 class="view-title">Codebases</h1>
      <span class="view-sub">repositories the agent can work on</span>
    </div>
    ${cards ? `<div class="card-grid">${cards}</div>`
            : `<div class="empty" style="margin-bottom:22px"><div class="big">NO CODEBASES</div>Register the repos your agents should work on.</div>`}
    <div class="panel">
      <h3>register codebase</h3>
      <div class="field-row">
        <div class="field"><label>Name</label><input id="r-name" placeholder="backend"></div>
        <div class="field"><label>Default branch</label><input id="r-branch" value="main"></div>
      </div>
      <div class="field"><label>Git URL or local path</label>
        <input id="r-url" placeholder="git@github.com:acme/backend.git">
        <div class="hint">https://, git@, or an absolute local path — PRs need a GitHub-style remote + GITHUB_TOKEN</div>
      </div>
      <div class="field"><label>Description <span style="text-transform:none">(helps task → repo routing)</span></label>
        <input id="r-desc" placeholder="Python FastAPI monolith serving the public API"></div>
      <div class="field-row">
        <div class="field"><label>Setup command</label><input id="r-setup" placeholder="uv sync"></div>
        <div class="field"><label>Test command</label><input id="r-test" placeholder="uv run pytest -x -q"></div>
      </div>
      <div class="field"><label>Context docs <span style="text-transform:none">(comma-separated globs)</span></label>
        <input id="r-ctx" placeholder="docs/architecture.md, CONTRIBUTING.md"></div>
      <div class="field-row">
        <div class="field">
          <label>Sandbox <span style="text-transform:none">(run tasks in a container)</span></label>
          <select id="r-sandbox">
            <option value="">server default</option>
            <option value="true">on — isolate shell + egress</option>
            <option value="false">off — run on host</option>
          </select>
        </div>
        <div class="field">
          <label>Sandbox network</label>
          <select id="r-network">
            <option value="">default (none)</option>
            <option value="none">none — no egress</option>
            <option value="bridge">bridge — allow egress</option>
          </select>
        </div>
      </div>
      <div class="field">
        <label>Approval policy</label>
        <select id="r-approval">
          <option value="">server default</option>
          <option value="auto">auto-ship (open PR directly)</option>
          <option value="required">require human approval</option>
        </select>
      </div>
      <button class="btn" id="r-submit">＋ Save codebase</button>
    </div>`;
  document.querySelectorAll("[data-del]").forEach((b) => b.onclick = async () => {
    try { await api(`/api/repos/${b.dataset.del}`, { method: "DELETE" }); renderRepos(); }
    catch (e) { toast(e.message, "err"); }
  });
  $("#r-submit").onclick = async () => {
    try {
      await api("/api/repos", {
        method: "POST",
        body: JSON.stringify({
          name: $("#r-name").value.trim(),
          url: $("#r-url").value.trim(),
          default_branch: $("#r-branch").value.trim() || "main",
          description: $("#r-desc").value.trim(),
          setup: $("#r-setup").value.trim() || null,
          test: $("#r-test").value.trim() || null,
          context: $("#r-ctx").value.split(",").map((s) => s.trim()).filter(Boolean),
          sandbox: { "true": true, "false": false }[$("#r-sandbox").value] ?? null,
          sandbox_network: $("#r-network").value || null,
          approval: $("#r-approval").value || null,
        }),
      });
      toast("codebase saved", "ok");
      renderRepos();
    } catch (e) { toast(e.message, "err"); }
  };
}

/* ---------- view: agents ---------- */

async function renderAgents() {
  let cfg, catalog = {};
  try {
    [cfg, catalog] = await Promise.all([api("/api/config/agents"), api("/api/models")]);
  } catch (e) { return toast(e.message, "err"); }
  const subs = cfg.subagents || {};
  const row = (role, spec, isOrch) => `
    <div class="agent-card">
      <div class="role" style="color:${isOrch ? "var(--green)" : "var(--ink)"}">${esc(role)}
        <small>${isOrch ? "coordinates everything" : "subagent"}</small></div>
      <div class="about">${esc((spec.description || (isOrch ? "Plans the task, delegates to subagents, ships the PR." : "")).trim())}</div>
      <div><label style="font-size:10px;color:var(--ink-faint);text-transform:uppercase;letter-spacing:1px">model</label>
        <select data-role="${esc(role)}">${modelOptions(catalog, spec.model || "",
          isOrch ? null : "inherit from orchestrator")}</select>
        <label style="font-size:10px;color:var(--ink-faint);text-transform:uppercase;letter-spacing:1px;margin-top:6px;display:block">reasoning effort</label>
        <select data-effort="${esc(role)}">
          ${["", "low", "medium", "high"].map((e) =>
            `<option value="${e}" ${(spec.effort || "") === e ? "selected" : ""}>${e || "provider default"}</option>`).join("")}
        </select></div>
    </div>`;
  main.innerHTML = `
    <div class="view-head">
      <h1 class="view-title">Agents</h1>
      <span class="view-sub">model per role — applies to the next task</span>
    </div>
    <div class="model-hints">
      formats: <code>anthropic:claude-sonnet-4-6</code> · <code>anthropic:claude-opus-4-8</code> ·
      <code>google_genai:gemini-2.5-pro</code> · <code>google_genai:gemini-2.5-flash</code> ·
      <code>openai:gpt-5.4</code> · <code>openai:o4-mini</code>
    </div>
    ${row("orchestrator", cfg.orchestrator || {}, true)}
    ${Object.entries(subs).map(([n, s]) => row(n, s || {}, false)).join("")}
    <div style="margin-top:18px; display:flex; gap:10px">
      <button class="btn" id="a-save">✓ Save models</button>
      <span class="view-sub" style="align-self:center">MCP servers & prompts: edit <b>config/agents.yaml</b></span>
    </div>`;
  $("#a-save").onclick = async () => {
    document.querySelectorAll("[data-role]").forEach((sel) => {
      const role = sel.dataset.role, v = sel.value;
      if (role === "orchestrator") { if (v) (cfg.orchestrator ||= {}).model = v; }
      else if (cfg.subagents?.[role]) cfg.subagents[role].model = v || null;
    });
    document.querySelectorAll("[data-effort]").forEach((sel) => {
      const role = sel.dataset.effort, v = sel.value || null;
      if (role === "orchestrator") (cfg.orchestrator ||= {}).effort = v;
      else if (cfg.subagents?.[role]) cfg.subagents[role].effort = v;
    });
    try {
      await api("/api/config/agents", { method: "PUT", body: JSON.stringify(cfg) });
      toast("agent config saved", "ok");
    } catch (e) { toast(e.message, "err"); }
  };
}

/* ---------- view: chat ---------- */

const chatState = { sessionId: sessionStorage.getItem("chat_session") || null, msgs: [] };

function renderChatMsgs() {
  const thread = $("#chat-thread");
  if (!thread) return;
  thread.innerHTML = chatState.msgs.map((m) => `
    <div class="chat-msg ${m.role}">
      <span class="chat-who">${m.role === "user" ? "&gt; you" : "▚▞ sde-deepagent"}${m.cost ? ` <i class="chat-cost">$${m.cost.toFixed(4)}</i>` : ""}</span>
      <div class="chat-text">${esc(m.text)}</div>
    </div>`).join("") +
    (chatState.waiting ? `<div class="chat-msg assistant"><span class="chat-who">▚▞ sde-deepagent</span>
      <div class="chat-text thinking">consulting task history<span class="cursor">█</span></div></div>` : "");
  thread.scrollTop = thread.scrollHeight;
}

async function renderChat() {
  main.innerHTML = `
    <div class="view-head">
      <h1 class="view-title">Chat</h1>
      <span class="view-sub">ask about any past or running task</span>
      <span class="spacer"></span>
      <button class="btn ghost sm" id="chat-reset">↺ new conversation</button>
    </div>
    <div class="chat-box">
      <div class="chat-thread" id="chat-thread"></div>
      <form class="chat-form" id="chat-form">
        <input id="chat-input" placeholder='e.g. "what did the agent change in the subtract task?"' autocomplete="off" autofocus>
        <button class="btn" type="submit" id="chat-send">send</button>
      </form>
    </div>`;
  if (!chatState.msgs.length)
    chatState.msgs.push({ role: "assistant", text:
      "Ask me about any task — what was done, why one failed, what it cost, " +
      "what the agents learned about a codebase." });
  renderChatMsgs();

  $("#chat-reset").onclick = async () => {
    if (chatState.sessionId)
      try { await api(`/api/chat/${chatState.sessionId}`, { method: "DELETE" }); } catch {}
    chatState.sessionId = null;
    sessionStorage.removeItem("chat_session");
    chatState.msgs = [];
    renderChat();
  };

  $("#chat-form").onsubmit = async (e) => {
    e.preventDefault();
    const input = $("#chat-input");
    const text = input.value.trim();
    if (!text || chatState.waiting) return;
    input.value = "";
    chatState.msgs.push({ role: "user", text });
    chatState.waiting = true;
    $("#chat-send").disabled = true;
    renderChatMsgs();
    try {
      const res = await api("/api/chat", {
        method: "POST",
        body: JSON.stringify({ message: text, session_id: chatState.sessionId }),
      });
      chatState.sessionId = res.session_id;
      sessionStorage.setItem("chat_session", res.session_id);
      chatState.msgs.push({ role: "assistant", text: res.reply, cost: res.cost_usd });
      refreshStats();
    } catch (err) {
      chatState.msgs.push({ role: "assistant", text: `⚠ ${err.message}` });
    } finally {
      chatState.waiting = false;
      const send = $("#chat-send");
      if (send) send.disabled = false;
      renderChatMsgs();
      $("#chat-input")?.focus();
    }
  };
}

/* ---------- view: resources ---------- */

const RES_STATUS_COLOR = {
  done: "var(--green)", queued: "var(--cyan)", extracting: "var(--amber)",
  processing: "var(--amber)", failed: "var(--red)", unknown: "var(--ink-faint)",
};

async function renderResources() {
  let repos = {};
  try { repos = await api("/api/repos"); } catch {}
  main.innerHTML = `
    <div class="view-head">
      <h1 class="view-title">Resources</h1>
      <span class="view-sub">links &amp; docs ingested into the agents' long-term memory</span>
      <span class="spacer"></span>
      <button class="btn ghost sm" id="res-refresh">↻ refresh</button>
    </div>
    <div class="panel" style="max-width:860px">
      <h3>add to company memory</h3>
      <div class="field">
        <label>URL or text</label>
        <textarea id="res-content" style="min-height:70px"
          placeholder="https://docs.yourcompany.com/architecture — or paste any text: API conventions, runbooks, onboarding notes…"></textarea>
        <div class="hint">URLs are fetched (via Firecrawl when configured, with JS rendering), extracted, and indexed; agents and chat can then recall the content.</div>
      </div>
      <div class="field" style="max-width:280px">
        <label>Scope</label>
        <select id="res-scope">
          <option value="global">global (all codebases)</option>
          ${Object.keys(repos).map((n) => `<option value="${esc(n)}">repo: ${esc(n)}</option>`).join("")}
        </select>
      </div>
      <button class="btn" id="res-submit">＋ Ingest</button>
    </div>
    <div class="view-sub" style="margin:18px 0 8px">ingested resources</div>
    <div id="res-list" class="task-rows"><div class="boot">loading<span class="cursor">█</span></div></div>`;

  async function loadList() {
    const list = $("#res-list");
    try {
      const docs = await api("/api/resources");
      if (!docs.length) {
        list.innerHTML = `<div class="empty"><div class="big">NO RESOURCES YET</div>
          Paste a docs link or some text above to give your agents company context.</div>`;
        return;
      }
      list.innerHTML = docs.map((d, i) => `
        <div class="task-row" style="--status-color:${RES_STATUS_COLOR[d.status] || RES_STATUS_COLOR.unknown}; animation-delay:${Math.min(i * 30, 300)}ms; cursor:default">
          <span class="task-id">${esc(d.kind)}</span>
          <span class="task-title">${esc(d.title || d.summary || d.id)}</span>
          <div class="task-right">
            <span class="badge ${d.status === "done" ? "completed" : d.status === "failed" ? "failed" : "queued"}">${esc(d.status || "?")}</span>
            <button class="btn danger sm" data-del="${esc(d.id)}">del</button>
          </div>
          <div class="task-meta">
            <span class="chip ${d.scope === "global" ? "" : "repo"}">${esc(d.scope)}</span>
            ${d.summary ? `<span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:480px">${esc(d.summary)}</span>` : ""}
          </div>
        </div>`).join("");
      list.querySelectorAll("[data-del]").forEach((b) => b.onclick = async () => {
        try { await api(`/api/resources/${b.dataset.del}`, { method: "DELETE" }); loadList(); }
        catch (e) { toast(e.message, "err"); }
      });
    } catch (e) {
      list.innerHTML = `<div class="empty">${esc(e.message)}</div>`;
    }
  }

  $("#res-refresh").onclick = loadList;
  $("#res-submit").onclick = async () => {
    const content = $("#res-content").value.trim();
    if (!content) return toast("paste a URL or some text first", "err");
    $("#res-submit").disabled = true;
    try {
      await api("/api/resources", {
        method: "POST",
        body: JSON.stringify({ content, scope: $("#res-scope").value }),
      });
      toast("ingesting — indexing may take a moment", "ok");
      $("#res-content").value = "";
      setTimeout(loadList, 800);
    } catch (e) { toast(e.message, "err"); }
    finally { $("#res-submit").disabled = false; }
  };
  loadList();
}

/* ---------- boot ---------- */

connectGlobal();
refreshStats();
refreshHealth();
setInterval(refreshStats, 30000);
route();
