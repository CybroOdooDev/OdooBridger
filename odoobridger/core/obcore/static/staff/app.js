/* The OdooBridger staff console: accounts, payments, leads, the catalog. */

import '/assets/password-toggle.js';
import { fmtDate, money } from '/assets/api.js';
import { $, $$, banner, empty, esc, field, fmtAgo, busy, guard, kpi, kpiIcon, loading, readForm, toast, wireTips } from '/assets/ui.js';
import { authCard, dialog, match, mountShell, parseHash, submitting, unmountShell } from '/assets/shell.js';
import { client } from './client.js';
import { renderLeads } from './leads.js';

const { api, session, restore, signOut } = client;
const state = { me: null, services: null };

const STATUS_TONE = { trialing: 'trialing', active: 'ok', past_due: 'bad', suspended: 'bad', cancelled: 'mute', closed: 'mute' };
const STATUS_LABEL = { trialing: 'Trial', active: 'Active', past_due: 'Past Due', suspended: 'Suspended', cancelled: 'Cancelled', closed: 'Closed' };
export const statusPill = (s) => `<span class="pill ${STATUS_TONE[s] || 'mute'}">${esc(STATUS_LABEL[s] || s)}</span>`;

async function services() {
  if (!state.services) state.services = await api.get('/staff/services');
  return state.services;
}

/* =========================================================================
 * Sign in
 * ====================================================================== */
function renderLogin() {
  const root = authCard(`
    <h1>Staff Sign In</h1>
    <p class="sub">For OdooBridger staff only.</p>
    <form id="form">
      ${field({ name: 'email', label: 'Email', type: 'email', required: true })}
      ${field({ name: 'password', label: 'Password', type: 'password', required: true })}
      <button class="primary" style="width:100%" id="go">Sign In To Console</button>
    </form>`, { badge: 'staff console', foot: '<a href="/app/">Customer sign-in</a>' });
  $('#form', root).addEventListener('submit', (e) => {
    e.preventDefault();
    submitting($('#go', root), async () => {
      session.persist(await api.post('/auth/staff/login', readForm(e.target)));
      window.location.hash = '#/overview';
      route();
    });
  });
}

/* =========================================================================
 * Overview
 * ====================================================================== */
const SVC_STATUS = {
  operational: ['ok', 'Operational'], degraded: ['warn', 'Degraded'], unavailable: ['bad', 'Unavailable'],
  maintenance: ['warn', 'Maintenance'], disabled: ['mute', 'Disabled'], not_deployed: ['mute', 'Not Deployed'],
};
const SEVERITY = { critical: ['bad', 'Critical'], high: ['bad', 'High'], medium: ['warn', 'Medium'] };
const SUB_LABEL = { paying: ['ok', 'Paying'], trial: ['trialing', 'Trial'], past_due: ['bad', 'Past Due'], suspended: ['bad', 'Suspended'] };
const MIX = [['paying', 'Paying', 'var(--ok)'], ['trial', 'Free trial', 'var(--accent)'],
  ['past_due', 'Past due', 'var(--warn)'], ['suspended', 'Suspended', 'var(--bad)'], ['no_service', 'No service', 'var(--faint)']];

const tag = (tone, text) => `<span class="pill ${tone}">${esc(text)}</span>`;
const badge = (tone, text) => `<span class="pill ${tone} kpi-b">${esc(text)}</span>`;
const signed = (n) => (n > 0 ? `+${n}` : `${n}`);

/** "3 h", "2 days": how long a problem has been going on. */
function since(iso) {
  if (!iso) return '';
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 3600) return `${Math.max(1, Math.round(s / 60))} min`;
  if (s < 86400) { const h = Math.floor(s / 3600); const m = Math.round((s % 3600) / 60); return m ? `${h} h ${m} min` : `${h} h`; }
  const d = Math.floor(s / 86400);
  return `${d} day${d === 1 ? '' : 's'}`;
}

/** Total customers at each month's end: one line, a point per month, a tooltip on hover. */
function growthChart(points) {
  const W = 340; const H = 270; const L = 32; const B = 26; const T = 14; const R = 12;
  const max = Math.max(4, ...points.map((p) => p.total));
  const step = Math.ceil(max / 4);
  const top = step * 4;
  const x = (i) => L + 8 + (i * (W - L - R - 8)) / Math.max(1, points.length - 1);
  const y = (v) => H - B - ((H - B - T) * v) / top;
  const month = (m) => new Date(`${m}-01T00:00:00`).toLocaleDateString(undefined, { month: 'short' });
  const line = points.map((p, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(p.total).toFixed(1)}`).join(' ');
  const area = `${line} L${x(points.length - 1).toFixed(1)},${H - B} L${x(0).toFixed(1)},${H - B} Z`;
  const grid = [0, 1, 2, 3, 4].map((g) => `<line x1="${L}" x2="${W - R}" y1="${y(g * step)}" y2="${y(g * step)}" class="ch-grid"/>
    <text x="${L - 8}" y="${y(g * step) + 4}" text-anchor="end" class="ch-ax">${g * step}</text>`).join('');
  const labels = points.map((p, i) => (i % 2 === 1 ? `<text x="${x(i)}" y="${H - 7}" text-anchor="middle" class="ch-ax">${esc(month(p.month))}</text>` : '')).join('');
  const dots = points.map((p, i) => `<circle cx="${x(i)}" cy="${y(p.total)}" r="4" class="ch-dot"/>`).join('');
  const hits = points.map((p, i) => `<rect x="${x(i) - (W - L) / points.length / 2}" y="${T}" width="${(W - L) / points.length}" height="${H - B - T}"
    fill="transparent" data-i="${i}"/>`).join('');
  return `<div class="ch-wrap"><svg viewBox="0 0 ${W} ${H}" width="100%" role="img" aria-label="Total customers, last 12 months">
    <defs><linearGradient id="chFill" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="var(--accent)" stop-opacity=".18"/><stop offset="1" stop-color="var(--accent)" stop-opacity="0"/></linearGradient></defs>
    ${grid}<path d="${area}" fill="url(#chFill)"/><path d="${line}" fill="none" stroke="var(--accent)" stroke-width="2"/>
    <line class="ch-cross" y1="${T}" y2="${H - B}" x1="0" x2="0" hidden/>${dots}${labels}${hits}</svg>
    <div class="ch-tip" hidden></div></div>`;
}

function wireGrowth(root, points) {
  const wrap = $('.ch-wrap', root);
  if (!wrap) return;
  const tip = $('.ch-tip', wrap);
  const cross = $('.ch-cross', wrap);
  const month = (m) => new Date(`${m}-01T00:00:00`).toLocaleDateString(undefined, { month: 'long', year: 'numeric' });
  wrap.addEventListener('mousemove', (e) => {
    const hit = e.target.closest('[data-i]');
    if (!hit) return;
    const p = points[Number(hit.dataset.i)];
    const dot = wrap.querySelectorAll('.ch-dot')[Number(hit.dataset.i)];
    const cx = Number(dot.getAttribute('cx'));
    cross.setAttribute('x1', cx); cross.setAttribute('x2', cx); cross.hidden = false;
    tip.innerHTML = `<b>${esc(month(p.month))}</b><span>${p.total} customers</span><span>${signed(p.added)} that month</span>`;
    tip.hidden = false;
    const box = wrap.getBoundingClientRect();
    const dbox = dot.getBoundingClientRect();
    const left = Math.min(box.width - tip.offsetWidth, Math.max(0, dbox.left - box.left - tip.offsetWidth / 2));
    tip.style.left = `${left}px`;
    tip.style.top = `${Math.max(0, dbox.top - box.top - tip.offsetHeight - 10)}px`;
  });
  wrap.addEventListener('mouseleave', () => { tip.hidden = true; cross.hidden = true; });
}

const incidentRow = (i) => `<li${i.account_id ? ` class="click" data-account="${esc(i.account_id)}"` : i.href ? ` class="click" data-href="${esc(i.href)}"` : ''}>
  <span class="al-ic ${SEVERITY[i.severity][0]}">${kpiIcon('alert')}</span>
  <div class="al-t"><b>${esc(i.issue)}</b>${i.detail ? `<p class="al-d" title="${esc(i.detail)}">${esc(i.detail)}</p>` : ''}<span>${esc([i.account, i.service].filter(Boolean).join(' · '))}</span></div>
  <div class="al-r">${tag(...SEVERITY[i.severity])}<span>${esc(since(i.since))}</span></div></li>`;

async function renderOverview(mount) {
  const o = await api.get('/staff/overview');
  const k = o.kpis;
  // Platform-level notices sit with the rest, not in a banner across the page.
  const incidents = [...(!o.stripe.enabled ? [{ key: 'stripe', severity: 'medium', issue: 'Online payments are off',
    detail: 'Add Stripe keys so customers can pay.', service: 'Payments', href: '#/payments' }] : []), ...o.incidents];
  const mixTotal = MIX.reduce((n, [key]) => n + (o.mix[key] || 0), 0);
  const mixShown = MIX.filter(([key]) => o.mix[key] || key !== 'no_service');
  const svcShort = o.services.map((sv) => {
    const [tone, label] = SVC_STATUS[sv.status] || ['mute', sv.status];
    const sub = sv.integrations
      ? `${sv.integrations.active} integrations · ${sv.jobs?.pending ?? 0} queued`
      : sv.status === 'unavailable' ? (sv.error || 'not answering')
        : sv.status === 'not_deployed' || sv.status === 'disabled' ? `${sv.customers} customers` : `${sv.customers} customers · no metrics yet`;
    const failed = sv.integrations ? `${sv.integrations.failed} failed` : '—';
    return `<li><div><b>${esc(sv.name)}</b><small>${esc(sub)}</small></div>${tag(tone, label)}<span class="num">${esc(failed)}</span></li>`;
  }).join('');
  mount.innerHTML = `
    <div class="kpis">
      ${kpi({ icon: 'users', tone: 'acc', label: 'Customers', value: k.customers.value, href: '#/accounts' })}
      ${kpi({ icon: 'card', tone: 'acc', label: 'Subscriptions', value: k.subscriptions.value, href: '#/accounts' })}
      ${kpi({ icon: 'money', tone: 'ok', label: 'Monthly Revenue', value: money(k.mrr.value_cents), href: '#/payments' })}
      ${kpi({ icon: 'link', tone: k.integrations.failed ? 'bad' : 'acc', label: 'Integrations', value: k.integrations.value })}
      ${kpi({ icon: 'server', tone: k.services.not_operational.length ? 'warn' : 'ok', label: 'Services',
        value: `${k.services.operational} / ${k.services.deployed}` })}
      ${kpi({ icon: 'alert', tone: incidents.length ? 'bad' : 'ok', label: 'Needs Attention', value: incidents.length })}
    </div>
    <div class="ov-row2">
      <div class="card"><div class="card-head"><h2>Customers</h2><span class="hint">total · last 12 months</span></div>
        <div class="cust-split"><div class="cust-chart">${growthChart(o.growth)}</div>
          <div class="cust-mix">
        ${mixTotal ? `<div class="mix" role="img" aria-label="Customer mix">${mixShown.filter(([key]) => o.mix[key]).map(([key, label, color]) =>
          `<span style="flex:${o.mix[key]};background:${color}" title="${esc(label)}: ${o.mix[key]}"></span>`).join('')}</div>
        <ul class="mix-leg">${mixShown.map(([key, label, color]) => `<li><span class="sw" style="background:${color}"></span>${esc(label)}
          <b>${o.mix[key] || 0}</b><em>${Math.round(((o.mix[key] || 0) * 100) / mixTotal)}%</em></li>`).join('')}</ul>` : empty('No Customers Yet', '')}
        <div class="facts2">
          <div>Email not verified<b>${o.customer_facts.unverified}</b></div>
          <div>Trials ending 7 d<b>${o.customer_facts.trials_ending_7d}</b></div>
          <div>With failures<b>${o.customer_facts.with_failures}</b></div>
          <div><a href="#/leads">Open leads</a><b>${o.customer_facts.open_leads}</b></div></div></div></div></div>
      <div class="card"><div class="card-head"><h2>Needs Attention</h2>${incidents.length > 5 ? '<a href="#" id="allIncidents">View All →</a>' : ''}</div>
        ${incidents.length ? `<ul class="alerts">${incidents.slice(0, 5).map(incidentRow).join('')}</ul>`
          : empty('Nothing Needs Attention', '')}</div>
    </div>
    <div class="ov-row2">
      <div class="card"><div class="card-head"><h2>Recent Customers</h2><a href="#/accounts">Accounts →</a></div>
        ${o.recent_customers.length ? `<div class="table-wrap"><table>
          <thead><tr><th>Customer</th><th>Subscription</th><th>Services</th><th>Status</th><th>Registered</th><th>Last active</th></tr></thead>
          <tbody>${o.recent_customers.map((c) => `<tr${c.id ? ` class="click" data-account="${esc(c.id)}"` : ''}>
            <td>${esc(c.name)}</td>
            <td>${c.subscription ? tag(...SUB_LABEL[c.subscription]) : '—'}</td>
            <td>${esc(c.services.join(', ') || '—')}</td>
            <td>${c.status === 'unverified' ? tag('mute', 'Email Not Verified') : statusPill(c.status)}</td>
            <td class="nowrap">${esc(fmtDate(c.created_at))}</td>
            <td class="nowrap">${c.last_active_at ? esc(fmtAgo(c.last_active_at)) : '—'}</td></tr>`).join('')}</tbody></table></div>`
          : empty('No Customers Yet', '')}</div>
      <div class="card"><div class="card-head"><h2>Service Health</h2><a href="#/catalog">Services →</a></div>
        ${o.services.length ? `<ul class="svc-list">${svcShort}</ul>` : empty('No Services Yet', '')}
        <div class="hint" style="margin-top:8px">Checked ${esc(fmtAgo(o.services[0]?.checked_at || o.checked_at))}</div></div>
    </div>`;
  wireGrowth(mount, o.growth);
  mount.onclick = (e) => {
    if (e.target.closest('#allIncidents')) {
      e.preventDefault();
      dialog({ title: 'Needs Attention', wide: true, submitLabel: 'Done',
        body: `<ul class="alerts">${incidents.map(incidentRow).join('')}</ul>` });
      return;
    }
    const go = e.target.closest('[data-href]');
    if (go) { document.querySelector('.wiz-backdrop')?.click(); window.location.hash = go.dataset.href; return; }
    const row = e.target.closest('[data-account]');
    if (row) {
      document.querySelector('.wiz-backdrop')?.click();
      window.location.hash = `#/accounts/${row.dataset.account}`;
    }
  };
}

/* =========================================================================
 * One service's dashboard: only the generic figures the service reports
 * ====================================================================== */
const HEALTH = { healthy: ['ok', 'Healthy'], delayed: ['warn', 'Delayed'], failing: ['bad', 'Failing'],
  not_started: ['mute', 'Not Started'] };
const minutes = (n) => (n == null ? '—' : n < 60 ? `${n} min` : n % 60 ? `${Math.floor(n / 60)} h ${n % 60} min` : `${n / 60} h`);

function cell(kind, v) {
  if (kind === 'usage') {
    if (!v || v.used == null) return '—';
    const full = v.limit != null && v.used >= v.limit;
    const text = `${Number(v.used).toLocaleString()} / ${v.limit == null ? '∞' : Number(v.limit).toLocaleString()}`;
    return full ? `<span class="t-warn" title="At the plan's limit">${esc(text)}</span>` : esc(text);
  }
  if (kind === 'minutes') return esc(minutes(v));
  if (kind === 'ago') return v ? esc(fmtAgo(v)) : '—';
  if (kind === 'health') { const [t, l] = HEALTH[v] || ['mute', v || '—']; return tag(t, l); }
  if (typeof v === 'number') return esc(v.toLocaleString());
  return esc(v ?? '—');
}

/** One series of daily counts: bars, a tooltip on hover. */
function dailyBars(series) {
  const pts = series.points || [];
  const W = 760; const H = 230; const L = 44; const B = 26; const T = 12; const R = 6;
  const max = Math.max(4, ...pts.map((p) => p.value));
  const mag = 10 ** Math.floor(Math.log10(max / 4));
  const step = Math.ceil(max / 4 / mag) * mag;
  const top = step * 4;
  const bw = (W - L - R) / Math.max(1, pts.length);
  const y = (v) => H - B - ((H - B - T) * v) / top;
  const short = (n) => (n >= 1000 ? `${+(n / 1000).toFixed(1)}k` : `${n}`);
  const day = (d) => new Date(`${d}T00:00:00`).toLocaleDateString(undefined, { day: 'numeric', month: 'short' });
  const grid = [0, 1, 2, 3, 4].map((g) => `<line x1="${L}" x2="${W - R}" y1="${y(g * step)}" y2="${y(g * step)}" class="ch-grid"/>
    <text x="${L - 8}" y="${y(g * step) + 4}" text-anchor="end" class="ch-ax">${short(g * step)}</text>`).join('');
  const bars = pts.map((p, i) => {
    const x = L + i * bw + 2; const w = Math.max(2, bw - 4); const top2 = y(p.value);
    const h = H - B - top2; const r = Math.min(3, w / 2, h);
    return h > 0 ? `<path d="M${x},${H - B} V${top2 + r} q0,-${r} ${r},-${r} h${w - 2 * r} q${r},0 ${r},${r} V${H - B} Z" class="ch-bar"/>` : '';
  }).join('');
  const last = pts.length - 1;
  const labels = pts.map((p, i) => ((i % 7 === 0 && last - i >= 4) || i === last
    ? `<text x="${L + i * bw + bw / 2}" y="${H - 7}" text-anchor="middle" class="ch-ax">${esc(day(p.date))}</text>` : '')).join('');
  const hits = pts.map((p, i) => `<rect x="${L + i * bw}" y="${T}" width="${bw}" height="${H - B - T}" fill="transparent" data-i="${i}"/>`).join('');
  return `<div class="ch-wrap" data-series><svg viewBox="0 0 ${W} ${H}" width="100%" role="img" aria-label="${esc(series.title || 'Daily figures')}">
    ${grid}${bars}${labels}${hits}</svg><div class="ch-tip" hidden></div></div>`;
}

function wireBars(root, series) {
  const wrap = $('[data-series]', root);
  if (!wrap) return;
  const tip = $('.ch-tip', wrap);
  const pts = series.points || [];
  const day = (d) => new Date(`${d}T00:00:00`).toLocaleDateString(undefined, { weekday: 'short', day: 'numeric', month: 'short' });
  wrap.addEventListener('mousemove', (e) => {
    const hit = e.target.closest('[data-i]');
    if (!hit) return;
    const p = pts[Number(hit.dataset.i)];
    tip.innerHTML = `<b>${esc(day(p.date))}</b><span>${Number(p.value).toLocaleString()} ${esc(series.unit || '')}</span>`;
    tip.hidden = false;
    const box = wrap.getBoundingClientRect(); const hb = hit.getBoundingClientRect();
    tip.style.left = `${Math.min(box.width - tip.offsetWidth, Math.max(0, hb.left - box.left + hb.width / 2 - tip.offsetWidth / 2))}px`;
    tip.style.top = '0px';
  });
  wrap.addEventListener('mouseleave', () => { tip.hidden = true; });
}

async function renderServiceDashboard(mount, route, { slug }) {
  const d = await api.get(`/staff/services/${encodeURIComponent(slug)}/dashboard`);
  $('#pageTitle').textContent = d.service.name;
  const sv = d.service;
  const [tone, label] = SVC_STATUS[sv.status] || ['mute', sv.status || 'Unknown'];
  const facts = [sv.version ? `Version ${sv.version}` : null,
    sv.scheduler?.last_run_at ? `scheduler ran ${fmtAgo(sv.scheduler.last_run_at)}` : null,
    sv.checked_at ? `checked ${fmtAgo(sv.checked_at)}` : null].filter(Boolean).join(' · ');
  const cols = d.columns;
  const rowHtml = (c) => `<tr class="click" data-account="${esc(c.account_id)}" data-name="${esc(c.name.toLowerCase())}">
    <td><b>${esc(c.name)}</b></td><td>${esc(c.plan || '—')}</td><td>${statusPill(c.status)}</td>
    ${cols.map((col) => `<td class="${col.kind === 'usage' || col.kind === 'number' ? 'nowrap' : ''}">${cell(col.kind, c.values[col.key])}</td>`).join('')}</tr>`;
  mount.innerHTML = `
    <div class="svc-head-row">${tag(tone, label)}<span class="muted">${esc(facts)}</span></div>
    ${sv.status === 'unavailable' ? banner('The Service Is Not Answering', sv.error || 'Its figures will show once it answers again.', 'bad') : ''}
    ${!sv.has_dashboard && sv.status !== 'unavailable' ? banner('No Figures From This Service Yet', 'It does not report a dashboard; only its customers are listed.', 'warn') : ''}
    <div class="kpis" style="grid-template-columns:repeat(${Math.min(5, Math.max(1, d.tiles.length))},minmax(0,1fr))">
      ${d.tiles.map((t) => kpi({ icon: t.icon || 'server', tone: t.tone || 'acc', label: t.label,
        value: typeof t.value === 'number' ? t.value.toLocaleString() : (t.value ?? '—') })).join('')}
    </div>
    ${d.series || d.attention.length ? `<div class="ov-row2">
      ${d.series ? `<div class="card"><div class="card-head"><h2>${esc(d.series.title || 'Activity')}</h2>${d.series.subtitle ? `<span class="hint">${esc(d.series.subtitle)}</span>` : ''}</div>${dailyBars(d.series)}</div>` : ''}
      <div class="card"><div class="card-head"><h2>Needs Attention</h2></div>
        ${d.attention.length ? `<ul class="alerts">${d.attention.map((i) => incidentRow({ ...i, issue: i.title, service: null,
          severity: SEVERITY[i.severity] ? i.severity : 'medium' })).join('')}</ul>` : empty('Nothing Needs Attention', '')}</div>
    </div>` : ''}
    <div class="card" style="margin-top:14px"><div class="card-head"><h2>Customers</h2>
      <input type="search" id="svcSearch" placeholder="Search customers" style="max-width:260px"></div>
      ${d.customers.length ? `<div class="table-wrap"><table>
        <thead><tr><th>Customer</th><th>Plan</th><th>Status</th>${cols.map((c) => `<th>${esc(c.label)}</th>`).join('')}</tr></thead>
        <tbody>${d.customers.map(rowHtml).join('')}</tbody></table></div>` : empty('No Customers Yet', '')}</div>`;
  if (d.series) wireBars(mount, d.series);
  $('#svcSearch', mount)?.addEventListener('input', (e) => {
    const q = e.target.value.trim().toLowerCase();
    $$('tr[data-name]', mount).forEach((tr) => { tr.hidden = !!q && !tr.dataset.name.includes(q); });
  });
  mount.onclick = (e) => {
    const row = e.target.closest('[data-account]');
    if (row) window.location.hash = `#/accounts/${row.dataset.account}`;
  };
}

/* =========================================================================
 * Accounts
 * ====================================================================== */
const filters = { q: '', status: '', service: '' };

async function renderAccounts(mount) {
  const svcs = await services();
  const qs = new URLSearchParams(Object.entries({ q: filters.q, status_filter: filters.status, service: filters.service })
    .filter(([, v]) => v)).toString();
  const rows = await api.get(`/staff/accounts${qs ? `?${qs}` : ''}`);
  mount.innerHTML = `
    <div class="filterbar">
      <label class="fb-search"><svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>
        <input id="q" type="search" placeholder="Search company or email" value="${esc(filters.q)}" aria-label="Search accounts"></label>
      <select id="status" aria-label="Status"><option value="">All statuses</option>${['active', 'suspended', 'closed'].map((s) =>
        `<option value="${s}" ${filters.status === s ? 'selected' : ''}>${STATUS_LABEL[s]}</option>`).join('')}</select>
      <select id="service" aria-label="Service"><option value="">All services</option>${svcs.map((s) =>
        `<option value="${esc(s.slug)}" ${filters.service === s.slug ? 'selected' : ''}>${esc(s.name)}</option>`).join('')}</select>
      <span class="fb-spacer"></span>
      <button class="primary" id="new">New Account</button>
    </div>
    <div class="card"><div class="table-wrap"><table>
      <thead><tr><th>Account</th><th>Owner</th><th>Services</th><th>Status</th><th class="num">Per Month</th><th>Since</th></tr></thead>
      <tbody>${rows.map((a) => `<tr class="click" data-id="${esc(a.id)}">
        <td><strong>${esc(a.name)}</strong></td><td>${esc(a.owner_email || '—')}</td>
        <td>${a.services.filter((s) => s.status !== 'cancelled').map((s) => `${esc(s.service.name)} ${statusPill(s.status)}`).join('<br>') || '<span class="muted">—</span>'}</td>
        <td>${statusPill(a.status)}${a.pays_online ? ' <span class="pill">Stripe</span>' : ''}</td>
        <td class="num">${money(a.monthly_total_cents)}</td><td class="nowrap">${esc(fmtDate(a.created_at))}</td></tr>`).join('')
        || `<tr><td colspan="6">${empty('No Accounts Match', '')}</td></tr>`}</tbody>
    </table></div></div>`;
  let timer;
  $('#q', mount).addEventListener('input', (e) => {
    clearTimeout(timer);
    timer = setTimeout(() => { filters.q = e.target.value; renderAccounts(mount).then(() => { const q = $('#q', mount); q.focus(); q.setSelectionRange(q.value.length, q.value.length); }); }, 300);
  });
  $('#status', mount).addEventListener('change', (e) => { filters.status = e.target.value; renderAccounts(mount); });
  $('#service', mount).addEventListener('change', (e) => { filters.service = e.target.value; renderAccounts(mount); });
  $$('tr[data-id]', mount).forEach((tr) => tr.addEventListener('click', () => { window.location.hash = `#/accounts/${tr.dataset.id}`; }));
  $('#new', mount).addEventListener('click', () => newAccountDialog());
}

function packageOptions(service, current) {
  return service.packages.filter((p) => p.is_active || p.id === current)
    .map((p) => ({ value: p.id, label: `${p.name} — ${money(p.monthly_price_cents, p.currency)}/mo` }));
}

export async function newAccountDialog(prefill = {}) {
  const svcs = (await services()).filter((s) => s.is_active);
  const result = await dialog({
    title: 'New Account', submitLabel: 'Create Account', wide: true,
    body: `
      <div class="grid cols-2">
        ${field({ name: 'company', label: 'Company', required: true, value: prefill.company || '' })}
        ${field({ name: 'country', label: 'Country', value: prefill.country || '' })}
        ${field({ name: 'owner_name', label: 'Owner name', value: prefill.name || '' })}
        ${field({ name: 'owner_email', label: 'Owner email', type: 'email', required: true, value: prefill.email || '' })}
      </div>
      <div class="section-title">Services</div>
      ${svcs.map((s) => `<div class="row" style="gap:10px;align-items:center;margin-bottom:8px">
        <label style="margin:0;min-width:180px"><input type="checkbox" data-svc="${esc(s.slug)}"> ${esc(s.name)}</label>
        <select data-pkg="${esc(s.slug)}">${packageOptions(s).map((o) => `<option value="${esc(o.value)}">${esc(o.label)}</option>`).join('')}</select>
        <input type="number" min="0" max="365" data-trial="${esc(s.slug)}" placeholder="Trial days" style="max-width:120px">
      </div>`).join('')}
      <label style="margin-top:10px"><input type="checkbox" name="send_invite" checked> Email the owner a link to set their password</label>`,
    onSubmit: (form) => {
      const values = readForm(form);
      const picked = $$('[data-svc]', form).filter((c) => c.checked).map((c) => {
        const slug = c.dataset.svc;
        const trial = $(`[data-trial="${slug}"]`, form).value;
        return { service: slug, package_id: $(`[data-pkg="${slug}"]`, form).value, trial_days: trial === '' ? null : Number(trial) };
      });
      return api.post('/staff/accounts', {
        company: values.company, country: values.country || null, owner_name: values.owner_name || null,
        owner_email: values.owner_email, send_invite: values.send_invite, services: picked,
      });
    },
  });
  if (result?.id) { toast('Account created.', 'ok'); window.location.hash = `#/accounts/${result.id}`; }
}

async function renderAccount(mount, route, { id }) {
  const a = await api.get(`/staff/accounts/${id}`);
  const svcs = await services();
  const missing = svcs.filter((s) => !a.services.some((r) => r.service.slug === s.slug && r.status !== 'cancelled'));
  $('#pageTitle').textContent = a.name;
  mount.innerHTML = `
    <p><a href="#/accounts">&larr; All accounts</a></p>
    ${a.status === 'suspended' ? banner('Suspended', `${a.suspension_reason || 'No reason recorded'} · since ${fmtDate(a.suspended_at)}`, 'bad') : ''}
    <div class="grid cols-2">
      <div class="card">
        <div class="card-head"><h2>Account</h2><div class="actions">
          <button class="sm" id="edit">Edit</button>
          ${a.status === 'suspended' ? '<button class="sm" id="reactivate">Reactivate</button>' : '<button class="sm danger-outline" id="suspend">Suspend</button>'}
        </div></div>
        <dl class="kv">
          <dt>Status</dt><dd>${statusPill(a.status)}</dd>
          <dt>Country</dt><dd>${esc(a.country || '—')}</dd>
          <dt>Timezone</dt><dd>${esc(a.timezone)}</dd>
          <dt>Created</dt><dd>${esc(fmtDate(a.created_at))}</dd>
          ${a.lead_id ? `<dt>From lead</dt><dd><a href="#/leads?open=${esc(a.lead_id)}">Open lead</a></dd>` : ''}
          <dt>Account id</dt><dd class="mono">${esc(a.id)}</dd>
        </dl>
      </div>
      <div class="card">
        <div class="card-head"><h2>Billing</h2></div>
        <dl class="kv">
          <dt>Pays online</dt><dd>${a.pays_online ? `Yes · ${esc(a.stripe_status || '')}` : 'No'}</dd>
          <dt>Interval</dt><dd>${a.billing_interval === 'year' ? 'Yearly' : 'Monthly'}</dd>
          <dt>Per month</dt><dd>${money(a.monthly_total_cents)}</dd>
          <dt>Renews</dt><dd>${esc(fmtDate(a.renews_at))}${a.cancel_at_period_end ? ' <span class="pill warn">Cancels then</span>' : ''}</dd>
          <dt>Stripe customer</dt><dd class="mono">${esc(a.stripe_customer_id || '—')}</dd>
        </dl>
      </div>
    </div>
    <div class="card" style="margin-top:14px">
      <div class="card-head"><h2>Services</h2><div class="actions">${missing.length ? '<button class="sm primary" id="addSvc">Add Service</button>' : ''}</div></div>
      <div class="table-wrap"><table>
        <thead><tr><th>Service</th><th>Package</th><th>Status</th><th>Trial ends</th><th>Limits</th><th></th></tr></thead>
        <tbody>${a.services.map((r) => `<tr>
          <td><strong>${esc(r.service.name)}</strong></td>
          <td>${esc(r.package?.name || '—')}${r.billed ? ' <span class="pill">billed</span>' : ''}</td>
          <td>${statusPill(r.status)}</td>
          <td>${esc(r.trial_ends_at ? fmtDate(r.trial_ends_at) : '—')}</td>
          <td class="muted">${esc(limitsText(r.entitlements))}
            ${r.entitlement_overrides ? '<span class="pill warn">custom</span>' : ''}</td>
          <td class="right nowrap">
            ${r.in_sync ? '' : `<span class="pill warn" title="${esc(r.provision_error || 'The service has not confirmed the latest change yet.')}">Not Synced</span>`}
            <button class="sm" data-manage="${esc(r.service.slug)}">Manage</button>
            ${r.in_sync ? `<a class="btn sm" href="#/service/${esc(r.service.slug)}">Open Dashboard</a>`
              : `<button class="sm" data-resync="${esc(r.service.slug)}">Resend</button>`}</td></tr>`).join('')
          || `<tr><td colspan="6">${empty('No Services', '')}</td></tr>`}</tbody>
      </table></div>
    </div>
    <div class="grid cols-2" style="margin-top:14px">
      <div class="card"><div class="card-head"><h2>People</h2></div>
        <table><tbody>${a.users.map((u) => `<tr><td>${esc(u.full_name || '—')}<div class="hint">${esc(u.email)}</div></td>
          <td>${esc(u.role)}</td><td>${u.has_password ? '<span class="pill ok">Active</span>' : '<span class="pill warn">Invited</span>'}</td>
          <td class="right"><button class="sm" data-link="${esc(u.id)}">${u.has_password ? 'Send Reset Link' : 'Resend Invite'}</button></td></tr>`).join('')}</tbody></table>
      </div>
      <div class="card"><div class="card-head"><h2>Invoices</h2></div>
        ${a.invoices_error ? banner('', a.invoices_error, 'warn') : ''}
        ${a.invoices.length ? `<table><tbody>${a.invoices.map((i) => `<tr><td>${esc(fmtDate(i.created))}</td><td>${esc(i.status)}</td>
          <td class="num">${money(i.status === 'paid' ? i.amount_paid : i.amount_due, i.currency)}</td>
          <td class="right">${i.hosted_invoice_url ? `<a class="btn sm" target="_blank" rel="noopener" href="${esc(i.hosted_invoice_url)}">View</a>` : ''}</td></tr>`).join('')}</tbody></table>`
          : empty('No Invoices', '')}
      </div>
    </div>
    <div class="card" style="margin-top:14px"><div class="card-head"><h2>Odoo</h2></div>
      ${(a.odoo || []).length ? `<table><tbody>${a.odoo.map((c) => `<tr>
          <td>${esc(c.url)}<div class="hint">${esc(c.db)} · ${esc(c.login)}${c.version ? ` · Odoo ${esc(c.version)}` : ''}</div></td>
          <td>${c.status === 'connected' ? '<span class="pill ok">Connected</span>' : c.status === 'failed' ? `<span class="pill bad" title="${esc(c.status_message || '')}">Failed</span>` : '<span class="pill mute">Not Checked</span>'}</td>
          <td>${c.services.map((sv) => `${esc(sv.name)}: ${sv.ready === true ? 'ready' : sv.ready === false ? esc(sv.message || 'not ready') : 'not checked'}`).join('<br>') || '—'}</td>
        </tr>`).join('')}</tbody></table>` : empty('No Odoo Connected', '')}
    </div>
    <div class="card" style="margin-top:14px"><div class="card-head"><h2>History</h2></div>
      <ul class="timeline">${a.audit.map((e) => `<li><div><strong>${esc(e.action)}</strong>${e.detail ? ` · ${esc(e.detail)}` : ''}</div>
        <div class="hint">${esc(e.actor)} · ${esc(fmtAgo(e.at))}</div></li>`).join('') || '<li class="muted">Nothing yet.</li>'}</ul>
    </div>
    <div class="card danger-zone" style="margin-top:14px">
      <div class="card-head"><h2>Delete Account</h2></div>
      <p>Cancels any Stripe subscription immediately, asks every service to delete its data, and keeps a closure record.</p>
      <button class="danger-outline" id="delete">Delete Account</button>
    </div>`;

  const reload = () => renderAccount(mount, route, { id });
  $('#edit', mount).addEventListener('click', async () => {
    const ok = await dialog({ title: 'Edit Account', body: `${field({ name: 'name', label: 'Company', value: a.name, required: true })}
      ${field({ name: 'country', label: 'Country', value: a.country || '' })}${field({ name: 'timezone', label: 'Timezone', value: a.timezone })}`,
    onSubmit: (form) => api.patch(`/staff/accounts/${id}`, readForm(form)) });
    if (ok) reload();
  });
  $('#suspend', mount)?.addEventListener('click', async () => {
    const ok = await dialog({ title: 'Suspend Account', danger: true, submitLabel: 'Suspend',
      body: `<p>Every service stops working for ${esc(a.name)} until reactivated. Data is kept.</p>
        ${field({ name: 'reason', label: 'Reason (staff only)' })}`,
      onSubmit: (form) => api.post(`/staff/accounts/${id}/suspend`, readForm(form)) });
    if (ok) reload();
  });
  $('#reactivate', mount)?.addEventListener('click', () => guard(async () => { await api.post(`/staff/accounts/${id}/reactivate`); reload(); }, 'Reactivated.'));
  $('#addSvc', mount)?.addEventListener('click', async () => {
    const pending = dialog({ title: 'Add Service', submitLabel: 'Add',
      body: `${field({ name: 'service', label: 'Service', options: missing.map((s) => ({ value: s.slug, label: s.name })) })}
        <div id="pkgHost"></div>${field({ name: 'trial_days', label: 'Trial days', type: 'number' })}`,
      onSubmit: (form) => {
        const v = readForm(form);
        return api.post(`/staff/accounts/${id}/services`, { service: v.service, package_id: v.package_id || null, trial_days: v.trial_days });
      } });
    // The package list follows the chosen service.
    const select = document.querySelector('.wiz select[name=service]');
    const paint = () => {
      const s = svcs.find((x) => x.slug === select.value);
      document.querySelector('#pkgHost').innerHTML = s ? field({ name: 'package_id', label: 'Package', options: packageOptions(s) }) : '';
    };
    select.addEventListener('change', paint);
    paint();
    if (await pending) reload();
  });
  $$('[data-manage]', mount).forEach((b) => b.addEventListener('click', () => manageDialog(a, b.dataset.manage, svcs).then((ok) => ok && reload())));
  $$('[data-resync]', mount).forEach((b) => b.addEventListener('click', () => guard(async () => {
    const r = await api.post(`/staff/accounts/${id}/services/${b.dataset.resync}/resync`);
    toast(r.message, 'ok'); reload();
  })));
  $$('[data-link]', mount).forEach((b) => b.addEventListener('click', () => guard(async () => {
    const r = await api.post(`/staff/accounts/${id}/users/${b.dataset.link}/resend`);
    toast(r.message, 'ok');
  })));
  $('#delete', mount).addEventListener('click', async () => {
    const ok = await dialog({ title: `Delete ${a.name}?`, danger: true, submitLabel: 'Delete For Good',
      body: `${field({ name: 'reason', label: 'Reason', type: 'textarea' })}
        <label><input type="checkbox" name="force"> Delete even if a service can't delete its data</label>`,
      onSubmit: (form) => api.post(`/staff/accounts/${id}/delete`, readForm(form)) });
    if (ok) { toast(ok.message, 'ok'); (ok.warnings || []).forEach((w) => toast(w, 'warn')); window.location.hash = '#/accounts'; }
  });
}

async function manageDialog(account, slug, svcs) {
  const row = account.services.find((r) => r.service.slug === slug);
  const service = svcs.find((s) => s.slug === slug);
  const over = row.entitlement_overrides || {};
  const keys = [...new Set([...Object.keys(row.package?.entitlements || {}), ...Object.keys(over)])];
  const trial = row.trial_ends_at ? row.trial_ends_at.slice(0, 10) : '';
  return dialog({
    title: `${account.name} — ${service.name}`, submitLabel: 'Save', wide: true,
    body: `
      <div class="grid cols-2">
        ${field({ name: 'package_id', label: 'Package', options: packageOptions(service, row.package?.id), value: row.package?.id })}
        ${field({ name: 'status', label: 'Status', value: row.status, options: ['trialing', 'active', 'past_due', 'suspended', 'cancelled']
          .map((v) => ({ value: v, label: STATUS_LABEL[v] || v })) })}
        ${field({ name: 'trial_ends_at', label: 'Trial ends', type: 'date', value: trial })}
      </div>
      <p class="hint">${row.billed ? 'Billed through Stripe: a package change is prorated, and Stripe stays in charge of the status.'
        : 'Not billed online: setting Active here is how an account billed offline is switched on.'}</p>
      <div class="section-title">Custom limits</div>
      <p class="hint">Leave blank to use the package's value. Applies on top of any package.</p>
      <div class="grid cols-3">${keys.map((k) => field({ name: `ent_${k}`, label: k.replace(/_/g, ' '), type: 'number',
        value: over[k] ?? '', placeholder: String(row.package?.entitlements?.[k] ?? 'unlimited') })).join('')}</div>`,
    onSubmit: (form) => {
      const v = readForm(form);
      const overrides = {};
      keys.forEach((k) => { if (v[`ent_${k}`] != null) overrides[k] = v[`ent_${k}`]; });
      const body = { entitlement_overrides: overrides };
      if (v.package_id && v.package_id !== row.package?.id) body.package_id = v.package_id;
      if (v.status !== row.status) body.status = v.status;
      if (v.trial_ends_at !== trial) body.trial_ends_at = v.trial_ends_at ? `${v.trial_ends_at}T23:59:00Z` : null;
      return api.patch(`/staff/accounts/${account.id}/services/${slug}`, body);
    },
  });
}

/* =========================================================================
 * Payments
 * ====================================================================== */
async function renderPayments(mount) {
  const p = await api.get('/staff/payments');
  const s = p.stripe;
  mount.innerHTML = `
    ${p.error ? banner('Stripe Error', p.error, 'warn') : ''}
    <div class="grid cols-2">
      <div class="card">
        <div class="card-head"><h2>Stripe</h2><div class="actions">
          <button class="sm" id="keys">${s.enabled ? 'Change Keys' : 'Add Keys'}</button>
          ${s.enabled ? '<button class="sm" id="test">Test</button>' : ''}</div></div>
        <dl class="kv">
          <dt>Status</dt><dd>${s.enabled ? `<span class="pill ok">On</span> ${s.mode === 'live' ? '<span class="pill warn">Live</span>' : '<span class="pill">Test mode</span>'}` : '<span class="pill mute">Off</span>'}</dd>
          <dt>Secret key</dt><dd>${esc(s.secret_key_hint || '—')} ${s.source ? `<span class="hint">(${esc(s.source)})</span>` : ''}</dd>
          <dt>Webhook secret</dt><dd>${s.webhook_secret_set ? 'Set' : '<span class="pill warn">Missing</span>'}</dd>
          <dt>Webhook URL</dt><dd class="mono">${esc(s.webhook_url)}</dd>
        </dl>
        <p class="hint" style="margin-top:10px">In Stripe, send <span class="mono">checkout.session.completed</span>,
          <span class="mono">customer.subscription.*</span> and <span class="mono">invoice.*</span> to the webhook URL.
          Each package needs its Stripe price id (Catalog).</p>
      </div>
      <div class="card">
        <div class="card-head"><h2>Totals</h2></div>
        <dl class="kv">
          <dt>Paying accounts</dt><dd>${p.subscribers.filter((x) => x.stripe_status === 'active').length}</dd>
          <dt>Past due</dt><dd>${p.subscribers.filter((x) => ['past_due', 'unpaid'].includes(x.stripe_status)).length}</dd>
          <dt>Cancelling</dt><dd>${p.subscribers.filter((x) => x.cancel_at_period_end).length}</dd>
          <dt>Per month</dt><dd>${money(p.subscribers.filter((x) => x.stripe_status === 'active').reduce((t, x) => t + x.monthly_total_cents, 0))}</dd>
        </dl>
      </div>
    </div>
    <div class="card" style="margin-top:14px"><div class="card-head"><h2>Subscriptions</h2></div>
      <div class="table-wrap"><table>
        <thead><tr><th>Account</th><th>Services</th><th>Stripe status</th><th>Interval</th><th>Renews</th><th class="num">Per month</th></tr></thead>
        <tbody>${p.subscribers.map((x) => `<tr class="click" data-account="${esc(x.id)}"><td>${esc(x.name)}</td>
          <td class="muted">${esc(x.services.join(', ') || '—')}</td>
          <td>${esc(x.stripe_status || 'checkout started')}${x.cancel_at_period_end ? ' <span class="pill warn">cancelling</span>' : ''}</td>
          <td>${x.billing_interval === 'year' ? 'Yearly' : 'Monthly'}</td><td>${esc(fmtDate(x.renews_at))}</td>
          <td class="num">${money(x.monthly_total_cents)}</td></tr>`).join('') || `<tr><td colspan="6">${empty('No Subscriptions Yet', '')}</td></tr>`}</tbody>
      </table></div></div>
    <div class="card" style="margin-top:14px"><div class="card-head"><h2>Recent Invoices</h2></div>
      ${p.invoices.length ? `<div class="table-wrap"><table>
        <thead><tr><th>Date</th><th>Customer</th><th>Number</th><th>Status</th><th class="num">Amount</th><th></th></tr></thead>
        <tbody>${p.invoices.map((i) => `<tr><td>${esc(fmtDate(i.created))}</td><td>${esc(i.customer_name || i.customer_email || i.customer || '—')}</td>
          <td class="mono">${esc(i.number || i.id)}</td><td>${esc(i.status)}${i.attempt_count > 1 ? ` <span class="pill bad">${i.attempt_count} attempts</span>` : ''}</td>
          <td class="num">${money(i.status === 'paid' ? i.amount_paid : i.amount_due, i.currency)}</td>
          <td class="right">${i.hosted_invoice_url ? `<a class="btn sm" target="_blank" rel="noopener" href="${esc(i.hosted_invoice_url)}">View</a>` : ''}</td></tr>`).join('')}</tbody>
      </table></div>` : empty('No Invoices', s.enabled ? '' : 'Stripe is not connected.')}
    </div>`;
  $$('[data-account]', mount).forEach((tr) => tr.addEventListener('click', () => { window.location.hash = `#/accounts/${tr.dataset.account}`; }));
  $('#keys', mount).addEventListener('click', async () => {
    const ok = await dialog({ title: 'Stripe Keys', submitLabel: 'Save Keys',
      body: `${field({ name: 'secret_key', label: 'Secret key', type: 'password', placeholder: 'sk_live_… or sk_test_…' })}
        ${field({ name: 'webhook_secret', label: 'Webhook signing secret', type: 'password', placeholder: 'whsec_…' })}
        <p class="hint">Stored encrypted. Leave a box empty to keep what is saved.</p>`,
      onSubmit: (form) => {
        const v = readForm(form);
        const body = {};
        if (v.secret_key) body.secret_key = v.secret_key.trim();
        if (v.webhook_secret) body.webhook_secret = v.webhook_secret.trim();
        return api.patch('/staff/payments/stripe', body);
      } });
    if (ok) renderPayments(mount);
  });
  $('#test', mount)?.addEventListener('click', () => guard(async () => toast((await api.post('/staff/payments/stripe/test')).message, 'ok')));
}

/* =========================================================================
 * Catalog: services and packages
 * ====================================================================== */
/* What each service's entitlements mean, as labelled limit boxes. A service not
 * listed here gets a JSON box instead. Empty = unlimited. */
const LIMIT_FIELDS = {
  biometric: [
    { key: 'max_employees', label: 'Employees', help: 'Badges matched to an Odoo employee. Lowering it never unmaps anyone; only new matches wait.' },
    { key: 'max_devices', label: 'Devices', help: 'Terminals the account has added. Punches from terminals beyond the limit are held until it covers them.' },
    { key: 'min_sync_interval_minutes', label: 'Fastest sync (min)', help: 'The fastest interval a customer on this package can choose.' },
  ],
};

/** Entitlements as people read them: "2 devices · 50 employees · sync every 15 min". */
const LIMIT_TEXT = {
  max_devices: (v) => (v == null ? 'Unlimited devices' : `${v} device${v === 1 ? '' : 's'}`),
  max_employees: (v) => (v == null ? 'unlimited employees' : `${v} employee${v === 1 ? '' : 's'}`),
  max_seats: (v) => (v == null ? 'Unlimited seats' : `${v} seat${v === 1 ? '' : 's'}`),
  min_sync_interval_minutes: (v) => (v == null ? 'any sync interval' : `sync every ${v} min`),
};
function limitsText(ent) {
  const parts = Object.entries(ent || {}).map(([k, v]) => (LIMIT_TEXT[k] ? LIMIT_TEXT[k](v) : `${k.replace(/_/g, ' ')}: ${v ?? 'unlimited'}`));
  if (!parts.length) return '—';
  parts[0] = parts[0][0].toUpperCase() + parts[0].slice(1);
  return parts.join(' · ');
}

const dollars = (cents) => (cents == null ? '' : cents / 100);
const cents = (v) => (v == null || v === '' ? null : Math.round(Number(v) * 100));

function openPackageDialog(service, pkg, onSaved) {
  document.querySelector('dialog.plan-dialog')?.remove();
  const dlg = document.createElement('dialog');
  dlg.className = 'wizard config-dialog plan-dialog';
  document.body.append(dlg);
  const p = pkg || { is_active: true, is_default: false, entitlements: {} };
  const limits = LIMIT_FIELDS[service.slug];
  const close = () => { dlg.close(); dlg.remove(); };
  const accounts = pkg?.accounts || 0;

  dlg.innerHTML = `
    <div class="wiz-head">
      <strong>${pkg ? `Edit ${esc(pkg.name)}` : `New ${esc(service.name)} Package`}</strong>
      <button type="button" class="link wiz-x" data-close aria-label="Close">&times;</button>
    </div>
    <div class="wiz-body">
      <form id="pkgForm">
        <div class="grid cols-2">
          ${field({ name: 'name', label: 'Name', value: p.name || '', required: true, placeholder: 'Growth' })}
          ${field({ name: 'price', label: 'Price per month (USD)', type: 'number', value: dollars(p.monthly_price_cents),
            tip: true, help: 'What the pricing page shows. Empty shows "Custom". What a customer is charged is the Stripe price below, so keep the two in step.' })}
        </div>
        ${field({ name: 'yearly_price', label: 'Price per year (USD)', type: 'number', value: dollars(p.yearly_price_cents),
          tip: true, help: 'Shown when a visitor switches to yearly. Empty means no yearly option.' })}
        ${field({ name: 'description', label: 'Description', value: p.description || '', placeholder: 'Growing teams on several devices.' })}
        ${field({ name: 'features', label: 'Features on the pricing page', type: 'textarea', value: (p.features || []).join('\n'),
          tip: true, help: 'One per line. Empty lists the limits below.' })}
        <h3 style="margin:6px 0 10px;font-size:13px">Limits <span class="hint">empty = unlimited</span></h3>
        ${limits
          ? `<div class="grid cols-3">${limits.map((l) => field({ name: `lim_${l.key}`, label: l.label, type: 'number',
              value: p.entitlements?.[l.key] ?? '', tip: true, help: l.help })).join('')}</div>`
          : field({ name: 'entitlements', label: 'Entitlements (JSON, sent to the service)', type: 'textarea',
              value: JSON.stringify(p.entitlements || {}, null, 1) })}
        <h3 style="margin:6px 0 10px;font-size:13px">Selling</h3>
        <div class="grid cols-2">
          ${field({ name: 'stripe_price_id', label: 'Stripe monthly price', value: p.stripe_price_id || '', placeholder: 'price_…',
            tip: true, help: 'A recurring monthly Price. Empty means it cannot be bought online.' })}
          ${field({ name: 'stripe_yearly_price_id', label: 'Stripe yearly price', value: p.stripe_yearly_price_id || '', placeholder: 'price_…',
            tip: true, help: 'A recurring yearly Price. Empty means yearly payment cannot be bought online.' })}
        </div>
        <div class="grid cols-3">
          ${field({ name: 'trial_days', label: 'Trial (days)', type: 'number', value: p.trial_days ?? '',
            tip: true, help: 'Empty uses the platform default.' })}
          ${field({ name: 'is_active', label: 'Status', value: String(p.is_active !== false), boolean: true, required: true,
            options: [{ value: 'true', label: 'On Sale' }, { value: 'false', label: 'Retired: kept for accounts on it' }] })}
          ${field({ name: 'is_default', label: 'Recommended', value: String(!!p.is_default), boolean: true, required: true,
            options: [{ value: 'false', label: 'No' }, { value: 'true', label: 'Yes' }],
            tip: true, help: 'Marked on the pricing page and chosen when a signup picks nothing.' })}
        </div>
        ${accounts ? `<div class="hint" style="margin-top:4px">${accounts} account${accounts === 1 ? ' is' : 's are'} on this package; changed limits reach them straight away.</div>` : ''}
      </form>
    </div>
    <div class="wiz-foot">
      <button type="button" class="link" data-close>Cancel</button>
      <div class="actions"><button type="submit" form="pkgForm" class="primary save">${pkg ? 'Save Package' : 'Create Package'}</button></div>
    </div>`;

  $$('[data-close]', dlg).forEach((b) => b.addEventListener('click', close));
  dlg.addEventListener('cancel', (e) => { e.preventDefault(); close(); });
  dlg.addEventListener('click', (e) => { if (e.target === dlg) close(); });

  $('#pkgForm', dlg).addEventListener('submit', (event) => {
    event.preventDefault();
    const v = readForm(event.target);
    let entitlements;
    if (limits) {
      entitlements = { ...(p.entitlements || {}) };
      limits.forEach((l) => { const n = v[`lim_${l.key}`]; entitlements[l.key] = n === '' || n == null ? null : Number(n); });
    } else {
      try { entitlements = JSON.parse(v.entitlements || '{}'); } catch { toast('Entitlements must be valid JSON, e.g. {"max_devices": 5}', 'bad'); return; }
    }
    const body = {
      name: v.name.trim(), description: (v.description || '').trim() || null,
      monthly_price_cents: cents(v.price), yearly_price_cents: cents(v.yearly_price),
      stripe_price_id: (v.stripe_price_id || '').trim() || null,
      stripe_yearly_price_id: (v.stripe_yearly_price_id || '').trim() || null,
      trial_days: v.trial_days === '' ? null : v.trial_days, currency: p.currency || 'usd', entitlements,
      features: (v.features || '').split('\n').map((x) => x.trim()).filter(Boolean),
      is_default: v.is_default, is_active: v.is_active,
    };
    busy($('.save', dlg), async () => {
      const saved = await guard(() => (pkg ? api.patch(`/staff/packages/${pkg.id}`, body) : api.post(`/staff/services/${service.id}/packages`, body)));
      if (!saved) return;
      close();
      toast(`${saved.name} ${pkg ? 'saved' : 'created'}`, 'ok');
      onSaved();
    });
  });
  dlg.showModal();
  $('#name', dlg).focus();
}

function serviceForm(s = {}) {
  return `
    <div class="grid cols-2">
      ${s.id ? '' : field({ name: 'slug', label: 'Slug (token audience, never changes)', required: true, placeholder: 'marketplace' })}
      ${field({ name: 'name', label: 'Name', value: s.name || '', required: true })}
      ${field({ name: 'base_url', label: 'Public URL', value: s.base_url || '', required: true, placeholder: 'https://biometric.odoobridger.com' })}
      ${field({ name: 'internal_url', label: 'Internal URL (server to server)', value: s.internal_url || '', placeholder: 'http://biobridge:8000' })}
      ${field({ name: 'sso_callback_path', label: 'SSO callback path', value: s.sso_callback_path || '/app/#/sso' })}
      ${field({ name: 'tagline', label: 'Tagline', value: s.tagline || '' })}
    </div>
    ${field({ name: 'description', label: 'Description', type: 'textarea', value: s.description || '' })}
    <label><input type="checkbox" name="is_active" ${s.is_active !== false ? 'checked' : ''}> Running</label>
    <label><input type="checkbox" name="is_public" ${s.is_public !== false ? 'checked' : ''}> Sold on the website and in the portal</label>
    <label><input type="checkbox" name="uses_odoo" ${s.uses_odoo ? 'checked' : ''}> Works on the customer's Odoo (is sent the account's Odoo connection)</label>`;
}

async function showSecret(secret, message) {
  await dialog({ title: 'Client Secret', submitLabel: 'Done',
    body: `<p>${esc(message || 'Set this as the service’s PLATFORM_CLIENT_SECRET. It is shown only once.')}</p><div class="secret-box">${esc(secret)}</div>` });
}

async function renderCatalog(mount) {
  state.services = null;
  const svcs = await services();
  mount.innerHTML = `
    <div class="row" style="justify-content:flex-end;margin-bottom:14px"><button class="primary" id="newSvc">New Service</button></div>
    ${svcs.map((s) => `
      <div class="card" style="margin-bottom:14px">
        <div class="card-head"><h2>${esc(s.name)} <span class="hint">${esc(s.slug)} · ${s.accounts} account${s.accounts === 1 ? '' : 's'}</span></h2>
          <div class="actions">
            ${!s.is_active ? '<span class="pill mute">Stopped</span>' : ''}${!s.is_public ? '<span class="pill">Unlisted</span>' : ''}
            <button class="sm" data-edit-svc="${esc(s.id)}">Edit</button>
            <button class="sm" data-secret="${esc(s.id)}">Rotate Secret</button>
            <button class="sm primary" data-new-pkg="${esc(s.id)}">New Package</button></div></div>
        <p class="hint">${esc(s.base_url)}${s.internal_url ? ` · internal ${esc(s.internal_url)}` : ''}${s.has_client_secret ? '' : ' · <strong>no client secret yet</strong>'}</p>
        <div class="table-wrap"><table>
          <thead><tr><th>Package</th><th class="num">Monthly</th><th class="num">Yearly</th><th>Stripe</th><th>Limits</th><th></th></tr></thead>
          <tbody>${s.packages.map((p) => `<tr>
            <td><strong>${esc(p.name)}</strong> ${p.is_default ? '<span class="pill ok">Recommended</span>' : ''}${!p.is_active ? ' <span class="pill mute">Retired</span>' : ''}</td>
            <td class="num">${money(p.monthly_price_cents, p.currency)}</td><td class="num">${money(p.yearly_price_cents, p.currency)}</td>
            <td>${p.stripe_price_id ? '<span class="pill ok">Monthly</span>' : ''} ${p.stripe_yearly_price_id ? '<span class="pill ok">Yearly</span>' : ''}
              ${!p.stripe_price_id && !p.stripe_yearly_price_id ? '<span class="pill warn">Not buyable</span>' : ''}</td>
            <td>${esc(limitsText(p.entitlements))}</td>
            <td class="right"><button class="sm" data-edit-pkg="${esc(p.id)}">Edit</button></td></tr>`).join('')
            || `<tr><td colspan="6">${empty('No Packages', '')}</td></tr>`}</tbody>
        </table></div>
      </div>`).join('') || `<div class="card">${empty('No Services', 'Add the first one.')}</div>`}`;
  const reload = () => renderCatalog(mount);
  $('#newSvc', mount).addEventListener('click', async () => {
    const r = await dialog({ title: 'New Service', submitLabel: 'Create', wide: true, body: serviceForm(),
      onSubmit: (form) => api.post('/staff/services', readForm(form)) });
    if (r) { await showSecret(r.client_secret); reload(); }
  });
  $$('[data-edit-svc]', mount).forEach((b) => b.addEventListener('click', async () => {
    const s = svcs.find((x) => x.id === b.dataset.editSvc);
    const r = await dialog({ title: `Edit ${s.name}`, wide: true, body: serviceForm(s),
      onSubmit: (form) => api.patch(`/staff/services/${s.id}`, readForm(form)) });
    if (r) reload();
  }));
  $$('[data-secret]', mount).forEach((b) => b.addEventListener('click', async () => {
    const ok = await dialog({ title: 'Rotate Client Secret?', danger: true, submitLabel: 'Rotate',
      body: '<p>Sign-ins into this service stop working until the new secret is set on it.</p>',
      onSubmit: () => api.post(`/staff/services/${b.dataset.secret}/secret`) });
    if (ok) await showSecret(ok.client_secret, ok.message);
  }));
  $$('[data-new-pkg]', mount).forEach((b) => b.addEventListener('click', () => {
    openPackageDialog(svcs.find((x) => x.id === b.dataset.newPkg), null, reload);
  }));
  $$('[data-edit-pkg]', mount).forEach((b) => b.addEventListener('click', () => {
    const svc = svcs.find((x) => x.packages.some((q) => q.id === b.dataset.editPkg));
    openPackageDialog(svc, svc.packages.find((q) => q.id === b.dataset.editPkg), reload);
  }));
}

/* =========================================================================
 * Closed accounts, staff team
 * ====================================================================== */
async function renderClosures(mount) {
  const rows = await api.get('/staff/closures');
  mount.innerHTML = `<div class="card"><div class="table-wrap"><table>
    <thead><tr><th>Account</th><th>Owner</th><th>Services</th><th>Closed by</th><th>Reason</th><th>When</th></tr></thead>
    <tbody>${rows.map((c) => `<tr><td>${esc(c.account_name)}</td><td>${esc(c.owner_email || '—')}</td><td>${esc(c.services.join(', '))}</td>
      <td>${esc(c.closed_by)}</td><td>${esc(c.reason || '—')}</td><td class="nowrap">${esc(fmtDate(c.closed_at))}</td></tr>`).join('')
      || `<tr><td colspan="6">${empty('No Closed Accounts', '')}</td></tr>`}</tbody></table></div></div>`;
}

async function renderTeam(mount) {
  const rows = await api.get('/staff/team');
  mount.innerHTML = `<div class="card">
    <div class="card-head"><h2>Staff</h2><div class="actions"><button class="primary sm" id="add">Add Staff</button></div></div>
    <div class="table-wrap"><table><thead><tr><th>Name</th><th>Email</th><th>Last sign-in</th><th>Status</th></tr></thead>
    <tbody>${rows.map((s) => `<tr class="click" data-staff="${esc(s.id)}">
      <td><b>${esc(s.full_name || '—')}</b>${s.id === state.me.id ? ' <span class="hint">(you)</span>' : ''}${s.is_static ? ' <span class="pill mute">From settings</span>' : ''}</td>
      <td>${esc(s.email)}</td><td>${esc(s.last_login_at ? fmtAgo(s.last_login_at) : 'never')}</td>
      <td>${s.is_active ? '<span class="pill ok">Active</span>' : '<span class="pill mute">Disabled</span>'}</td></tr>`).join('')}</tbody>
    </table></div></div>`;
  $('#add', mount).addEventListener('click', async () => {
    const ok = await dialog({ title: 'Add Staff', submitLabel: 'Add',
      body: `${field({ name: 'full_name', label: 'Name' })}${field({ name: 'email', label: 'Email', type: 'email', required: true })}
        ${field({ name: 'password', label: 'Password', type: 'password', required: true, help: 'At least 10 characters, mixed case and a number. Share it securely.' })}`,
      onSubmit: (form) => api.post('/staff/team', readForm(form)) });
    if (ok) renderTeam(mount);
  });
  mount.onclick = (e) => {
    const tr = e.target.closest('[data-staff]');
    if (!tr) return;
    const s = rows.find((x) => x.id === tr.dataset.staff);
    const self = s.id === state.me.id;
    guard(async () => {
      const ok = await dialog({
        title: 'Edit Staff', submitLabel: 'Save',
        body: `${field({ name: 'full_name', label: 'Name', value: s.full_name || '', required: true })}
          <div class="field"><label>Email</label><div class="wiz-fixed">${esc(s.email)}</div></div>
          ${self ? '' : field({ name: 'is_active', label: 'Status', value: String(s.is_active), boolean: true, required: true,
            options: [{ value: 'true', label: 'Active' }, { value: 'false', label: 'Disabled: cannot sign in' }] })}
          <div class="section-title" style="margin-top:6px">Change Password</div>
          ${s.is_static
            ? '<p class="hint">This login comes from STAFF_EMAIL and STAFF_PASSWORD in the settings; change its password there.</p>'
            : `${self ? field({ name: 'current_password', label: 'Current password', type: 'password' }) : ''}
              ${field({ name: 'password', label: 'New password', type: 'password',
                help: 'Leave blank to keep it. At least 10 characters, mixed case and a number.' })}
              ${field({ name: 'confirm', label: 'Confirm password', type: 'password' })}`}`,
        onSubmit: async (form) => {
          const v = readForm(form);
          const body = { full_name: v.full_name };
          if (!self) body.is_active = v.is_active;
          if (v.password) {
            if (v.password !== v.confirm) throw new Error('The new passwords do not match.');
            body.password = v.password;
            if (self) body.current_password = v.current_password || '';
          }
          return api.patch(`/staff/team/${s.id}`, body);
        },
      });
      if (ok) {
        toast('Saved.', 'ok');
        if (self) { state.me = { ...state.me, full_name: ok.full_name }; route(); } else renderTeam(mount);
      }
    });
  };
}

/* =========================================================================
 * Router
 * ====================================================================== */
/** The signed-in staff member in the top bar (amber, so it never reads as a customer's tag). */
function staffChip(me) {
  const name = (me?.full_name || '').trim() || me?.email || 'Staff';
  return `<span class="account-chip staff-chip" title="${esc(`Staff console · ${me?.email || ''}`)}">
    <span class="account-initial" aria-hidden="true">${esc(name[0].toUpperCase())}</span><span class="account-name">${esc(name)}</span></span>`;
}

const PAGES = [
  { path: '/overview', title: 'Overview', icon: 'overview', render: renderOverview },
  { path: '/accounts', title: 'Accounts', icon: 'accounts', render: renderAccounts },
  { path: '/leads', title: 'Leads', icon: 'mail', render: renderLeads },
  { path: '/payments', title: 'Payments', icon: 'card', render: renderPayments },
  { path: '/catalog', title: 'Services & Packages', icon: 'tag', render: renderCatalog },
  { path: '/closed', title: 'Closed Accounts', icon: 'closed', render: renderClosures },
  { path: '/team', title: 'Staff', icon: 'people', render: renderTeam },
];
const DETAIL = [{ pattern: '/accounts/:id', title: 'Account', render: renderAccount },
  { pattern: '/service/:slug', title: 'Service', render: renderServiceDashboard }];

let running = false;
let again = false;
async function route() {
  if (running) { again = true; return; }
  running = true;
  try {
    const r = parseHash();
    const signedIn = session.signedIn || await restore();
    if (!signedIn) return renderLogin();
    if (r.path === '/' || r.path === '/login') { window.location.hash = '#/overview'; return; }
    if (!state.me) state.me = await api.get('/auth/staff/me');
    let page = PAGES.find((p) => p.path === r.path);
    let params = {};
    if (!page) {
      for (const d of DETAIL) {
        const m = match(d.pattern, r.path);
        if (m) { page = d; params = m; break; }
      }
    }
    let navServices = [];
    try { navServices = (await services()).map((sv) => ({ path: `/service/${sv.slug}`, title: sv.name, icon: 'grid' })); } catch { /* the nav still works without them */ }
    mountShell({
      nav: [{ label: 'Console', items: PAGES },
        ...(navServices.length ? [{ label: 'Services', items: navServices }] : [])],
      user: state.me.email,
      pill: staffChip(state.me),
      onSignOut: async () => { await signOut(); state.me = null; unmountShell(); route(); },
    });
    $('#pageTitle').textContent = page ? page.title : 'Not Found';
    const content = $('#content');
    const fresh = content.cloneNode(false);
    content.replaceWith(fresh);
    if (!page) { fresh.innerHTML = empty('Page Not Found', ''); return; }
    fresh.innerHTML = loading();
    try {
      await page.render(fresh, r, params);
    } catch (error) {
      if (error.status !== 401) fresh.innerHTML = banner('Could Not Load This Page', error.message, 'bad');
    }
  } finally {
    running = false;
    if (again) { again = false; route(); }
  }
}

window.addEventListener('ob:signed-out', () => { state.me = null; unmountShell(); route(); });
window.addEventListener('hashchange', route);
wireTips();
route();
