'use strict';
/* forge UI — vanilla SPA with hash routing. Data from the local JSON API in ui/server.py. */

const AUTO_REFRESH_MS = 30000;
const AGENT_REFRESH_MS = 4000;
const AGENT_TAIL_LIMIT = 200;
const DONE_STATES = ['handed-back', 'done'];
// Fallback only; /api/task returns forge_lib.state.STATES as task_states.
const TASK_STATES = ['scoped', 'dispatched', 'in-progress', 'blocked', 'in-review', 'handed-back', 'done', 'coordinating', 'cancelled'];
const SEVERITIES = ['blocker', 'decision', 'question', 'fyi'];
const ISSUE_STATUSES = ['open', 'resolved', 'wontfix'];
const ISSUE_STATES = ['open', 'awaiting-user', 'decided', 'in-progress', 'resolved', 'wontfix'];
const CLOSED_ISSUE_STATES = ['resolved', 'wontfix'];
const issueState = (i) => i.state || i.status || 'open';

const ui = {
  collapsed: new Set(),   // task keys collapsed in the tree
  expanded: new Set(),    // expandable row ids (PR threads)
  drafts: {},             // comment-box drafts by form id
  taskStateMsg: {},       // last task-state apply result by task key: {text, error}
  lastSig: null,
  agentTimer: null,      // live transcript poller on #/agent/<key>
  agentSig: null,
  fsOpen: new Set(),      // expanded folders in the Files tree (by rel path)
  fsRaw: false,           // Files viewer: show raw text instead of rendered md/json
  fsTreeHidden: false,
  fsFile: null,           // file open in the Files view (for mtime polling)
  fsTreeSig: null,
  fsTimer: null,
  token: 0,
};

// ---------- helpers ----------
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function cls(value) {
  return String(value ?? '').toLowerCase().replace(/[^a-z0-9-]/g, '');
}

function isUrl(value) {
  return /^https?:\/\//i.test(String(value || ''));
}

function link(url, text, extra = '') {
  if (!isUrl(url)) return esc(text);
  return `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer" ${extra}>${esc(text)}</a>`;
}

function ago(iso) {
  if (!iso) return '<span class="muted">—</span>';
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return esc(iso);
  const secs = Math.max(0, Math.round((Date.now() - then) / 1000));
  let text;
  if (secs < 60) text = `${secs}s ago`;
  else if (secs < 3600) text = `${Math.round(secs / 60)}m ago`;
  else if (secs < 86400) text = `${Math.round(secs / 3600)}h ago`;
  else text = `${Math.round(secs / 86400)}d ago`;
  return `<time title="${esc(iso)}">${text}</time>`;
}

const taskHref = (key) => `#/task/${encodeURIComponent(key)}`;
const projectHref = (slug) => `#/project/${encodeURIComponent(slug)}`;
const agentHref = (key) => `#/agent/${encodeURIComponent(key)}`;
const agentsHref = (project) => `#/agents${project ? `?project=${encodeURIComponent(project)}` : ''}`;
const issueHref = (key, id) => `#/issue/${encodeURIComponent(key)}/${encodeURIComponent(id)}`;
const shortKey = (key) => String(key).split('/').pop();
const filesHref = (rel) => `#/files/${String(rel || '').split('/').filter(Boolean).map(encodeURIComponent).join('/')}`;
// FORGE_HOME-relative folder of a project/task key: slug/tasks/a/tasks/b.
const keyRel = (key) => String(key).split('/').map((s, i) => (i ? `tasks/${s}` : s)).join('/');
const fileLink = (key, name, text = name) => `<a class="small fs-link" href="${filesHref(`${keyRel(key)}/${name}`)}" title="open in Files">${esc(text)}</a>`;

function badge(state, extra = '') {
  if (!state) return '<span class="muted">—</span>';
  return `<span class="badge st-${cls(state)} ${extra}">${esc(state)}</span>`;
}

function toast(message, isError = false) {
  const el = $('#toast');
  el.textContent = message;
  el.className = `toast${isError ? ' error' : ''}`;
  el.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { el.hidden = true; }, 6000);
}

async function api(path, body) {
  const opts = body === undefined ? {} : {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  };
  const resp = await fetch(path, opts);
  let data = null;
  try { data = await resp.json(); } catch (e) { /* non-JSON error */ }
  if (!resp.ok) throw new Error((data && data.error) || `${resp.status} ${resp.statusText}`);
  return data;
}

function qs(params) {
  const sp = new URLSearchParams();
  Object.entries(params).forEach(([k, v]) => { if (v) sp.set(k, v); });
  const s = sp.toString();
  return s ? `?${s}` : '';
}

// ---------- markdown + mermaid ----------
function mdHtml(text) {
  if (!text) return '<p class="muted">(empty)</p>';
  if (window.marked && window.DOMPurify) {
    return `<div class="md">${window.DOMPurify.sanitize(window.marked.parse(String(text), { gfm: true }))}</div>`;
  }
  return `<pre class="md-fallback">${esc(text)}</pre>`;
}

let mermaidReady = false;
function renderMermaid(root) {
  if (!window.mermaid) return;
  if (!mermaidReady) {
    const dark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
    window.mermaid.initialize({ startOnLoad: false, securityLevel: 'strict', theme: dark ? 'dark' : 'default' });
    mermaidReady = true;
  }
  const nodes = $$('pre > code.language-mermaid', root).map((code) => {
    const div = document.createElement('div');
    div.className = 'mermaid';
    div.textContent = code.textContent;
    code.parentElement.replaceWith(div);
    return div;
  });
  if (nodes.length) {
    window.mermaid.run({ nodes }).catch((err) => console.warn('mermaid render failed', err));
  }
}

// ---------- shared components ----------
function copyButton(text, label = 'copy') {
  return `<button class="btn tiny" data-copy="${esc(text)}" title="${esc(text)}">${esc(label)}</button>`;
}

function sessionCell(session, active) {
  if (!session || !session.id) return '<span class="muted">—</span>';
  let live = '';
  if (Array.isArray(active)) {
    live = active.includes(session.id)
      ? '<span class="dot live" title="active"></span>' : '<span class="dot" title="not active"></span>';
  }
  return `<span class="session">${live}<span class="mono small">${esc(session.name || session.id)}</span>
    ${copyButton(`claude attach ${session.id}`, 'attach')}</span>`;
}

function flattenClickup(summary, depth = 0, out = []) {
  if (!summary) return out;
  (summary.subtasks || []).forEach((sub) => {
    out.push({ ...sub, depth });
    flattenClickup(sub, depth + 1, out);
  });
  return out;
}

function clickupIndex(summary) {
  const map = new Map();
  if (summary) {
    map.set(summary.id, summary);
    if (summary.custom_id) map.set(summary.custom_id, summary);
  }
  flattenClickup(summary).forEach((t) => {
    map.set(t.id, t);
    if (t.custom_id) map.set(t.custom_id, t);
  });
  return map;
}

function clickupCell(ref, cuMap) {
  if (!ref) return '<span class="muted">—</span>';
  const live = cuMap && (cuMap.get(ref.id) || cuMap.get(ref.custom_id));
  const status = live ? ` ${badge(live.status, 'cu')}` : '';
  return `${link(ref.url, ref.custom_id || ref.id)}${status}`;
}

function clickupSyncCell(sync) {
  if (!sync) return '';
  const when = sync.at ? ` <span class="small muted">${ago(sync.at)}</span>` : '';
  return sync.error
    ? ` <span class="pill bad small" title="${esc(sync.error)}">ClickUp sync failed: ${esc(sync.status)}</span>${when}`
    : ` <span class="small">ClickUp: ${esc(sync.status)} ✓</span>${when}`;
}

function taskStateForm(t, states) {
  const opts = (states && states.length ? states : TASK_STATES)
    .map((x) => `<option value="${esc(x)}"${x === t.state ? ' selected' : ''}>${esc(x)}</option>`).join('');
  const msg = ui.taskStateMsg[t.key];
  return `<form class="comment-form" data-form="task-state" data-key="${esc(t.key)}" data-current="${esc(t.state)}">
      <div class="form-row"><label class="small muted">State</label><select name="state">${opts}</select>
        <input type="text" name="note" placeholder="Note (optional)"><button class="btn" type="submit">Apply</button>
        ${msg ? `<span class="small ${msg.error ? 'pill bad' : ''}">${esc(msg.text)}</span>` : ''}</div></form>`;
}

async function submitTaskState(form) {
  const key = form.dataset.key;
  const state = $('[name="state"]', form).value;
  const note = $('[name="note"]', form).value.trim();
  if (state === 'handed-back' && form.dataset.current !== 'handed-back'
      && !window.confirm('Mark this task handed-back from the UI?\n\nThis SKIPS the forge gate (CI, SonarCloud, '
        + 'review threads, tests, issues). The state history will record "via UI (gate skipped)".')) return;
  const btn = $('button[type="submit"]', form);
  btn.disabled = true;
  try {
    const r = await api('/api/task/state', { key, state, note });
    const sync = r.clickup_pushed ? r.clickup_sync : null;
    const cu = !sync ? '' : sync.error ? ` · ClickUp: ${sync.status} failed: ${sync.error}` : ` · ClickUp: ${sync.status} ✓`;
    ui.taskStateMsg[key] = { text: `${r.task.state}${cu}`, error: Boolean(sync && sync.error) };
  } catch (err) {
    ui.taskStateMsg[key] = { text: `Failed: ${err.message}`, error: true };
  } finally {
    btn.disabled = false;
  }
  ui.lastSig = null;
  await render(true);
}

function countsCell(counts) {
  const c = counts || {};
  const bits = [];
  if (c.open_issues) bits.push(`<span class="pill warn" title="open issues">${c.open_issues} issue${c.open_issues > 1 ? 's' : ''}</span>`);
  if (c.open_inbox) bits.push(`<span class="pill info" title="open inbox">${c.open_inbox} inbox</span>`);
  if (c.prs) bits.push(`<span class="pill ${c.prs_green === c.prs ? 'ok' : ''}" title="green / total PRs">PR ${c.prs_green}/${c.prs}</span>`);
  if (c.tests) bits.push(`<span class="pill ${c.tests_green === c.tests ? 'ok' : ''}" title="green / total tests">T ${c.tests_green}/${c.tests}</span>`);
  return bits.join(' ') || '<span class="muted">—</span>';
}

function ciClass(state) {
  return { SUCCESS: 'ok', FAILURE: 'bad', ERROR: 'bad', PENDING: 'warn' }[state] || 'muted';
}

function ciCell(st) {
  if (!st) return '<span class="muted">not fetched</span>';
  const c = st.checks || {};
  const state = c.state || 'NONE';
  let html = `<span class="pill ${ciClass(state)}">${esc(state.toLowerCase())}</span>`;
  if (st.error) html += `<div class="small bad-text" title="${esc(st.error)}">refresh failed</div>`;
  if ((c.failing || []).length) {
    html += `<div class="small">${c.failing.map((f) => link(f.url, f.name, 'class="bad-text"')).join(', ')}</div>`;
  }
  if ((c.pending || []).length) {
    const names = c.pending.map((p) => p.name).join(', ');
    html += `<div class="small muted" title="${esc(names)}">${c.pending.length} pending</div>`;
  }
  return html;
}

function sonarCell(st) {
  const s = st && st.sonar;
  if (!s) return '<span class="muted">—</span>';
  const klass = { OK: 'ok', ERROR: 'bad', WARN: 'warn' }[s.status] || 'muted';
  const failed = (s.conditions || []).filter((c) => c.status !== 'OK')
    .map((c) => `${c.metric} ${c.actual}/${c.threshold}`).join('; ');
  return `${link(s.url, s.status || 'NONE', `class="pill ${klass}" title="${esc(failed)}"`)}`;
}

function prRowId(pr) {
  return `pr:${pr.repo}#${pr.number}`;
}

function threadsHtml(threads) {
  const open = (threads.items || []).filter((t) => !t.is_resolved);
  if (!open.length) return '<p class="muted small">No unresolved threads.</p>';
  return open.map((t) => `
    <div class="thread">
      <div class="thread-head mono small">${esc(t.path || '')}${t.line ? `:${esc(t.line)}` : ''}
        ${t.is_outdated ? '<span class="pill muted">outdated</span>' : ''}</div>
      ${(t.comments || []).map((c) => `
        <div class="comment">
          <div class="comment-head small"><b>${esc(c.author)}</b> · ${ago(c.at)} · ${link(c.url, 'open')}</div>
          ${mdHtml(c.body)}
        </div>`).join('')}
    </div>`).join('');
}

function prRows(prsList, { showTask = false } = {}) {
  const sorted = [...prsList].sort((a, b) => (a.stack_index ?? 0) - (b.stack_index ?? 0));
  return sorted.map((pr) => {
    const st = pr.status;
    const th = (st && st.threads) || { total: 0, resolved: 0, unresolved: 0, items: [] };
    const rowId = prRowId(pr);
    const state = st ? st.state : '';
    const draft = st ? (st.is_draft ? '<span class="pill muted">draft</span>' : '<span class="pill info">ready</span>') : '—';
    const toggle = th.unresolved
      ? `<button class="btn tiny" data-expand="${esc(rowId)}">${th.unresolved} unresolved</button>` : '';
    const main = `<tr class="health-${cls(pr.health || '')}">
      <td class="num" title="stack index">${(pr.stack_index ?? 0) > 0 ? '↳' : ''}${esc(pr.stack_index ?? 0)}</td>
      <td>${link(pr.url, `#${pr.number}`)} ${esc(pr.title || (st && st.title) || '')}
        <div class="small muted mono">${esc(pr.repo)} · ${esc(pr.branch || (st && st.head) || '')} → ${esc(pr.base || (st && st.base) || '')}</div>
        ${showTask ? `<div class="small">${link(taskHref(pr.task_key), shortKey(pr.task_key))}</div>` : ''}</td>
      <td>${draft}</td>
      <td>${badge(state && state.toLowerCase())}</td>
      <td>${ciCell(st)}</td>
      <td>${sonarCell(st)}</td>
      <td class="nowrap">${st ? `${th.resolved}/${th.total} resolved ${toggle}` : '—'}</td>
      <td class="num">${st ? esc(st.comments_count ?? 0) : '—'}</td>
      <td>${st && st.review_decision ? badge(st.review_decision.toLowerCase().replace(/_/g, '-')) : '<span class="muted">—</span>'}</td>
      <td class="nowrap">${ago(st && st.fetched_at)}</td>
    </tr>`;
    const detail = th.unresolved ? `<tr class="expand-row" data-expand-id="${esc(rowId)}"><td></td>
      <td colspan="9">${threadsHtml(th)}</td></tr>` : '';
    return main + detail;
  }).join('');
}

// Plain link helper for internal hrefs (link() only accepts http URLs).
function ilink(href, text) {
  return `<a href="${esc(href)}">${esc(text)}</a>`;
}

function prTable(prsList, opts) {
  if (!prsList.length) return '<p class="muted">No PRs.</p>';
  return `<div class="table-wrap"><table class="grid prs">
    <colgroup><col class="c-idx"><col><col class="c-sm"><col class="c-sm"><col class="c-ci"><col class="c-sm">
      <col class="c-th"><col class="c-xs"><col class="c-rv"><col class="c-sm"></colgroup>
    <thead><tr><th>#</th><th>PR</th><th>Draft</th><th>State</th><th>CI</th><th>Sonar</th><th>Threads</th>
    <th>Cmts</th><th>Review</th><th>Fetched</th></tr></thead>
    <tbody>${prRows(prsList, opts)}</tbody></table></div>`;
}

function messageHtml(m) {
  const meta = [];
  if (m.author !== 'worker') {
    meta.push(badge(m.status));
    meta.push(m.delivered ? '<span class="pill ok">delivered</span>' : '<span class="pill muted">not delivered</span>');
  }
  if (m.reply_to) meta.push(`<span class="small muted">↩ ${esc(m.reply_to)}</span>`);
  return `<div class="msg author-${cls(m.author)}" id="msg-${esc(m.id)}">
    <div class="msg-head small"><b>${esc(m.author)}</b> <span class="mono muted">${esc(m.id)}</span>
      ${m.issue_id ? `<span class="pill muted">${esc(m.issue_id)}</span>` : ''} · ${ago(m.at)} ${meta.join(' ')}</div>
    ${mdHtml(m.body)}
  </div>`;
}

function threadBlock(messages, { key, issueId, placeholder }) {
  const formId = `c:${key}:${issueId || ''}`;
  return `<div class="thread-list">${messages.length ? messages.map(messageHtml).join('')
    : '<p class="muted">No messages yet.</p>'}</div>
    <form class="comment-form" data-form="comment" data-form-id="${esc(formId)}" data-key="${esc(key)}"
      data-issue="${esc(issueId || '')}">
      <label class="small muted" for="ta-${esc(formId)}">${esc(placeholder)}</label>
      <textarea id="ta-${esc(formId)}" rows="4" placeholder="${esc(placeholder)}" required></textarea>
      <div class="form-row"><button class="btn primary" type="submit">Send</button>
        <span class="small muted">Adds a user message to the task inbox and nudges the worker if its session is idle.</span></div>
    </form>`;
}

function walkTree(node, fn, depth = 0, ancestors = []) {
  (node.children || []).forEach((child) => {
    fn(child, depth, ancestors);
    walkTree(child, fn, depth + 1, [...ancestors, child.key]);
  });
}

// ---------- views ----------
const views = {};

views.projects = {
  load: () => api('/api/projects'),
  build(projects) {
    if (!projects.length) return '<h1>Projects</h1><p class="muted">No projects in FORGE_HOME yet. Use <code>forge init-project</code>.</p>';
    const cards = projects.map((p) => {
      const c = p.counts || {};
      const states = Object.entries(p.task_states || {})
        .map(([s, n]) => `${badge(s)}<span class="count">${n}</span>`).join(' ');
      const health = c.prs_failing ? 'bad' : (c.prs && c.prs_green === c.prs ? 'ok' : (c.prs ? 'warn' : 'muted'));
      return `<article class="card">
        <header><a class="card-title" href="${projectHref(p.slug)}">${esc(p.title || p.slug)}</a> ${badge(p.state)}</header>
        <div class="small muted mono">${esc(p.slug)}${p.clickup_parent ? ` · ${link(p.clickup_parent.url, p.clickup_parent.custom_id || p.clickup_parent.id)}` : ''}
          ${p.clickup ? badge(p.clickup.status, 'cu') : ''}</div>
        <div class="states">${states || '<span class="muted">no tasks</span>'}</div>
        <dl class="stats">
          <div><dt>Tasks</dt><dd>${c.tasks || 0}</dd></div>
          <div><dt>Open issues</dt><dd>${ilink(`#/issues${qs({ project: p.slug })}`, c.open_issues || 0)}
            ${c.awaiting_user ? `<a class="pill bad" href="#/issues${qs({ project: p.slug, state: 'awaiting-user' })}">${c.awaiting_user} awaiting you</a>` : ''}</dd></div>
          <div><dt>Open inbox</dt><dd>${c.open_inbox || 0}</dd></div>
          <div><dt>PR health</dt><dd><a href="#/prs${qs({ project: p.slug })}" class="pill ${health}">${c.prs_green || 0}/${c.prs || 0} green</a>
            ${c.prs_failing ? `<span class="pill bad">${c.prs_failing} failing</span>` : ''}</dd></div>
          <div><dt>Tests</dt><dd>${c.tests_green || 0}/${c.tests || 0}</dd></div>
        </dl>
      </article>`;
    }).join('');
    return `<h1>Projects</h1><div class="cards">${cards}</div>`;
  },
};

views.project = {
  load: (r) => api(`/api/project${qs({ slug: r.parts[1] })}`),
  build(d) {
    const p = d.project;
    const cuMap = clickupIndex(d.clickup);
    const byKey = new Map();
    walkTree(d.tree, (n) => byKey.set(n.key, n));
    const rows = [];
    walkTree(d.tree, (n, depth, ancestors) => {
      const hasKids = (n.children || []).length > 0;
      const deps = (n.depends_on || (n.task && n.task.depends_on) || []);
      rows.push(`<tr data-key="${esc(n.key)}" data-anc="${esc(ancestors.join(' '))}">
        <td class="tree-cell" style="padding-left:${0.5 + depth * 1.25}rem">
          ${hasKids ? `<button class="caret" data-collapse="${esc(n.key)}" aria-label="toggle">${ui.collapsed.has(n.key) ? '▸' : '▾'}</button>` : '<span class="caret-pad"></span>'}
          ${ilink(taskHref(n.key), shortKey(n.key))}<div class="small">${esc(n.title)}</div></td>
        <td>${badge(n.state)}</td>
        <td>${sessionCell(n.session, d.active_sessions)}</td>
        <td class="small">${deps.map((k) => `${ilink(taskHref(k), shortKey(k))} ${badge(byKey.get(k) && byKey.get(k).state)}`).join('<br>') || '<span class="muted">—</span>'}</td>
        <td>${clickupCell(n.clickup, cuMap)}</td>
        <td>${countsCell(n.counts)}</td>
      </tr>`);
    });
    const depRows = [];
    byKey.forEach((n) => {
      (n.depends_on || []).forEach((dep) => {
        const target = byKey.get(dep);
        const met = target && DONE_STATES.includes(target.state);
        depRows.push(`<tr><td>${ilink(taskHref(n.key), shortKey(n.key))}</td><td>${badge(n.state)}</td>
          <td>→ ${ilink(taskHref(dep), shortKey(dep))}</td><td>${badge(target ? target.state : 'missing')}</td>
          <td>${met ? '<span class="pill ok">met</span>' : '<span class="pill warn">waiting</span>'}</td></tr>`);
      });
    });
    const trackedIds = new Map();
    byKey.forEach((n) => {
      if (n.clickup) { trackedIds.set(n.clickup.id, n.key); if (n.clickup.custom_id) trackedIds.set(n.clickup.custom_id, n.key); }
    });
    let cuPanel = '<p class="muted small">No ClickUp parent.</p>';
    if (p.clickup_parent) {
      const s = d.clickup;
      cuPanel = `<p>${link(p.clickup_parent.url, `${p.clickup_parent.custom_id || p.clickup_parent.id}${s ? ` — ${s.name}` : ''}`)}
        ${s ? badge(s.status, 'cu') : '<span class="muted small">(status not fetched yet)</span>'}
        ${s && (s.assignees || []).length ? `<span class="small muted">· ${esc(s.assignees.join(', '))}</span>` : ''}
        ${d.clickup_fetched_at ? `<span class="small muted">· fetched ${ago(d.clickup_fetched_at)}</span>` : ''}</p>`;
      const subs = flattenClickup(s);
      if (subs.length) {
        cuPanel += `<table class="grid compact"><thead><tr><th>ClickUp task</th><th>Status</th><th>forge task</th></tr></thead><tbody>
          ${subs.map((t) => {
            const fk = trackedIds.get(t.id) || trackedIds.get(t.custom_id);
            return `<tr><td style="padding-left:${0.5 + t.depth * 1.25}rem">${link(t.url, t.custom_id || t.id)} ${esc(t.name)}</td>
              <td>${badge(t.status, 'cu')}</td><td>${fk ? ilink(taskHref(fk), shortKey(fk)) : '<span class="muted small">not tracked</span>'}</td></tr>`;
          }).join('')}</tbody></table>`;
      }
    }
    const repos = (p.repos || []).map((r) => `<span class="mono small">${esc(r.github || r.path)}</span>`).join(', ');
    return `<nav class="crumbs">${ilink('#/', 'Projects')} / <b>${esc(p.slug)}</b></nav>
      <h1>${esc(p.title || p.slug)} ${badge(p.state)}</h1>
      <div class="meta">
        <span>Coordinator: ${sessionCell(p.coordinator_session, d.active_sessions)}</span>
        <span>Repos: ${repos || '—'}</span><span>Max parallel: ${esc(p.max_parallel ?? '—')}</span>
        <span>Updated ${ago(p.updated)}</span>
        <span>${fileLink(p.slug, 'status.md')} · ${fileLink(p.slug, 'decisions.md')} · ${fileLink(p.slug, 'project.md')} · ${fileLink(p.slug, 'spec.md')} · ${fileLink(p.slug, 'plan.md')}</span>
      </div>
      <div class="tabs"><span class="tab active">Tasks</span>
        <a class="tab" href="#/prs${qs({ project: p.slug })}">PRs</a>
        <a class="tab" href="#/issues${qs({ project: p.slug })}">Issues</a>
        <a class="tab" href="${filesHref(`${p.slug}/decisions.md`)}">Decisions</a>
        <a class="tab" href="${agentsHref(p.slug)}">Agents</a>
        <a class="tab" href="${filesHref(p.slug)}">Files</a></div>
      <section><div class="table-wrap"><table class="grid tree">
        <thead><tr><th>Task</th><th>State</th><th>Session</th><th>Depends on</th><th>ClickUp</th><th>Open / health</th></tr></thead>
        <tbody>${rows.join('') || '<tr><td colspan="6" class="muted">No tasks yet.</td></tr>'}</tbody></table></div></section>
      <div class="two-col">
        <section><h2>Dependencies</h2>${depRows.length ? `<table class="grid compact"><thead><tr><th>Task</th><th>State</th>
          <th>Depends on</th><th>Dep state</th><th></th></tr></thead><tbody>${depRows.join('')}</tbody></table>`
          : '<p class="muted small">No dependencies.</p>'}</section>
        <section><h2>ClickUp</h2>${cuPanel}</section>
      </div>`;
  },
  after: applyCollapse,
};

views.task = {
  load: (r) => api(`/api/task${qs({ key: r.parts[1] })}`),
  build(d) {
    const t = d.task;
    const segs = t.key.split('/');
    const crumbs = [ilink('#/', 'Projects'), ilink(projectHref(segs[0]), segs[0])];
    for (let i = 2; i < segs.length; i += 1) crumbs.push(ilink(taskHref(segs.slice(0, i).join('/')), segs[i - 1]));
    crumbs.push(`<b>${esc(segs[segs.length - 1])}</b>`);
    const gate = t.gate || {};
    const gateHtml = gate.last_run
      ? `<p>${gate.passed ? '<span class="pill ok">passed</span>' : '<span class="pill bad">failing</span>'}
          <span class="small muted">last run ${ago(gate.last_run)} · consecutive blocks ${esc(gate.consecutive_blocks ?? 0)}</span></p>
         ${(gate.reasons || []).length ? `<ul class="reasons">${gate.reasons.map((r) => `<li>${esc(r)}</li>`).join('')}</ul>` : ''}`
      : '<p class="muted small">Gate not run yet.</p>';
    const hb = t.handback;
    const children = (d.tree && d.tree.children) || [];
    const tests = d.tests.length ? `<div class="table-wrap"><table class="grid compact"><thead><tr><th>ID</th><th>Name</th><th>Type</th>
      <th>Status</th><th>Command</th><th>Evidence</th></tr></thead><tbody>${d.tests.map((x) => `<tr>
        <td class="mono small">${esc(x.id)}</td><td>${esc(x.name)}${x.expected ? `<div class="small muted">expect: ${esc(x.expected)}</div>` : ''}</td>
        <td>${esc(x.type)}</td><td>${badge(x.status)}${x.skip_reason ? `<div class="small muted">${esc(x.skip_reason)}</div>` : ''}</td>
        <td>${x.command ? `<code class="small">${esc(x.command)}</code>` : ''}</td>
        <td class="small">${isUrl(x.evidence) ? link(x.evidence, 'evidence') : esc(x.evidence)}</td></tr>`).join('')}</tbody></table></div>`
      : '<p class="muted">No tests recorded.</p>';
    const issues = d.issues.length ? `<table class="grid compact"><thead><tr><th>ID</th><th>Title</th><th>Severity</th><th>State</th><th>Updated</th></tr></thead>
      <tbody>${d.issues.map((i) => `<tr><td class="mono small">${esc(i.id)}</td><td>${ilink(issueHref(t.key, i.id), i.title)}</td>
        <td>${badge(i.severity)}</td><td>${badge(issueState(i))}</td><td>${ago(i.updated)}</td></tr>`).join('')}</tbody></table>`
      : '<p class="muted">No issues.</p>';
    const history = (t.state_history || []).map((h) => `<li>${badge(h.state)} ${ago(h.at)} ${h.note ? `<span class="small muted">— ${esc(h.note)}</span>` : ''}</li>`).join('');
    return `<nav class="crumbs">${crumbs.join(' / ')}</nav>
      <h1>${esc(t.title)} ${badge(t.state)}</h1>
      ${taskStateForm(t, d.task_states)}
      <div class="meta">
        <span>Session: ${sessionCell(t.session, d.active_sessions)} ${ilink(agentHref(t.key), t.session ? 'Watch agent →' : 'Agent →')}</span>
        <span>ClickUp: ${clickupCell(t.clickup, null)}${t.clickup ? clickupSyncCell(t.clickup_sync) : ''}</span>
        <span>Repo: <span class="mono small">${esc(t.github || t.repo || '—')}</span> @ ${esc(t.base_branch || '')}</span>
        ${(t.depends_on || []).length ? `<span>Depends on: ${t.depends_on.map((k) => ilink(taskHref(k), shortKey(k))).join(', ')}</span>` : ''}
        <span>Updated ${ago(t.updated)}</span>
        <span>${ilink(filesHref(keyRel(t.key)), 'Files →')}</span>
      </div>
      <div class="two-col">
        <section><h2>Gate ${fileLink(t.key, 'status.md')}</h2>${gateHtml}</section>
        <section><h2>Handback ${fileLink(t.key, 'HANDBACK.md')}</h2>${hb ? `${mdHtml(hb.summary)}<p class="small"><b>Docs:</b> ${esc(hb.docs)}</p><p class="small muted">${ago(hb.at)}</p>`
          : '<p class="muted small">Not handed back.</p>'}</section>
      </div>
      ${children.length ? `<section><h2>Subtasks</h2><table class="grid compact"><thead><tr><th>Task</th><th>State</th><th>Session</th><th>Open / health</th></tr></thead>
        <tbody>${children.map((c) => `<tr><td>${ilink(taskHref(c.key), shortKey(c.key))} <span class="small">${esc(c.title)}</span></td>
          <td>${badge(c.state)}</td><td>${sessionCell(c.session, d.active_sessions)}</td><td>${countsCell(c.counts)}</td></tr>`).join('')}</tbody></table></section>` : ''}
      <section><h2>Spec ${fileLink(t.key, 'spec.md')}</h2>${mdHtml(d.spec_md)}</section>
      <section><h2>Tests ${fileLink(t.key, 'tests.md')}</h2>${tests}</section>
      <section><h2>PRs ${fileLink(t.key, 'PRs.md')}</h2>${prTable(d.prs.map((p) => ({ ...p, task_key: t.key })))}</section>
      <section><h2>Issues ${fileLink(t.key, 'issues.md')}</h2>${issues}</section>
      <section><h2>Inbox</h2>${threadBlock(d.inbox, { key: t.key, issueId: null, placeholder: 'Message the worker…' })}</section>
      <div class="two-col">
        <section><h2>Log (tail) ${fileLink(t.key, 'log.md')}</h2><pre class="log">${esc(d.log_tail || '(empty)')}</pre></section>
        <section><h2>State history</h2><ul class="history">${history}</ul></section>
      </div>`;
  },
};

views.prs = {
  async load(r) {
    const project = r.params.get('project') || '';
    const [prs, projects] = await Promise.all([api(`/api/prs${qs({ project })}`), api('/api/projects')]);
    return { prs, projects, project };
  },
  build(d) {
    const groups = new Map();
    d.prs.forEach((pr) => {
      if (!groups.has(pr.project)) groups.set(pr.project, new Map());
      const tasks = groups.get(pr.project);
      if (!tasks.has(pr.task_key)) tasks.set(pr.task_key, []);
      tasks.get(pr.task_key).push(pr);
    });
    const summary = { green: 0, failing: 0, pending: 0 };
    d.prs.forEach((pr) => { summary[pr.health] = (summary[pr.health] || 0) + 1; });
    let body = '';
    groups.forEach((tasks, project) => {
      body += `<section class="group"><h2>${ilink(projectHref(project), project)}</h2>`;
      tasks.forEach((list, key) => {
        body += `<h3>${ilink(taskHref(key), shortKey(key))} <span class="small">${esc(list[0].task_title || '')}</span> ${badge(list[0].task_state)}</h3>
          ${prTable(list)}`;
      });
      body += '</section>';
    });
    return `<h1>Pull requests</h1>
      <div class="toolbar">
        <label>Project <select data-filter="project">${projectOptions(d.projects, d.project)}</select></label>
        <button class="btn" data-action="refresh" data-key="${esc(d.project)}">Refresh</button>
        <span class="pill ok">${summary.green} green</span><span class="pill bad">${summary.failing} failing</span>
        <span class="pill warn">${summary.pending} pending</span>
      </div>
      ${body || '<p class="muted">No PRs recorded.</p>'}`;
  },
};

function projectOptions(projects, selected) {
  return ['<option value="">All projects</option>'].concat(projects.map((p) => (
    `<option value="${esc(p.slug)}" ${p.slug === selected ? 'selected' : ''}>${esc(p.title || p.slug)}</option>`))).join('');
}

function simpleOptions(values, selected, allLabel) {
  return [`<option value="">${esc(allLabel)}</option>`].concat(values.map((v) => (
    `<option value="${esc(v)}" ${v === selected ? 'selected' : ''}>${esc(v)}</option>`))).join('');
}

views.issues = {
  async load(r) {
    const f = { project: r.params.get('project') || '', status: r.params.get('status') || '', severity: r.params.get('severity') || '' };
    const [issues, projects] = await Promise.all([api(`/api/issues${qs(f)}`), api('/api/projects')]);
    // state is filtered here so the "awaiting you" pill counts regardless of the state filter
    return { issues, projects, f: { ...f, state: r.params.get('state') || 'active' } };
  },
  build(d) {
    const sevOrder = (s) => { const i = SEVERITIES.indexOf(s); return i < 0 ? 99 : i; };
    const awaiting = d.issues.filter((i) => issueState(i) === 'awaiting-user').length;
    const stateOk = (i) => (d.f.state === 'all' ? true
      : d.f.state === 'active' ? !CLOSED_ISSUE_STATES.includes(issueState(i)) : issueState(i) === d.f.state);
    const sorted = d.issues.filter(stateOk).sort((a, b) => sevOrder(a.severity) - sevOrder(b.severity)
      || String(b.updated || '').localeCompare(String(a.updated || '')));
    // project -> task -> issues, keeping the severity order within each task
    const groups = new Map();
    sorted.forEach((i) => {
      if (!groups.has(i.project)) groups.set(i.project, new Map());
      const tasks = groups.get(i.project);
      if (!tasks.has(i.task_key)) tasks.set(i.task_key, []);
      tasks.get(i.task_key).push(i);
    });
    const sevPills = (list) => SEVERITIES.map((s) => {
      const n = list.filter((i) => i.severity === s).length;
      return n ? `${badge(s)}<span class="small">${n}</span>` : '';
    }).join(' ');
    const rows = (list) => list.map((i) => `<tr>
      <td>${badge(i.severity)}</td><td class="mono small">${esc(i.id)}</td>
      <td>${ilink(issueHref(i.task_key, i.id), i.title)}${i.artifact_url ? ` ${link(i.artifact_url, 'artifact', 'class="pill info"')}` : ''}</td>
      <td>${badge(issueState(i))}</td>
      <td class="small">${i.decision ? esc(i.decision.text) : '<span class="muted">—</span>'}</td>
      <td class="nowrap">${ago(i.updated)}</td></tr>`).join('');
    let body = '';
    groups.forEach((tasks, project) => {
      const all = [...tasks.values()].flat();
      body += `<section class="group"><h2>${ilink(projectHref(project), project)} <span class="small muted">${all.length}</span> ${sevPills(all)}</h2>`;
      tasks.forEach((list, key) => {
        body += `<h3>${ilink(taskHref(key), shortKey(key))} ${sevPills(list)}</h3>
          <div class="table-wrap"><table class="grid"><thead><tr><th>Severity</th><th>ID</th><th>Title</th><th>State</th>
          <th>Decision</th><th>Updated</th></tr></thead><tbody>${rows(list)}</tbody></table></div>`;
      });
      body += '</section>';
    });
    return `<h1>Issues</h1>
      <div class="toolbar">
        <label>Project <select data-filter="project">${projectOptions(d.projects, d.f.project)}</select></label>
        <label>State <select data-filter="state">${[['active', 'Not closed'], ['all', 'Any state']].concat(ISSUE_STATES.map((x) => [x, x]))
          .map(([v, l]) => `<option value="${esc(v)}" ${v === d.f.state ? 'selected' : ''}>${esc(l)}</option>`).join('')}</select></label>
        ${d.f.status ? `<label>Status <select data-filter="status">${simpleOptions(ISSUE_STATUSES, d.f.status, 'Any status')}</select></label>` : ''}
        <label>Severity <select data-filter="severity">${simpleOptions(SEVERITIES, d.f.severity, 'Any severity')}</select></label>
        <span class="small muted">${sorted.length} issue${sorted.length === 1 ? '' : 's'}</span>
        ${awaiting ? `<a class="pill bad" href="#/issues${qs({ project: d.f.project, severity: d.f.severity, state: 'awaiting-user' })}">${awaiting} awaiting you</a>`
          : '<span class="pill ok">nothing awaiting you</span>'}
      </div>
      ${body || '<p class="muted">No matching issues.</p>'}`;
  },
};

const msgLink = (id) => `<a href="#" class="mono" data-scroll="msg-${esc(id)}" title="show in the thread">${esc(id)}</a>`;

function issueTimeline(i) {
  const hist = [...(i.history || [])].reverse();
  if (!hist.length) return '<p class="muted small">No history.</p>';
  return `<ol class="timeline">${hist.map((h) => `<li class="tl-${cls(h.state)}">${badge(h.state)}
    <span class="small"><b>${esc(h.by || '?')}</b> · <span title="${esc(h.at)}">${ago(h.at)}</span>
    ${h.comment_id ? ` · ${msgLink(h.comment_id)}` : ''}</span>
    ${h.note ? `<div class="small">${esc(h.note)}</div>` : ''}</li>`).join('')}</ol>`;
}

function issueActions(key, i, nextStates) {
  const st = issueState(i);
  const base = `data-key="${esc(key)}" data-issue="${esc(i.id)}"`;
  if (CLOSED_ISSUE_STATES.includes(st)) {
    return `<form class="comment-form" data-form="issue-state" ${base}>
      <input type="hidden" name="state" value="open">
      <input type="text" name="note" placeholder="Why reopen? (optional)">
      <div class="form-row"><button class="btn" type="submit">Reopen</button></div></form>`;
  }
  const opts = nextStates.map((x) => `<option value="${esc(x)}">${esc(x)}</option>`).join('');
  return `<form class="comment-form" data-form="decide" ${base} data-form-id="d:${esc(key)}:${esc(i.id)}">
      <label class="small muted" for="dec-${esc(i.id)}">Record decision (sent to the worker as “Decision on ${esc(i.id)}: …”)</label>
      <textarea id="dec-${esc(i.id)}" rows="3" placeholder="What did you decide?" required></textarea>
      <div class="form-row"><button class="btn primary" type="submit">Record decision</button></div></form>
    ${opts ? `<form class="comment-form" data-form="issue-state" ${base}>
      <label class="small muted">Change state</label>
      <div class="form-row"><select name="state">${opts}</select>
        <input type="text" name="note" placeholder="Note (optional)"><button class="btn" type="submit">Apply</button></div></form>` : ''}`;
}

async function submitIssueForm(form) {
  const btn = $('button[type="submit"]', form);
  btn.disabled = true;
  try {
    const payload = { key: form.dataset.key, id: form.dataset.issue };
    if (form.dataset.form === 'decide') {
      payload.decision = $('textarea', form).value.trim();
      if (!payload.decision) return;
      const r = await api('/api/issue/decide', payload);
      delete ui.drafts[form.dataset.formId];
      const dl = r.delivery || {};
      toast(`${payload.id} decided — ${dl.mode || 'not sent'}${dl.detail ? `: ${String(dl.detail).slice(0, 160)}` : ''}`, dl.mode === 'failed');
    } else {
      payload.state = $('[name="state"]', form).value;
      payload.note = ($('[name="note"]', form) || {}).value || '';
      const r = await api('/api/issue/state', payload);
      toast(`${payload.id}: ${r.state}`);
    }
    ui.lastSig = null;
    await render(true);
  } catch (err) {
    toast(`Failed: ${err.message}`, true);
  } finally {
    btn.disabled = false;
  }
}

views.issue = {
  load: (r) => api(`/api/issue${qs({ key: r.parts[1], id: r.parts[2] })}`),
  build(d) {
    const i = d.issue;
    const key = d.task.key;
    return `<nav class="crumbs">${ilink('#/issues', 'Issues')} / ${ilink(projectHref(key.split('/')[0]), key.split('/')[0])}
        / ${ilink(taskHref(key), shortKey(key))} / <b>${esc(i.id)}</b></nav>
      <h1>${esc(i.title)} ${badge(i.severity)} ${badge(issueState(i))}</h1>
      <div class="meta"><span>Task: ${ilink(taskHref(key), d.task.title || key)} ${badge(d.task.state)}</span>
        <span>Created ${ago(i.created)}</span><span>Updated ${ago(i.updated)}</span>
        <span>${fileLink(key, i.file || `issues/${i.id}.md`, 'Open in Files →')}</span>
        ${i.artifact_url ? link(i.artifact_url, 'Open artifact ↗', 'class="btn primary"') : ''}</div>
      ${i.decision ? `<div class="callout decision"><b>Decision</b> <span class="small muted">by ${esc(i.decision.by)} · ${ago(i.decision.at)}
        ${i.decision.comment_id ? ` · ${msgLink(i.decision.comment_id)}` : ''}</span>${mdHtml(i.decision.text)}</div>` : ''}
      ${i.resolution ? `<div class="callout"><b>Resolution:</b> ${esc(i.resolution)}</div>` : ''}
      <div class="two-col">
        <section><h2>Timeline</h2>${issueTimeline(i)}</section>
        <section><h2>Actions</h2>${issueActions(key, i, d.next_states || [])}</section>
      </div>
      <section class="issue-body">${mdHtml(d.body_md)}</section>
      <section><h2>Discussion</h2>${threadBlock(d.thread, { key, issueId: i.id, placeholder: 'How should this be resolved?' })}</section>`;
  },
};

// ---------- agents ----------
function liveCell(r) {
  const l = r.live || {};
  if (l.live === null || l.live === undefined) return '<span class="dot" title="live status unknown"></span> <span class="small muted">unknown</span>';
  if (!l.live) return `<span class="dot" title="not live"></span> <span class="small muted">${r.session ? 'not live' : 'no session'}</span>`;
  const st = [l.state, l.status].filter(Boolean);
  return `<span class="dot live" title="live"></span> ${st.map((x) => badge(x)).join(' ') || '<span class="small">live</span>'}`;
}

function sessionInfo(r) {
  const s = r.session;
  if (!s) return '<span class="muted">—</span>';
  return `<span class="mono small">${esc(s.name || s.id)}</span>${r.session_kind ? ` <span class="pill muted">${esc(r.session_kind)}</span>` : ''}`;
}

function agentButtons(r, { watch = true } = {}) {
  const b = [];
  if (watch && r.session) b.push(`<a class="btn tiny" href="${agentHref(r.key)}">Watch</a>`);
  if (r.attach) {
    b.push(`<button class="btn tiny" data-action="agent-open" data-key="${esc(r.key)}">Open in Terminal</button>`);
    b.push(copyButton(r.attach, 'Copy attach'));
  }
  if (!r.session && r.kind === 'task') b.push(`<button class="btn tiny" data-action="agent-dispatch" data-key="${esc(r.key)}">Dispatch</button>`);
  return b.join(' ');
}

function agentTitle(r) {
  return r.kind === 'coordinator'
    ? `${ilink(projectHref(r.project), r.project)} <span class="pill st-coordinating">coordinator</span>`
    : `${ilink(taskHref(r.key), shortKey(r.key))} <span class="small">${esc(r.title)}</span>`;
}

views.agents = {
  load: (r) => api(`/api/agents${qs({ project: r.params.get('project') || '' })}`).then((rows) => ({ rows, project: r.params.get('project') || '' })),
  build(d) {
    const groups = new Map();
    d.rows.forEach((r) => { if (!groups.has(r.project)) groups.set(r.project, []); groups.get(r.project).push(r); });
    const liveCount = d.rows.filter((r) => r.live && r.live.live).length;
    let body = '';
    groups.forEach((rows, project) => {
      body += `<section class="group"><h2>${ilink(projectHref(project), project)}</h2><div class="table-wrap"><table class="grid">
        <thead><tr><th>Task</th><th>State</th><th>Session</th><th>Live</th><th>Last activity</th><th>Inbox</th><th></th></tr></thead>
        <tbody>${rows.map((r) => `<tr>
          <td>${agentTitle(r)}</td><td>${badge(r.state)}</td><td>${sessionInfo(r)}</td><td class="nowrap">${liveCell(r)}</td>
          <td class="nowrap">${ago(r.last_activity)}</td>
          <td>${r.pending_inbox ? `<span class="pill warn" title="undelivered user messages">${r.pending_inbox} pending</span>` : ''}
            ${r.open_inbox ? `<span class="pill info" title="open inbox">${r.open_inbox} open</span>` : ''}</td>
          <td class="nowrap">${agentButtons(r)}</td></tr>`).join('')}</tbody></table></div></section>`;
    });
    return `<h1>Agents</h1>
      <div class="toolbar"><span class="small muted">${d.rows.length} row${d.rows.length === 1 ? '' : 's'} · ${liveCount} live</span>
        ${d.project ? ilink(agentsHref(''), 'All projects') : ''}</div>
      ${body || '<p class="muted">No projects.</p>'}`;
  },
};

function entryHtml(e, i) {
  const id = `${e.at}|${e.kind}|${i}`;
  const when = ago(e.at);
  if (e.kind === 'text') {
    const who = { user: 'user', assistant: 'agent', cross: 'cross-session', system: 'system' }[e.role] || e.role;
    return `<div class="msg tr-${cls(e.role)}"><div class="msg-head small"><b>${esc(who)}</b> · ${when}</div>
      ${e.role === 'system' ? `<div class="small muted">${esc(e.text)}</div>` : mdHtml(e.text)}</div>`;
  }
  const label = e.kind === 'tool_use' ? `▸ ${esc(e.tool || 'tool')}` : `↳ ${esc(e.tool || 'result')}${e.is_error ? ' error' : ''}`;
  const first = String(e.text || '').split('\n')[0];
  return `<details class="tool ${e.is_error ? 'tool-error' : ''} ${e.kind}" data-entry="${esc(id)}" ${ui.expanded.has(id) ? 'open' : ''}>
    <summary class="small"><span class="mono">${label}</span> <span class="muted mono">${esc(first.slice(0, 160))}</span> <span class="muted">· ${when}</span></summary>
    <pre class="log">${esc(e.text || '')}</pre></details>`;
}

function agentHeader(r) {
  const s = r.session || {};
  const crumbs = r.kind === 'coordinator'
    ? `${ilink(agentsHref(r.project), 'Agents')} / ${ilink(projectHref(r.project), r.project)} / <b>coordinator</b>`
    : `${ilink(agentsHref(r.project), 'Agents')} / ${ilink(projectHref(r.project), r.project)} / ${ilink(taskHref(r.key), shortKey(r.key))}`;
  return `<nav class="crumbs">${crumbs}</nav>
    <h1>${esc(r.title || r.key)} ${badge(r.state)}</h1>
    <div class="meta"><span>Session: ${sessionInfo(r)}</span><span>${liveCell(r)}</span>
      <span>Last activity ${ago(r.last_activity)}</span>
      ${r.pending_inbox ? `<span class="pill warn">${r.pending_inbox} undelivered</span>` : ''}
      ${s.id ? `<span class="mono small muted">${esc(s.id)}</span>` : ''}
      <span>${agentButtons(r, { watch: false })}</span></div>`;
}

function transcriptHtml(r) {
  if (!r.session) return '<p class="muted">Not dispatched yet — no session to watch.</p>';
  if (!r.transcript) return '<p class="muted">No transcript file found for this session.</p>';
  if (!(r.entries || []).length) return '<p class="muted">Transcript is empty.</p>';
  return r.entries.map(entryHtml).join('');
}

views.agent = {
  load: (r) => api(`/api/agent${qs({ key: r.parts[1], limit: AGENT_TAIL_LIMIT })}`),
  build(r) {
    const canSend = r.session || r.kind === 'coordinator';
    return `<div id="agent-head">${agentHeader(r)}</div>
      <section><h2>Transcript <span class="small muted">(last ${AGENT_TAIL_LIMIT} entries, refreshes every ${AGENT_REFRESH_MS / 1000}s)</span></h2>
        <div id="transcript" class="transcript">${transcriptHtml(r)}</div></section>
      <section><form class="comment-form" data-form="agent-msg" data-key="${esc(r.key)}">
        <label class="small muted" for="agent-msg-ta">Message this agent</label>
        <textarea id="agent-msg-ta" rows="3" placeholder="Message this agent… (⌘/Ctrl+Enter to send)" ${canSend ? '' : 'disabled'}></textarea>
        <div class="form-row"><button class="btn primary" type="submit" ${canSend ? '' : 'disabled'}>Send</button>
          <span id="agent-send-result" class="small muted">${canSend ? 'Live sessions get it within seconds; idle ones are resumed with it.' : 'Dispatch the task first.'}</span></div>
      </form></section>`;
  },
  after(r) {
    const box = $('#transcript');
    if (box) box.scrollTop = box.scrollHeight;
    const draft = ui.drafts[`agent:${r.key}`];
    if (draft) $('#agent-msg-ta').value = draft;
    ui.agentSig = JSON.stringify(r);
    startAgentPoll(r.key);
  },
};

function stopAgentPoll() {
  if (ui.agentTimer) clearInterval(ui.agentTimer);
  ui.agentTimer = null;
}

function startAgentPoll(key) {
  stopAgentPoll();
  ui.agentTimer = setInterval(async () => {
    if (document.hidden || parseRoute().name !== 'agent') return;
    let r;
    try { r = await api(`/api/agent${qs({ key, limit: AGENT_TAIL_LIMIT })}`); } catch (err) {
      $('#updated').textContent = `Refresh failed: ${err.message}`; return;
    }
    if (parseRoute().name !== 'agent' || parseRoute().parts[1] !== key) return;
    $('#updated').textContent = `Updated ${new Date().toLocaleTimeString()}`;
    const sig = JSON.stringify(r);
    if (sig === ui.agentSig) return;
    ui.agentSig = sig;
    const head = $('#agent-head');
    if (head) head.innerHTML = agentHeader(r);
    const box = $('#transcript');
    if (!box) return;
    const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
    const prev = box.scrollTop;
    box.innerHTML = transcriptHtml(r);
    box.scrollTop = atBottom ? box.scrollHeight : prev;
  }, AGENT_REFRESH_MS);
}

async function sendAgentMessage(form) {
  const ta = $('textarea', form);
  const text = ta.value.trim();
  if (!text) return;
  const btn = $('button[type="submit"]', form);
  const out = $('#agent-send-result');
  btn.disabled = true;
  out.textContent = 'Sending… (the live bridge can take up to a minute)';
  try {
    const r = await api('/api/agent/message', { key: form.dataset.key, text });
    const label = { sent: 'Sent live', resumed: 'Resumed session with message', queued: 'Queued for relay', failed: 'Failed' }[r.mode] || r.mode;
    out.innerHTML = `<span class="pill ${r.delivered ? 'ok' : 'warn'}">${esc(label)}</span> ${r.message ? `<span class="mono">${esc(r.message.id)}</span>` : ''} <span class="muted">${esc(r.detail || '')}</span>`;
    delete ui.drafts[`agent:${form.dataset.key}`];
    ta.value = '';
  } catch (err) {
    out.innerHTML = `<span class="pill bad">Send failed</span> ${esc(err.message)}`;
  } finally {
    btn.disabled = false;
  }
}

async function agentAction(btn) {
  const action = btn.dataset.action;
  btn.disabled = true;
  try {
    if (action === 'agent-open') {
      const r = await api('/api/agent/open', { key: btn.dataset.key });
      toast(`Opened Terminal: ${r.command}`);
    } else {
      const r = await api('/api/agent/dispatch', { key: btn.dataset.key });
      toast(r.output, !r.dispatched);
      ui.lastSig = null;
      render(true);
    }
  } catch (err) {
    toast(err.message, true);
  } finally {
    btn.disabled = false;
  }
}

// ---------- files ----------
const FILES_REFRESH_MS = 10000;
const KIND_MARK = { rendered: 'R', authored: 'A', state: 'S' };

function fsLookup(node, rel) {
  if (node.rel === rel) return node;
  for (const child of node.children || []) {
    if (rel === child.rel || rel.startsWith(`${child.rel}/`)) {
      const hit = fsLookup(child, rel);
      if (hit) return hit;
    }
  }
  return null;
}

// Children as shown in the tree: a task/project's literal `tasks/` folder is inlined so subtasks nest directly.
function fsChildren(node) {
  const out = [];
  (node.children || []).forEach((c) => {
    if (c.type === 'dir' && c.name === 'tasks' && ['project', 'task'].includes(node.role)) out.push(...(c.children || []));
    else out.push(c);
  });
  const rank = (c) => (c.type === 'file' ? 0 : (c.role === 'task' ? 2 : 1));
  return out.sort((a, b) => rank(a) - rank(b) || a.name.localeCompare(b.name));
}

function fsSize(n) {
  if (n === null || n === undefined) return '';
  if (n < 1024) return `${n} B`;
  if (n < 1048576) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1048576).toFixed(1)} MB`;
}

function kindMark(kind) {
  const m = KIND_MARK[kind];
  return m ? `<span class="kind k-${cls(kind)}" title="${esc(kind)}">${m}</span>` : '<span class="kind"></span>';
}

function fsTreeHtml(node, current, depth = 0) {
  return fsChildren(node).map((c) => {
    const pad = `style="padding-left:${0.3 + depth * 0.9}rem"`;
    const sel = c.rel === current ? ' selected' : '';
    if (c.type === 'file') {
      return `<li><a class="fs-row${sel}" ${pad} href="${filesHref(c.rel)}">${kindMark(c.kind)}
        <span class="fs-name">${esc(c.name)}</span><span class="fs-ago small muted">${ago(c.mtime)}</span></a></li>`;
    }
    const open = ui.fsOpen.has(c.rel);
    const role = c.role !== 'dir' ? `<span class="pill ${c.role === 'project' ? 'st-coordinating' : 'muted'}">${esc(c.role)}</span>` : '';
    return `<li><div class="fs-row dir${sel}" ${pad}>
        <button class="caret" data-fs-toggle="${esc(c.rel)}" aria-label="toggle">${open ? '▾' : '▸'}</button>
        <a class="fs-name" href="${filesHref(c.rel)}">${esc(c.name)}/</a> ${role}</div>
      ${open ? `<ul>${fsTreeHtml(c, current, depth + 1)}</ul>` : ''}</li>`;
  }).join('');
}

function fsCrumbs(rel) {
  const segs = rel ? rel.split('/') : [];
  const out = [ilink(filesHref(''), 'FORGE_HOME')];
  segs.forEach((s, i) => {
    out.push(i === segs.length - 1 ? `<b>${esc(s)}</b>` : ilink(filesHref(segs.slice(0, i + 1).join('/')), s));
  });
  return out.join(' / ');
}

function jsonHtml(value, label, depth) {
  const lbl = label === null ? '' : `<span class="jk">${esc(label)}</span>: `;
  if (value && typeof value === 'object') {
    const entries = Array.isArray(value) ? value.map((v, i) => [i, v]) : Object.entries(value);
    const brace = Array.isArray(value) ? ['[', ']'] : ['{', '}'];
    if (!entries.length) return `<div class="jrow">${lbl}<span class="muted">${brace.join('')}</span></div>`;
    return `<details class="jnode" ${depth < 2 ? 'open' : ''}><summary>${lbl}<span class="muted">${brace[0]} ${entries.length} ${Array.isArray(value) ? 'items' : 'keys'} ${brace[1]}</span></summary>
      ${entries.map(([k, v]) => jsonHtml(v, k, depth + 1)).join('')}</details>`;
  }
  const type = value === null ? 'null' : typeof value;
  return `<div class="jrow">${lbl}<span class="jv j-${type}">${esc(JSON.stringify(value))}</span></div>`;
}

function fsBodyHtml(f) {
  if (!f) return '';
  const isMd = /\.md$/i.test(f.rel);
  const isJson = /\.json$/i.test(f.rel);
  if (ui.fsRaw || !(isMd || isJson)) return `<pre class="fs-raw">${esc(f.content)}</pre>`;
  if (isMd) return mdHtml(f.content);
  try { return `<div class="json-view">${jsonHtml(JSON.parse(f.content), null, 0)}</div>`; } catch (e) {
    return `<div class="callout error small">Invalid JSON: ${esc(e.message)}</div><pre class="fs-raw">${esc(f.content)}</pre>`;
  }
}

function fsHeadHtml(f) {
  const note = f.kind === 'rendered'
    ? `<div class="callout small">Generated from <code>${esc(f.source || 'json')}</code> — edit via the forge CLI, not by hand.</div>` : '';
  const toggle = /\.(md|json)$/i.test(f.rel) ? `<span class="seg">
      <button class="btn tiny ${ui.fsRaw ? '' : 'on'}" data-fs-raw="0">Rendered</button><button class="btn tiny ${ui.fsRaw ? 'on' : ''}" data-fs-raw="1">Raw</button></span>` : '';
  return `<div class="meta"><span>${kindMark(f.kind)} ${esc(f.kind)}</span><span>${fsSize(f.size)}</span>
      <span>modified ${ago(f.mtime)}</span>${toggle}</div>${note}`;
}

function fsDirHtml(node) {
  const kids = fsChildren(node);
  if (!kids.length) return '<p class="muted">Empty folder.</p>';
  return `<table class="grid compact"><thead><tr><th>Name</th><th>Kind</th><th>Size</th><th>Modified</th></tr></thead><tbody>
    ${kids.map((c) => `<tr><td>${c.type === 'file' ? kindMark(c.kind) : '📁'} ${ilink(filesHref(c.rel), c.type === 'dir' ? `${c.name}/` : c.name)}</td>
      <td>${c.type === 'dir' ? esc(c.role) : esc(c.kind)}</td><td class="num">${fsSize(c.size)}</td><td class="nowrap">${ago(c.mtime)}</td></tr>`).join('')}
    </tbody></table>`;
}

// Relative links inside a markdown file open the target inside the Files view.
function fsRewriteLinks(root, rel) {
  const dir = rel.includes('/') ? rel.slice(0, rel.lastIndexOf('/') + 1) : '';
  $$('a[href]', root).forEach((a) => {
    const href = a.getAttribute('href');
    if (!href || /^([a-z][a-z0-9+.-]*:|#|\/\/)/i.test(href)) return;
    try {
      const u = new URL(href, `http://forge.local/${dir}`);
      const target = decodeURIComponent(u.pathname.replace(/^\/+/, '')).replace(/\/+$/, '');
      a.setAttribute('href', filesHref(target) + (u.hash || ''));
      a.removeAttribute('target');
    } catch (e) { /* leave malformed links alone */ }
  });
}

function fsPaintBody(f) {
  const body = $('#fs-body');
  if (!body) return;
  body.innerHTML = fsBodyHtml(f);
  fsRewriteLinks(body, f.rel);
  renderMermaid(body);
}

views.files = {
  async load(r) {
    const rel = r.parts.slice(1).join('/');
    const tree = await api('/api/fs/tree');
    const node = fsLookup(tree, rel);
    if (!node) throw new Error(`not found: ${rel}`);
    const file = node.type === 'file' ? await api(`/api/fs/file${qs({ path: rel })}`) : null;
    const segs = rel.split('/');
    for (let i = 1; i <= segs.length; i += 1) ui.fsOpen.add(segs.slice(0, i).join('/'));
    return { rel, tree, node, file };
  },
  build(d) {
    const right = d.file
      ? `<div id="fs-head">${fsHeadHtml(d.file)}</div><div id="fs-body" class="fs-body"></div>`
      : `${d.rel ? '' : '<p class="small muted">Everything under FORGE_HOME. <b>R</b> rendered (generated from json) · <b>A</b> authored · <b>S</b> state json.</p>'}${fsDirHtml(d.node)}`;
    return `<nav class="crumbs">${fsCrumbs(d.rel)}</nav>
      <div class="files ${ui.fsTreeHidden ? 'tree-hidden' : ''}">
        <aside class="fs-tree"><div class="fs-tree-head small"><b>Files</b>
          <button class="btn tiny" data-fs-hide="1" title="hide tree">⟨</button></div>
          <ul id="fs-tree-list">${fsTreeHtml(d.tree, d.rel)}</ul></aside>
        <section class="fs-view"><h1><button class="btn tiny fs-show" data-fs-hide="0" title="show tree">☰</button>
          <span class="mono">${esc(d.rel.split('/').pop() || 'FORGE_HOME')}</span></h1>${right}</section>
      </div>`;
  },
  after(d) {
    ui.fsFile = d.file;
    ui.fsTreeSig = JSON.stringify(d.tree);
    if (d.file) fsPaintBody(d.file);
    startFilesPoll(d.rel);
  },
};

function stopFilesPoll() {
  if (ui.fsTimer) clearInterval(ui.fsTimer);
  ui.fsTimer = null;
}

function startFilesPoll(rel) {
  stopFilesPoll();
  ui.fsTimer = setInterval(async () => {
    const route = parseRoute();
    if (document.hidden || route.name !== 'files' || route.parts.slice(1).join('/') !== rel) return;
    try {
      const tree = await api('/api/fs/tree');
      const sig = JSON.stringify(tree);
      const list = $('#fs-tree-list');
      if (sig !== ui.fsTreeSig && list) {
        ui.fsTreeSig = sig;
        const scroll = list.parentElement.scrollTop;
        list.innerHTML = fsTreeHtml(tree, rel);
        list.parentElement.scrollTop = scroll;
      }
      if (ui.fsFile) {
        const meta = await api(`/api/fs/file${qs({ path: rel, meta: '1' })}`);
        if (meta.mtime !== ui.fsFile.mtime || meta.size !== ui.fsFile.size) {
          const f = await api(`/api/fs/file${qs({ path: rel })}`);
          ui.fsFile = f;
          const y = window.scrollY;
          const body = $('#fs-body');
          const inner = body ? body.scrollTop : 0;
          const head = $('#fs-head');
          if (head) head.innerHTML = fsHeadHtml(f);
          fsPaintBody(f);
          if (body) body.scrollTop = inner;
          window.scrollTo(0, y);
        }
      }
      $('#updated').textContent = `Updated ${new Date().toLocaleTimeString()}`;
    } catch (err) {
      $('#updated').textContent = `Refresh failed: ${err.message}`;
    }
  }, FILES_REFRESH_MS);
}

// ---------- routing / rendering ----------
function parseRoute() {
  const raw = location.hash.replace(/^#/, '') || '/';
  const [path, query = ''] = raw.split('?');
  let parts;
  try { parts = path.split('/').filter(Boolean).map(decodeURIComponent); } catch (e) { parts = []; }
  const name = parts[0] || 'projects';
  return { raw, name, parts, params: new URLSearchParams(query) };
}

function applyCollapse() {
  $$('tr[data-anc]').forEach((row) => {
    const anc = row.dataset.anc ? row.dataset.anc.split(' ') : [];
    row.hidden = anc.some((k) => ui.collapsed.has(k));
  });
  $$('[data-collapse]').forEach((btn) => { btn.textContent = ui.collapsed.has(btn.dataset.collapse) ? '▸' : '▾'; });
}

function applyExpanded() {
  $$('[data-expand-id]').forEach((row) => { row.hidden = !ui.expanded.has(row.dataset.expandId); });
}

function restoreDrafts() {
  $$('form[data-form="comment"], form[data-form="decide"]').forEach((form) => {
    const draft = ui.drafts[form.dataset.formId];
    if (draft) $('textarea', form).value = draft;
  });
}

async function render(silent = false) {
  const route = parseRoute();
  const view = views[route.name];
  const main = $('#main');
  $$('[data-nav]').forEach((a) => a.classList.toggle('active', a.dataset.nav === route.name
    || (a.dataset.nav === 'projects' && ['project', 'task'].includes(route.name))
    || (a.dataset.nav === 'issues' && route.name === 'issue')
    || (a.dataset.nav === 'agents' && route.name === 'agent')));
  if (route.name !== 'agent') stopAgentPoll();
  if (route.name !== 'files') stopFilesPoll();
  if (!view) { main.innerHTML = '<h1>Not found</h1>'; return; }
  const token = ++ui.token;
  try {
    const data = await view.load(route);
    if (token !== ui.token) return;
    const sig = route.raw + JSON.stringify(data);
    $('#updated').textContent = `Updated ${new Date().toLocaleTimeString()}`;
    if (silent && sig === ui.lastSig) return;
    ui.lastSig = sig;
    const scroll = window.scrollY;
    main.innerHTML = view.build(data, route);
    if (view.after) view.after(data);
    applyExpanded();
    restoreDrafts();
    renderMermaid(main);
    if (silent) window.scrollTo(0, scroll);
  } catch (err) {
    if (token !== ui.token) return;
    if (silent) { $('#updated').textContent = `Refresh failed: ${err.message}`; return; }
    ui.lastSig = null;
    main.innerHTML = `<div class="callout error"><b>Error:</b> ${esc(err.message)}</div>`;
  }
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
  } catch (e) {
    const ta = document.createElement('textarea');
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    document.execCommand('copy');
    ta.remove();
  }
  toast(`Copied: ${text}`);
}

async function doRefresh(btn) {
  btn.disabled = true;
  const label = btn.textContent;
  btn.textContent = 'Refreshing…';
  try {
    const res = await api('/api/refresh', btn.dataset.key ? { key: btn.dataset.key } : {});
    toast(`Refreshed in ${res.seconds}s`);
  } catch (err) {
    toast(`Refresh failed: ${err.message}`, true);
  } finally {
    btn.disabled = false;
    btn.textContent = label;
    ui.lastSig = null;
    render(true);
  }
}

async function submitComment(form) {
  const ta = $('textarea', form);
  const body = ta.value.trim();
  if (!body) return;
  const btn = $('button[type="submit"]', form);
  btn.disabled = true;
  try {
    const payload = { key: form.dataset.key, body };
    if (form.dataset.issue) payload.issue_id = form.dataset.issue;
    const msg = await api('/api/inbox', payload);
    delete ui.drafts[form.dataset.formId];
    ta.value = '';
    const nudge = msg.nudge || {};
    toast(`Sent ${msg.id}${nudge.reason ? ` — ${nudge.reason}` : ''}`);
    ui.lastSig = null;
    await render(true);
  } catch (err) {
    toast(`Send failed: ${err.message}`, true);
  } finally {
    btn.disabled = false;
  }
}

document.addEventListener('click', (ev) => {
  const scroll = ev.target.closest('[data-scroll]');
  if (scroll) {
    ev.preventDefault();
    const el = document.getElementById(scroll.dataset.scroll);
    if (el) { el.scrollIntoView({ behavior: 'smooth', block: 'center' }); el.classList.add('flash'); setTimeout(() => el.classList.remove('flash'), 1500); }
    return;
  }
  const copy = ev.target.closest('[data-copy]');
  if (copy) { ev.preventDefault(); copyText(copy.dataset.copy); return; }
  const collapse = ev.target.closest('[data-collapse]');
  if (collapse) {
    const key = collapse.dataset.collapse;
    if (ui.collapsed.has(key)) ui.collapsed.delete(key); else ui.collapsed.add(key);
    applyCollapse();
    return;
  }
  const expand = ev.target.closest('[data-expand]');
  if (expand) {
    const id = expand.dataset.expand;
    if (ui.expanded.has(id)) ui.expanded.delete(id); else ui.expanded.add(id);
    applyExpanded();
    return;
  }
  const action = ev.target.closest('[data-action="refresh"]');
  if (action) { doRefresh(action); return; }
  const agentBtn = ev.target.closest('[data-action="agent-open"], [data-action="agent-dispatch"]');
  if (agentBtn) { agentAction(agentBtn); return; }
  const fsToggle = ev.target.closest('[data-fs-toggle]');
  if (fsToggle) {
    const rel = fsToggle.dataset.fsToggle;
    if (ui.fsOpen.has(rel)) ui.fsOpen.delete(rel); else ui.fsOpen.add(rel);
    ui.lastSig = null;
    const list = $('#fs-tree-list');
    const scroll = list ? list.parentElement.scrollTop : 0;
    render(true).then(() => { const l = $('#fs-tree-list'); if (l) l.parentElement.scrollTop = scroll; });
    return;
  }
  const fsRaw = ev.target.closest('[data-fs-raw]');
  if (fsRaw) {
    ui.fsRaw = fsRaw.dataset.fsRaw === '1';
    $$('[data-fs-raw]').forEach((b) => b.classList.toggle('on', b.dataset.fsRaw === fsRaw.dataset.fsRaw));
    if (ui.fsFile) fsPaintBody(ui.fsFile);
    return;
  }
  const fsHide = ev.target.closest('[data-fs-hide]');
  if (fsHide) {
    ui.fsTreeHidden = fsHide.dataset.fsHide === '1';
    const box = $('.files');
    if (box) box.classList.toggle('tree-hidden', ui.fsTreeHidden);
  }
});

document.addEventListener('change', (ev) => {
  const sel = ev.target.closest('select[data-filter]');
  if (!sel) return;
  const route = parseRoute();
  const params = new URLSearchParams(route.params);
  if (sel.value) params.set(sel.dataset.filter, sel.value); else params.delete(sel.dataset.filter);
  const q = params.toString();
  location.hash = `#/${route.name}${q ? `?${q}` : ''}`;
});

document.addEventListener('input', (ev) => {
  const form = ev.target.closest('form[data-form="comment"], form[data-form="decide"]');
  if (form) ui.drafts[form.dataset.formId] = ev.target.value;
  const agentForm = ev.target.closest('form[data-form="agent-msg"]');
  if (agentForm) ui.drafts[`agent:${agentForm.dataset.key}`] = ev.target.value;
});

document.addEventListener('toggle', (ev) => {
  const d = ev.target;
  if (!d.dataset || !d.dataset.entry) return;
  if (d.open) ui.expanded.add(d.dataset.entry); else ui.expanded.delete(d.dataset.entry);
}, true);

document.addEventListener('submit', (ev) => {
  const agentForm = ev.target.closest('form[data-form="agent-msg"]');
  if (agentForm) { ev.preventDefault(); sendAgentMessage(agentForm); return; }
  const issueForm = ev.target.closest('form[data-form="decide"], form[data-form="issue-state"]');
  if (issueForm) { ev.preventDefault(); submitIssueForm(issueForm); return; }
  const stateForm = ev.target.closest('form[data-form="task-state"]');
  if (stateForm) { ev.preventDefault(); submitTaskState(stateForm); return; }
  const form = ev.target.closest('form[data-form="comment"]');
  if (!form) return;
  ev.preventDefault();
  submitComment(form);
});

document.addEventListener('keydown', (ev) => {
  if ((ev.metaKey || ev.ctrlKey) && ev.key === 'Enter') {
    const form = ev.target.closest && ev.target.closest('form[data-form="comment"]');
    if (form) { ev.preventDefault(); submitComment(form); }
    const agentForm = ev.target.closest && ev.target.closest('form[data-form="agent-msg"]');
    if (agentForm) { ev.preventDefault(); sendAgentMessage(agentForm); }
  }
});

window.addEventListener('hashchange', () => { ui.lastSig = null; window.scrollTo(0, 0); render(); });
setInterval(() => {
  const active = document.activeElement;
  const typing = active && ['TEXTAREA', 'INPUT', 'SELECT'].includes(active.tagName);
  if (!document.hidden && !typing && !['agent', 'files'].includes(parseRoute().name)) render(true);
}, AUTO_REFRESH_MS);
render();
