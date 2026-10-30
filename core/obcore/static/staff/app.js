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
    </form>`, { badge: 'staff console' });
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
const SEVERITY = { critical: ['bad', 'Critical'], high: ['bad', 'High'], medium: ['warn', 'Medium'], low: ['mute', 'Low'] };
const SUB_LABEL = { paying: ['ok', 'Paying'], trial: ['trialing', 'Trial'], past_due: ['bad', 'Past Due'], suspended: ['bad', 'Suspended'] };
const MIX = [['paying', 'Paying', 'var(--ok)'], ['trial', 'Free trial', 'var(--accent)'],
  ['past_due', 'Past due', 'var(--warn)'], ['suspended', 'Suspended', 'var(--bad)'], ['no_service', 'No service', 'var(--faint)']];

const tag = (tone, text) => `<span class="pill ${tone}">${esc(text)}</span>`;
const badge = (tone, text) => `<span class="pill ${tone} kpi-b">${esc(text)}</span>`;

/** "3 h", "2 days": how long a problem has been going on. */
function since(iso) {
  if (!iso) return '';
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 3600) return `${Math.max(1, Math.round(s / 60))} min`;
  if (s < 86400) { const h = Math.floor(s / 3600); const m = Math.round((s % 3600) / 60); return m ? `${h} h ${m} min` : `${h} h`; }
  const d = Math.floor(s / 86400);
  return `${d} day${d === 1 ? '' : 's'}`;
}

/* ---- overview helpers ---------------------------------------------------- */
const RANGES = [['30d', 'Last 30 Days'], ['6m', 'Last 6 Months'], ['12m', 'Last 12 Months'], ['all', 'All Time']];
let overviewRange = '30d';
const pct = (v) => (v == null ? '—' : `${Number(v).toLocaleString(undefined, { maximumFractionDigits: 1 })}%`);
const bucketLabel = (key, unit, long = false) => (unit === 'day'
  ? new Date(`${key}T00:00:00`).toLocaleDateString(undefined, long ? { weekday: 'short', day: 'numeric', month: 'short' } : { day: 'numeric', month: 'short' })
  : new Date(`${key}-01T00:00:00`).toLocaleDateString(undefined, long ? { month: 'long', year: 'numeric' } : { month: 'short' }));

/** "▲ 12% vs previous 30 days". ``upIsGood`` decides the colour; ``fmt`` formats the
 *  numbers shown when the earlier figure was 0 (a percentage of 0 means nothing). */
function compare(m, upIsGood = true, fmt = (v) => String(v)) {
  if (!m || !m.basis) return '';
  if (m.change_pct == null) {
    return m.previous === 0 && m.value
      ? `<span class="cmp up ${upIsGood ? 'good' : 'bad'}">+${esc(fmt(m.value))}</span> <span class="cmp-b">vs ${esc(fmt(0))} ${esc(m.basis)}</span>` : '';
  }
  if (m.change_pct === 0) return `<span class="cmp flat">No change</span> <span class="cmp-b">vs ${esc(m.basis)}</span>`;
  const up = m.change_pct > 0;
  return `<span class="cmp ${up ? 'up' : 'down'} ${up === upIsGood ? 'good' : 'bad'}">${up ? '▲' : '▼'} ${pct(Math.abs(m.change_pct))}</span> <span class="cmp-b">vs ${esc(m.basis)}</span>`;
}

/** Bars, one per day or month, with a tooltip. ``cents`` draws the axis in dollars. */
function bucketBars(points, { unit, value, label, cents = false, height = 220 }) {
  const W = 480; const H = height; const L = cents ? 46 : 30; const B = 26; const T = 12; const R = 6;
  const max = Math.max(cents ? 1000 : 4, ...points.map((p) => value(p) || 0));
  let step = Math.ceil(max / 4);
  if (cents) { const mag = 10 ** Math.floor(Math.log10(max / 4)); step = Math.ceil(max / 4 / mag) * mag; }
  const top = step * 4;
  const short = (c) => { const d = c / 100; return d >= 1000 ? `$${+(d / 1000).toFixed(1)}k` : `$${+d.toFixed(2)}`.replace(/\.00$/, ''); };
  const bw = (W - L - R) / Math.max(1, points.length);
  const y = (v) => H - B - ((H - B - T) * v) / top;
  const grid = [0, 1, 2, 3, 4].map((g) => `<line x1="${L}" x2="${W - R}" y1="${y(g * step)}" y2="${y(g * step)}" class="ch-grid"/>
    <text x="${L - 8}" y="${y(g * step) + 4}" text-anchor="end" class="ch-ax">${cents ? short(g * step) : g * step}</text>`).join('');
  const barW = Math.min(bw * 0.7, 44);
  const bars = points.map((p, i) => {
    const v = value(p) || 0; const x = L + i * bw + (bw - barW) / 2; const w = barW;
    const top2 = y(v); const h = H - B - top2; const r = Math.min(3, w / 2, h);
    return h > 0 ? `<path d="M${x},${H - B} V${top2 + r} q0,-${r} ${r},-${r} h${w - 2 * r} q${r},0 ${r},${r} V${H - B} Z" class="ch-bar"/>` : '';
  }).join('');
  const n = points.length;
  const every = unit === 'day' ? (n > 21 ? 7 : n > 10 ? 3 : 1) : n > 14 ? 3 : n > 7 ? 2 : 1;
  const last = n - 1;
  const labels = points.map((p, i) => ((i % every === 0 && last - i >= every / 2) || i === last
    ? `<text x="${L + i * bw + bw / 2}" y="${H - 7}" text-anchor="middle" class="ch-ax">${esc(bucketLabel(p.key, unit))}</text>` : '')).join('');
  const hits = points.map((p, i) => `<rect x="${L + i * bw}" y="${T}" width="${bw}" height="${H - B - T}" fill="transparent" data-i="${i}"/>`).join('');
  return `<div class="ch-wrap" data-chart="${esc(label)}"><svg viewBox="0 0 ${W} ${H}" width="100%" role="img" aria-label="${esc(label)}">
    ${grid}${bars}${labels}${hits}</svg><div class="ch-tip" hidden></div></div>`;
}

function wireChart(root, name, points, tipHtml) {
  const wrap = root.querySelector(`[data-chart="${name}"]`);
  if (!wrap) return;
  const tip = $('.ch-tip', wrap);
  wrap.addEventListener('mousemove', (e) => {
    const hit = e.target.closest('[data-i]');
    if (!hit) return;
    tip.innerHTML = tipHtml(points[Number(hit.dataset.i)]);
    tip.hidden = false;
    const box = wrap.getBoundingClientRect(); const hb = hit.getBoundingClientRect();
    tip.style.left = `${Math.min(box.width - tip.offsetWidth, Math.max(0, hb.left - box.left + hb.width / 2 - tip.offsetWidth / 2))}px`;
    tip.style.top = '0px';
  });
  wrap.addEventListener('mouseleave', () => { tip.hidden = true; });
}

const ATTENTION = [
  ['service', 'Service Failures', 'server'], ['integration', 'Integration Failures', 'link'],
  ['payment', 'Failed Payments', 'card'], ['suspended', 'Suspended Accounts', 'alert'],
  ['trial', 'Expiring Trials', 'clock'], ['onboarding', 'Failed Onboarding', 'device'],
  ['unverified', 'Unverified Accounts', 'mail'],
];
const ACT_ICON = { registered: ['acc', 'users'], signup: ['mute', 'mail'], verified: ['ok', 'check'], service: ['acc', 'card'],
  package: ['acc', 'trophy'], trial: ['warn', 'clock'], integration: ['ok', 'link'], suspended: ['bad', 'alert'],
  reactivated: ['ok', 'check'], closed: ['bad', 'alert'], cancel: ['warn', 'card'], paying: ['ok', 'money'],
  payment: ['ok', 'money'], payment_failed: ['bad', 'money'] };
const SVC_STATUS_CORE = { ...SVC_STATUS };
const ms = (v) => (v == null ? '—' : v < 1 ? '<1 ms' : `${Math.round(v)} ms`);

const TIP = {
  availability: "Share of the checks Core ran that got an answer, over the last 30 days. When Core itself is down no checks are recorded, so those gaps are not counted; a network fault on Core's side counts against the service.",
  response: 'The 95th percentile of answered checks in the last 24 hours (the slowest 1 in 20), with the average below. Checks that got no answer are left out of both and counted separately.',
  failed: '5xx answers out of the requests this process handled in the last 24 hours. Health and metrics polls are not counted. The count is per process, not combined across workers, and starts again when the process restarts. 401 and 403 answers are not failures but are shown apart.',
  jobs: 'Pending and running are right now. The age shows how long the oldest punch has been waiting to be written to Odoo, which tells you whether writes are blocked. Failed counts the last 24 hours.',
};
const DELAYED_MS = 30 * 60 * 1000;
const clock = (v) => new Date(v).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });

function dataSpan(days) {
  if (days == null || days >= 29.5) return '';
  if (days < 1) { const h = Math.max(1, Math.round(days * 24)); return `${h} hour${h === 1 ? '' : 's'} of data`; }
  const d = Math.round(days);
  return `${d} day${d === 1 ? '' : 's'} of data`;
}

function serviceHealthRows(services) {
  return services.map((sv) => {
    const [tone, label] = SVC_STATUS_CORE[sv.status] || ['mute', sv.status];
    const live = !['not_deployed', 'disabled'].includes(sv.status);
    const req = sv.requests;
    const failedReq = !req ? '—' : `<span class="${req.failed ? 'bad-t' : ''}"><b>${req.failed.toLocaleString()}</b></span> <span class="muted">of ${req.total.toLocaleString()}</span>${
      req.auth_failed ? `<small>${req.auth_failed.toLocaleString()} sign-in error${req.auth_failed === 1 ? '' : 's'}</small>` : ''}${
      req.started_at && req.since_minutes != null && req.since_minutes < 24 * 60 ? `<small>Since restart at ${esc(clock(req.started_at))}</small>` : ''}`;
    const j = sv.jobs;
    const oldest = j && j.pending && j.oldest_pending_at ? new Date(j.oldest_pending_at) : null;
    const stuck = oldest && Date.now() - oldest.getTime() > DELAYED_MS;
    const jobs = !j ? '—' : `${j.pending ?? 0} pending · ${j.running ?? 0} running${
      oldest ? `<small class="${stuck ? 'bad-t' : ''}">Oldest waiting ${esc(fmtAgo(j.oldest_pending_at).replace(' ago', ''))}</small>` : ''}
      <small class="${j.failed_24h ? 'bad-t' : ''}">${j.failed_24h ?? 0} failed (24 h)</small>`;
    const sch = sv.scheduler;
    const schedule = sch && sch.enabled === false ? tag('mute', 'Off') : sch ? `${tag(sch.ok ? 'ok' : 'bad', sch.ok ? 'Running' : 'Stopped')}<small>${sch.last_run_at ? `Last ${esc(fmtAgo(sch.last_run_at))}` : 'Never ran'}${
      sch.next_run_at ? ` · next ${esc(clock(sch.next_run_at))}` : ''}</small>` : '—';
    const db = sv.database ? `${tag(sv.database.ok ? 'ok' : 'bad', sv.database.ok ? 'OK' : 'Failed')}<small>${sv.database.ok ? esc(ms(sv.database.latency_ms)) : ''}</small>` : '—';
    const span = dataSpan(sv.availability_days);
    const avail = sv.availability_pct == null ? '—' : `<b class="${sv.availability_pct < 99 ? 'bad-t' : ''}">${pct(sv.availability_pct)}</b>${span ? `<small>${esc(span)}</small>` : ''}`;
    const p95 = sv.p95_latency_ms ?? sv.latency_ms;
    const latency = `<b>${ms(p95)}</b>${
      sv.avg_latency_ms != null ? `<small>avg ${esc(ms(sv.avg_latency_ms))}</small>` : ''}${
      sv.failed_checks_24h ? `<small class="bad-t">${sv.failed_checks_24h} of ${sv.checks_24h} failed</small>` : ''}`;
    const reason = sv.status_reason && sv.status !== 'operational' ? `<small class="why-line${tone === 'bad' ? ' bad-t' : ''}" title="${esc(sv.status_reason)}">${esc(sv.status_reason)}</small>` : '';
    return `<tr class="click" data-health="${esc(sv.slug)}" tabindex="0" title="Open ${esc(sv.name)} history">
      <td><b>${esc(sv.name)}</b>${sv.version ? `<small>v${esc(sv.version)}</small>` : ''}</td>
      <td>${tag(tone, label)}${reason}</td>
      <td class="num" ${live ? `title="${esc(TIP.availability)}"` : ''}>${live ? avail : '—'}</td>
      <td class="num" ${live ? `title="${esc(TIP.response)}"` : ''}>${live ? latency : '—'}</td>
      <td class="num" ${live ? `title="${esc(TIP.failed)}"` : ''}>${live ? failedReq : '—'}</td>
      <td ${live ? `title="${esc(TIP.jobs)}"` : ''}>${live ? jobs : '—'}</td><td>${live ? schedule : '—'}</td><td>${live ? db : '—'}</td></tr>`;
  }).join('');
}

/** The Service Health dialog: this row's figures, and for a service its last 24 hours of checks. */
async function serviceHealthDialog(sv) {
  const hist = await api.get(`/staff/services/${encodeURIComponent(sv.slug)}/health-history`).catch(() => null);
  const bar = (h) => {
    const none = h.availability_pct == null;
    const cls = none ? 'none' : h.availability_pct >= 99 ? 'ok' : h.availability_pct >= 50 ? 'warn' : 'bad';
    const tipText = none ? `${clock(h.start)}: no checks` : `${clock(h.start)}: ${h.availability_pct}% of ${h.checks} checks answered${h.avg_latency_ms != null ? `, ${h.avg_latency_ms} ms on average` : ''}`;
    return `<i class="hb ${cls}" title="${esc(tipText)}"></i>`;
  };
  const dur = (o) => {
    if (!o.until) return 'still failing';
    const mins = Math.max(1, Math.round((new Date(o.until) - new Date(o.from)) / 60000));
    return mins >= 60 ? `${Math.floor(mins / 60)} h ${mins % 60} min` : `${mins} min`;
  };
  const facts = [
    ['Status', `${tag(...(SVC_STATUS_CORE[sv.status] || ['mute', sv.status]))}${sv.status_reason && sv.status !== 'operational' ? ` <span class="muted">${esc(sv.status_reason)}</span>` : ''}`],
    ['Availability (30 days)', sv.availability_pct == null ? '—' : `${pct(sv.availability_pct)}${dataSpan(sv.availability_days) ? ` · ${esc(dataSpan(sv.availability_days))}` : ''}`],
    ['Response (24 h)', sv.p95_latency_ms == null ? '—' : `p95 ${esc(ms(sv.p95_latency_ms))} · avg ${esc(ms(sv.avg_latency_ms))}`],
    ['Failed requests (24 h)', sv.requests ? `${sv.requests.failed.toLocaleString()} of ${sv.requests.total.toLocaleString()}${sv.requests.auth_failed ? ` · ${sv.requests.auth_failed.toLocaleString()} sign-in errors` : ''}` : '—'],
    ['Database', sv.database ? `${sv.database.ok ? 'OK' : 'Failed'}${sv.database.ok ? ` · ${esc(ms(sv.database.latency_ms))}` : ''}` : '—'],
  ];
  const body = `<dl class="hd-facts">${facts.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${v}</dd>`).join('')}</dl>
    ${!hist || !hist.checks ? '<p class="muted hd-note">No checks in the last 24 hours.</p>'
      : `<h3 class="hd-h">Last 24 Hours</h3><div class="hd-strip">${hist.hours.map(bar).join('')}</div>
        <div class="hd-axis"><span>24 h ago</span><span>Now</span></div>
        <h3 class="hd-h">Outages</h3>${hist.outages.length ? `<ul class="hd-outages">${hist.outages.map((o) => `<li><b>${esc(fmtAgo(o.from))}</b><span>${esc(dur(o))} · ${o.checks} failed check${o.checks === 1 ? '' : 's'}</span></li>`).join('')}</ul>`
          : '<p class="muted hd-note">No failed checks in the last 24 hours.</p>'}`}`;
  dialog({ title: `${sv.name} Health`, wide: true, submitLabel: 'Done', body });
}

/** Core reports on itself: it can't be checked from outside, and if it is down this page doesn't load. */
function coreHealthCard(core) {
  const [tone, label] = SVC_STATUS_CORE[core.status] || ['mute', core.status];
  const req = core.requests;
  const sch = core.scheduler;
  const cell = (k, v, note = '') => `<div class="ch-cell"><span>${esc(k)}</span><b>${v}</b>${note ? `<small>${note}</small>` : ''}</div>`;
  const sweep = sch && sch.enabled === false ? tag('mute', 'Off') : sch ? tag(sch.ok ? 'ok' : 'bad', sch.ok ? 'Running' : 'Stopped') : '—';
  const sweepNote = !sch || sch.enabled === false ? '' : `${sch.last_run_at ? `Last ran ${esc(fmtAgo(sch.last_run_at))}` : 'Has not run yet'}${sch.next_run_at ? ` · next ${esc(clock(sch.next_run_at))}` : ''}`;
  const since = req && req.started_at && req.since_minutes < 24 * 60 ? `Since restart at ${esc(clock(req.started_at))}` : 'Last 24 hours';
  return `<div class="card a-core"><div class="card-head"><h2>Core Health</h2></div>
    <div class="ch-row">
      ${cell('Status', `${tag(tone, label)}`, core.status_reason && core.status !== 'operational' ? esc(core.status_reason) : '')}
      ${cell('Database', core.database ? `${tag(core.database.ok ? 'ok' : 'bad', core.database.ok ? 'OK' : 'Failed')} <span class="muted">${core.database.ok ? esc(ms(core.database.latency_ms)) : ''}</span>` : '—')}
      ${cell('Background Sweep', sweep, sweepNote)}
      ${cell('Failed Requests', req ? `<span class="${req.failed ? 'bad-t' : ''}">${req.failed.toLocaleString()}</span> <span class="muted">of ${req.total.toLocaleString()}</span>` : '—', req ? since : '')}
      ${cell('Sign-In Errors', req ? `<span>${(req.auth_failed || 0).toLocaleString()}</span>` : '—', req ? '401 and 403 answers' : '')}
    </div></div>`;
}

async function renderOverview(mount) {
  const o = await api.get(`/staff/overview?period=${overviewRange}`);
  const k = o.kpis;
  const r = o.range;
  const incidents = [...(!o.stripe.enabled ? [{ key: 'stripe', severity: 'medium', category: 'payment', issue: 'Online payments are off',
    detail: 'Add Stripe keys so customers can pay.', service: 'Payments', href: '#/payments' }] : []), ...o.incidents];
  const byCat = Object.fromEntries(ATTENTION.map(([key]) => [key, incidents.filter((i) => i.category === key)]));
  const urgent = incidents.filter((i) => ['critical', 'high'].includes(i.severity)).length;
  const rev = o.revenue.summary;
  const unit = o.acquisition.unit;
  const mixTotal = MIX.reduce((n, [key]) => n + (o.mix[key] || 0), 0);
  const mixShown = MIX.filter(([key]) => o.mix[key] || key !== 'no_service');
  const funnelTop = Math.max(1, ...o.funnel.map((f) => f.value));
  // No comparison yet means no line at all: five cards saying so is noise.
  const kpiNote = (m, up = true, extra = '', fmt) => [extra, compare(m, up, fmt)].filter(Boolean).join(' · ');
  const trialsRunning = k.trial_conversion.running;
  const revStat = (label, value, tone = '') => `<div class="rv-stat"><span>${esc(label)}</span><b class="${tone}">${value}</b></div>`;

  const actRow = (a) => {
    const [tone, icon] = ACT_ICON[a.kind] || ['mute', 'check'];
    const extra = a.amount_cents != null ? money(a.amount_cents, a.currency || undefined) : a.mrr_cents != null ? `${money(a.mrr_cents)}/mo` : a.detail || '';
    return `<li${a.account_id ? ` class="click" data-account="${esc(a.account_id)}"` : ''}><span class="al-ic ${tone}">${kpiIcon(icon)}</span>
      <div><b>${esc(a.title)}</b>${extra ? ` <span class="muted">${esc(extra)}</span>` : ''}<small>${esc(a.account || '')}</small></div>
      <time>${esc(fmtAgo(a.at))}</time></li>`;
  };
  const SHOW_ACT = 8;
  // The service under each name only helps when customers use different ones.
  const variedServices = new Set(o.recent_customers.filter((c) => c.services.length).map((c) => c.services.join(','))).size > 1;

  mount.innerHTML = `<div class="ov">
    <div class="ov-bar">
      <div class="seg" role="group" aria-label="Date range">${RANGES.map(([key, label]) =>
        `<button type="button" data-range="${key}" class="${key === r.key ? 'on' : ''}">${esc(label)}</button>`).join('')}</div>
      <a href="#attention" class="ov-attn ${urgent ? 'bad' : incidents.length ? 'warn' : 'ok'}">${kpiIcon('alert')}
        ${incidents.length ? `${incidents.length} need attention` : 'Nothing needs attention'}</a>
    </div>

    <div class="kpis ov-kpis">
      ${kpi({ icon: 'users', tone: 'acc', label: 'New Customers', value: k.new_customers.value,
        note: kpiNote(k.new_customers, true, k.new_customers.closed ? `${k.new_customers.closed} since closed` : ''), href: '#/accounts' })}
      ${kpi({ icon: 'check', tone: 'ok', label: 'Active Customers', value: k.active_customers.value, note: kpiNote(k.active_customers), href: '#/accounts' })}
      ${kpi({ icon: 'percent', tone: 'acc', label: 'Trial Conversion', value: pct(k.trial_conversion.value),
        title: 'Of the free trials that ended in this period, the share that became paying customers. Trials still running are not counted yet.',
        note: kpiNote(k.trial_conversion, true, [k.trial_conversion.value == null ? 'No trials ended yet'
          : `${k.trial_conversion.converted} of ${k.trial_conversion.converted + k.trial_conversion.ended_unpaid} ended`,
        trialsRunning ? `${trialsRunning} still running` : ''].filter(Boolean).join(' · ')) })}
      ${kpi({ icon: 'money', tone: 'ok', label: 'MRR', value: money(k.mrr.value), note: kpiNote(k.mrr, true, '', (c) => money(c)), href: '#/payments' })}
      ${kpi({ icon: 'users', tone: k.churn.value ? 'warn' : 'acc', label: 'Churn Rate', value: k.churn.value == null ? 'N/A' : pct(k.churn.value),
        title: k.churn.value == null ? (k.churn.why || 'Not enough history yet') : 'Customers who stopped paying in this period, as a share of those paying at its start.',
        note: kpiNote(k.churn, false, k.churn.base ? `${k.churn.churned} of ${k.churn.base}` : k.churn.churned ? `${k.churn.churned} stopped paying` : '') })}
      ${kpi({ icon: 'card', tone: k.outstanding.value ? 'bad' : 'ok', label: 'Outstanding Payments', value: money(k.outstanding.cents),
        note: kpiNote(k.outstanding, false, `${k.outstanding.value} account${k.outstanding.value === 1 ? '' : 's'}`, (n) => `${n} account${n === 1 ? '' : 's'}`), href: '#/payments' })}
      ${kpi({ icon: 'trophy', tone: 'acc', label: 'Open Leads', value: k.open_leads.value, note: kpiNote(k.open_leads), href: '#/leads' })}
      ${kpi({ icon: 'link', tone: k.failed_integrations.value ? 'bad' : 'ok', label: 'Failed Integrations', value: k.failed_integrations.value,
        note: kpiNote(k.failed_integrations, false) })}
    </div>

    <div class="ov-main">
      <div class="card a-acq"><div class="card-head"><h2>Customer Acquisition</h2>
        <span class="hint">${o.acquisition.total} new${o.acquisition.previous != null ? ` · ${o.acquisition.previous} in the ${esc(r.compare)}` : ''}</span></div>
        ${bucketBars(o.acquisition.buckets, { unit, value: (p) => p.new, label: 'acq', height: 330 })}</div>
      <div class="card a-rev"><div class="card-head"><h2>Revenue Overview</h2>
        <span class="hint">${rev.growth_pct != null ? `${rev.growth_pct >= 0 ? '▲' : '▼'} ${pct(Math.abs(rev.growth_pct))} since ${esc(rev.growth_basis)}` : ''}</span></div>
        <div class="rv-stats">
          ${revStat('MRR', esc(money(rev.mrr_cents)))}${revStat('Net New', `${rev.net_new_cents < 0 ? '−' : '+'}${esc(money(Math.abs(rev.net_new_cents)))}<small>/mo</small>`,
            rev.net_new_cents > 0 ? 'ok-t' : rev.net_new_cents < 0 ? 'bad-t' : '')}
          ${revStat('Growth', rev.growth_pct == null ? '—' : `${rev.growth_pct >= 0 ? '+' : '−'}${pct(Math.abs(rev.growth_pct))}`,
            rev.growth_pct == null ? '' : rev.growth_pct >= 0 ? 'ok-t' : 'bad-t')}
          ${revStat('New revenue', `+${esc(money(rev.new_cents + rev.expansion_cents))}<small>/mo</small>`, 'ok-t')}
          ${revStat('Churned revenue', `−${esc(money(rev.churned_cents + rev.contraction_cents))}<small>/mo</small>`, rev.churned_cents + rev.contraction_cents ? 'bad-t' : '')}
          ${revStat('Failed payments', `${rev.failed_payments}${rev.failed_cents ? ` <small>${esc(money(rev.failed_cents))}</small>` : ''}`, rev.failed_payments ? 'bad-t' : '')}
        </div>
        ${o.revenue.buckets.length >= (unit === 'day' ? 3 : 2)
    ? bucketBars(o.revenue.buckets, { unit, value: (p) => p.mrr_cents, label: 'mrr', cents: true })
    : `<p class="rv-empty">Revenue history builds up day by day${rev.history_from ? ` (tracking since ${esc(fmtDate(rev.history_from))})` : ''}. The chart appears once there are a few days to show.</p>`}
      </div>
      <div class="card a-reg"><div class="card-head"><h2>Recent Registrations</h2><a href="#/accounts">Accounts →</a></div>
        ${o.recent_customers.length ? `<div class="table-wrap"><table class="compact reg-table">
          <thead><tr><th>Customer</th><th>Subscription</th><th>Status</th><th>Registered</th></tr></thead>
          <tbody>${o.recent_customers.slice(0, SHOW_ACT).map((c) => `<tr${c.id ? ` class="click" data-account="${esc(c.id)}"` : ''}>
            <td class="cust-n" title="${esc(c.name)}">${esc(c.name)}${variedServices && c.services.length ? `<small class="muted" style="display:block">${esc(c.services.join(', '))}</small>` : ''}</td>
            <td>${c.subscription ? tag(...SUB_LABEL[c.subscription]) : '—'}</td>
            <td>${c.status === 'unverified' ? tag('mute', 'Email Not Verified') : statusPill(c.status)}</td>
            <td class="nowrap">${esc(fmtDate(c.created_at))}</td></tr>`).join('')}</tbody></table></div>`
          : empty('No Customers Yet', '')}</div>
      <div class="card a-mix"><div class="card-head"><h2>Subscription Overview</h2><span class="hint">${mixTotal} customer${mixTotal === 1 ? '' : 's'}</span></div>
        ${mixTotal ? `<div class="mix" role="img" aria-label="Subscriptions">${mixShown.filter(([key]) => o.mix[key]).map(([key, label, color]) =>
          `<span style="flex:${o.mix[key]};background:${color}" title="${esc(label)}: ${o.mix[key]}"></span>`).join('')}</div>
          <ul class="mix-leg">${mixShown.map(([key, label, color]) => `<li><span class="sw" style="background:${color}"></span>${esc(label)}
            <b>${o.mix[key] || 0}</b><em>${Math.round(((o.mix[key] || 0) * 100) / mixTotal)}%</em></li>`).join('')}</ul>` : empty('No Customers Yet', '')}
        ${o.mix_closed ? `<div class="hint" style="margin-top:8px">${o.mix_closed} closed account${o.mix_closed === 1 ? '' : 's'} not included.</div>` : ''}</div>
      <div class="card a-sal"><div class="card-head"><h2>Sales Overview</h2><a href="#/leads">Leads →</a></div>
        <ol class="funnel">${o.funnel.map((f) => `<li title="${f.pct_top != null ? `${pct(f.pct_top)} of those who registered` : ''}"><span class="fn-l">${esc(f.label)}</span>
          <span class="fn-bar"><span style="width:${Math.max(f.value ? 2 : 0, (f.value * 100) / funnelTop)}%"></span></span>
          <b>${f.value}</b><em>${f.pct_prev == null ? '' : pct(f.pct_prev)}</em></li>`).join('')}</ol>
        <div class="fn-note">Each percentage is of the stage before. Subscribed means a free trial or a paid plan.${
          o.funnel_closed ? ` ${o.funnel_closed} of those who registered have since closed their account.` : ''}</div>
        <div class="fn-foot"><span>Open leads <b>${o.leads.open}</b> <span class="muted">(tracked separately)</span></span>
          <span>New ${o.leads.by_stage.new} · Contacted ${o.leads.by_stage.contacted} · Qualified ${o.leads.by_stage.qualified}</span>
          <span>Pipeline <b>${o.leads.pipeline_cents ? `${esc(money(o.leads.pipeline_cents))}/mo` : '—'}</b></span></div></div>
      <div class="card a-att" id="attention"><div class="card-head"><h2>Needs Attention</h2><span class="hint">${incidents.length} open</span></div>
        <ul class="attn-list">${ATTENTION.map(([key, label, icon]) => {
          const items = byCat[key];
          const worst = items.some((i) => ['critical', 'high'].includes(i.severity)) ? 'bad' : items.some((i) => i.severity === 'medium') ? 'warn' : items.length ? 'acc' : 'ok';
          const names = [...new Set(items.map((i) => i.account || i.service || i.issue))];
          return `<li${items.length ? ` class="click" data-cat="${key}"` : ''}><span class="al-ic ${worst}">${kpiIcon(icon)}</span>
            <div><b>${esc(label)}</b><small>${items.length ? esc(names.slice(0, 2).join(', ') + (names.length > 2 ? ` +${names.length - 2}` : '')) : 'All clear'}</small></div>
            <span class="attn-n ${items.length ? worst : ''}">${items.length}</span></li>`;
        }).join('')}</ul></div>
      <div class="card a-act"><div class="card-head"><h2>Recent Activity</h2>${o.activity.length > SHOW_ACT ? '<a href="#" id="allActivity">View All →</a>' : ''}</div>
        ${o.activity.length ? `<ul class="act-feed">${o.activity.slice(0, SHOW_ACT).map(actRow).join('')}</ul>` : empty('Nothing Yet', '')}</div>
      ${coreHealthCard(o.core)}
      <div class="card a-hlt"><div class="card-head"><h2>Service Health</h2><span class="hint">Checked ${esc(fmtAgo(o.checked_at))}</span></div>
      <div class="table-wrap"><table class="health compact">
      <colgroup><col class="c-svc"><col class="c-stat"><col class="c-av"><col class="c-rsp"><col class="c-req"><col class="c-job"><col class="c-sch"><col class="c-db"></colgroup>
      <thead><tr><th>Service</th><th>Status</th><th class="num">Availability<small>30 days</small></th><th class="num">Response<small>p95 · 24 h</small></th>
        <th class="num">Failed Requests<small>5xx · 24 h</small></th><th>Background Jobs<small>now · failed 24 h</small></th><th>Scheduler</th><th>Database</th></tr></thead>
      <tbody>${serviceHealthRows(o.services)}</tbody></table></div>
      <div class="hint" style="margin-top:8px">Aggregate across all customers; no per-customer data. Select a row for its last 24 hours.
        Availability is the share of Core's own checks that got an answer, so it can't see a time when Core was down. Request counts are per process and restart with it. Hover a figure for its definition.</div></div>
    </div>
  </div>`;

  wireChart(mount, 'acq', o.acquisition.buckets, (p) => `<b>${esc(bucketLabel(p.key, unit, true))}</b><span>${p.new} new customer${p.new === 1 ? '' : 's'}</span>`);
  wireChart(mount, 'mrr', o.revenue.buckets, (p) => `<b>${esc(bucketLabel(p.key, unit, true))}</b><span>MRR ${p.mrr_cents == null ? '—' : esc(money(p.mrr_cents))}</span>
    ${p.new_cents ? `<span>New +${esc(money(p.new_cents))}</span>` : ''}${p.churned_cents ? `<span>Churned −${esc(money(p.churned_cents))}</span>` : ''}`);
  mount.onkeydown = (e) => {
    const health = e.key === 'Enter' && e.target.closest?.('[data-health]');
    if (health) health.click();
  };
  mount.onclick = (e) => {
    const range = e.target.closest('[data-range]');
    if (range) { overviewRange = range.dataset.range; guard(() => renderOverview(mount)); return; }
    if (e.target.closest('.ov-attn')) { e.preventDefault(); $('#attention', mount).scrollIntoView({ behavior: 'smooth', block: 'center' }); return; }
    if (e.target.closest('#allActivity')) {
      e.preventDefault();
      dialog({ title: 'Recent Activity', wide: true, submitLabel: 'Done', body: `<ul class="act-feed">${o.activity.map(actRow).join('')}</ul>` });
      return;
    }
    const cat = e.target.closest('[data-cat]');
    if (cat) {
      const [, label] = ATTENTION.find(([key]) => key === cat.dataset.cat);
      dialog({ title: label, wide: true, submitLabel: 'Done',
        body: `<ul class="alerts">${byCat[cat.dataset.cat].map(incidentRow).join('')}</ul>` });
      return;
    }
    const health = e.target.closest('[data-health]');
    if (health) { const sv = o.services.find((x) => x.slug === health.dataset.health); if (sv) guard(() => serviceHealthDialog(sv)); return; }
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
const ACTIVITY = { active: ['ok', 'Active'], idle: ['warn', 'Idle'], not_started: ['mute', 'Not Started'] };
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
  if (kind === 'activity') { const [t, l] = ACTIVITY[v] || ['mute', v || '—']; return tag(t, l); }
  if (kind === 'onoff') return v == null ? '—' : v ? tag('ok', 'On') : tag('mute', 'Off');
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
    <td class="nowrap">${c.since ? esc(fmtDate(c.since)) : '—'}</td>
    <td class="nowrap">${c.billing ? tag(c.billing.tone || 'mute', c.billing.date ? `${c.billing.label} ${fmtDate(c.billing.date)}` : c.billing.label) : '—'}</td>
    ${cols.map((col) => `<td class="${col.kind === 'usage' || col.kind === 'number' ? 'nowrap' : ''}">${cell(col.kind, c.values[col.key])}</td>`).join('')}</tr>`;
  mount.innerHTML = `
    <div class="svc-head-row">${tag(tone, label)}<span class="muted">${esc(facts)}</span></div>
    ${sv.status === 'unavailable' ? banner('The Service Is Not Answering', sv.error || 'Its figures will show once it answers again.', 'bad') : ''}
    ${!sv.has_dashboard && sv.status !== 'unavailable' ? banner('No Figures From This Service Yet', 'It does not report a dashboard; only its customers are listed.', 'warn') : ''}
    <div class="kpis" style="grid-template-columns:repeat(${Math.min(5, Math.max(1, d.tiles.length))},minmax(0,1fr))">
      ${d.tiles.map((t) => kpi({ icon: t.icon || 'server', tone: t.tone || 'acc', label: t.label,
        value: typeof t.value === 'number' ? t.value.toLocaleString() : (t.value ?? '—') })).join('')}
    </div>
    ${d.series ? `<div class="ov-row2">
      ${d.series ? `<div class="card"><div class="card-head"><h2>${esc(d.series.title || 'Activity')}</h2>${d.series.subtitle ? `<span class="hint">${esc(d.series.subtitle)}</span>` : ''}</div>${dailyBars(d.series)}</div>` : ''}
      <div class="card"><div class="card-head"><h2>Needs Attention</h2></div>
        ${d.attention.length ? `<ul class="alerts">${d.attention.map((i) => incidentRow({ ...i, issue: i.title, service: null,
          severity: SEVERITY[i.severity] ? i.severity : 'medium' })).join('')}</ul>` : empty('Nothing Needs Attention', '')}</div>
    </div>` : d.attention.length ? `<div class="card" style="margin-top:14px"><div class="card-head"><h2>Needs Attention</h2></div>
        <ul class="alerts">${d.attention.map((i) => incidentRow({ ...i, issue: i.title, service: null,
          severity: SEVERITY[i.severity] ? i.severity : 'medium' })).join('')}</ul></div>` : ''}
    <div class="card" style="margin-top:14px"><div class="card-head"><h2>Customers</h2>
      <input type="search" id="svcSearch" placeholder="Search customers" style="max-width:260px"></div>
      ${d.customers.length ? `<div class="table-wrap"><table>
        <thead><tr><th>Customer</th><th>Plan</th><th>Status</th><th>Since</th><th>Billing</th>${cols.map((c) => `<th>${esc(c.label)}</th>`).join('')}</tr></thead>
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
const filters = { q: '', status: '', service: '', billing: '' };

const BILLING_FILTER = [['paying', 'Paying'], ['trial', 'Trial'], ['trial_ending', 'Trial Ending'], ['past_due', 'Payment Due'], ['none', 'No Service']];
const SHOWN_SERVICES = 2;

/** One service in the list: a state dot and its name. Billing says what happens next. */
function serviceLine(r) {
  const tone = r.status === 'trialing' ? 'trialing' : r.status === 'active' ? 'ok' : r.status === 'past_due' || r.status === 'suspended' ? 'bad' : 'mute';
  const word = r.status === 'active' && !r.billed ? 'Active' : r.status === 'active' ? 'Paying' : STATUS_LABEL[r.status] || r.status;
  return `<div class="svc-line" title="${esc(`${r.service.name}: ${word}`)}"><i class="dot ${tone}" aria-label="${esc(word)}"></i><span>${esc(r.service.name)}</span></div>`;
}

// What needs a look comes first, so the two shown are the two that matter.
function serviceRank(r) {
  const left = r.trial_ends_at ? new Date(r.trial_ends_at).getTime() : Infinity;
  return r.status === 'past_due' ? [0, 0] : r.status === 'trialing' ? [1, left] : [2, 0];
}

function perMonth(a) {
  const paying = a.paying_cents, trial = a.after_trial_cents;
  if (!paying && !trial) return '<span class="muted">—</span>';
  return `${paying ? money(paying) : '<span class="muted">—</span>'}${trial ? `<small class="muted">${paying ? '+ ' : ''}${esc(money(trial))} after trial</small>` : ''}`;
}

const dayMonth = (v) => new Date(v).toLocaleDateString(undefined, { day: 'numeric', month: 'short' });

/** The Billing pill with its next event, and under it how a paying account pays. */
function billingCell(a) {
  const b = a.billing;
  const label = b.key === 'paying' && b.renews_at ? `Paying · renews ${dayMonth(b.renews_at)}` : b.label;
  const under = b.note || (b.key === 'paying' ? (a.pays_online ? 'Paid via Stripe' : 'Not billed online') : '');
  return `${tag(b.tone, label)}${under ? `<small class="muted">${esc(under)}</small>` : ''}`;
}

const SORTS = {
  name: (x, y) => x.name.localeCompare(y.name),
  billing: (x, y) => x.billing.rank - y.billing.rank || (x.billing.trial_days ?? 999) - (y.billing.trial_days ?? 999),
  permonth: (x, y) => x.paying_cents - y.paying_cents,
  since: (x, y) => new Date(x.created_at) - new Date(y.created_at),
};
// Default: what needs a look first (payment due, suspended, trials ending soonest), newest first within a tie.
const sorting = { key: 'billing', dir: 1 };
const sortHead = (key, label, cls = '') => `<th class="sort ${cls}" data-sort="${key}" tabindex="0" aria-sort="${sorting.key === key ? (sorting.dir > 0 ? 'ascending' : 'descending') : 'none'}">${label}<i>${sorting.key === key ? (sorting.dir > 0 ? '▲' : '▼') : ''}</i></th>`;

async function renderAccounts(mount) {
  const svcs = await services();
  const qs = new URLSearchParams(Object.entries({ q: filters.q, status_filter: filters.status, service: filters.service, billing_filter: filters.billing })
    .filter(([, v]) => v)).toString();
  const rows = await api.get(`/staff/accounts${qs ? `?${qs}` : ''}`);
  rows.sort((x, y) => sorting.dir * SORTS[sorting.key](x, y) || new Date(y.created_at) - new Date(x.created_at));
  mount.innerHTML = `
    <div class="filterbar">
      <label class="fb-search"><svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>
        <input id="q" type="search" placeholder="Search company or email" value="${esc(filters.q)}" aria-label="Search accounts"></label>
      <select id="status" aria-label="Status"><option value="">All statuses</option>${['active', 'suspended', 'closed'].map((s) =>
        `<option value="${s}" ${filters.status === s ? 'selected' : ''}>${STATUS_LABEL[s]}</option>`).join('')}</select>
      <select id="service" aria-label="Service"><option value="">All services</option>${svcs.map((s) =>
        `<option value="${esc(s.slug)}" ${filters.service === s.slug ? 'selected' : ''}>${esc(s.name)}</option>`).join('')}</select>
      <select id="billing" aria-label="Billing"><option value="">All billing</option>${BILLING_FILTER.map(([v, l]) =>
        `<option value="${v}" ${filters.billing === v ? 'selected' : ''}>${l}</option>`).join('')}</select>
      <span class="fb-spacer"></span>
      <button class="primary" id="new">New Account</button>
    </div>
    <div class="card"><div class="table-wrap"><table class="accounts">
      <thead><tr>${sortHead('name', 'Account')}<th>Services</th><th>Status</th>${sortHead('billing', 'Billing')}${sortHead('permonth', 'Per Month', 'num')}${sortHead('since', 'Since')}</tr></thead>
      <tbody>${rows.map((a) => {
        const live = a.services.filter((r) => r.status !== 'cancelled')
          .sort((x, y) => { const [a1, a2] = serviceRank(x); const [b1, b2] = serviceRank(y); return a1 - b1 || a2 - b2; });
        return `<tr class="click" data-id="${esc(a.id)}">
        <td><strong>${esc(a.name)}</strong>${a.owner_email ? `<small class="muted">${esc(a.owner_email)}</small>` : ''}</td>
        <td>${live.slice(0, SHOWN_SERVICES).map(serviceLine).join('') || '<span class="muted">—</span>'}${
          live.length > SHOWN_SERVICES ? `<small class="muted" title="${esc(live.slice(SHOWN_SERVICES).map((r) => r.service.name).join(', '))}">+${live.length - SHOWN_SERVICES} more</small>` : ''}</td>
        <td>${a.status === 'active' ? '<i class="dot ok" title="Active" aria-label="Active"></i>' : statusPill(a.status)}</td>
        <td>${billingCell(a)}</td>
        <td class="num">${perMonth(a)}</td><td class="nowrap">${esc(fmtDate(a.created_at))}</td></tr>`;
      }).join('')
        || `<tr><td colspan="6">${empty('No Accounts Match', '')}</td></tr>`}</tbody>
    </table></div></div>`;
  let timer;
  $('#q', mount).addEventListener('input', (e) => {
    clearTimeout(timer);
    timer = setTimeout(() => { filters.q = e.target.value; renderAccounts(mount).then(() => { const q = $('#q', mount); q.focus(); q.setSelectionRange(q.value.length, q.value.length); }); }, 300);
  });
  $('#status', mount).addEventListener('change', (e) => { filters.status = e.target.value; renderAccounts(mount); });
  $('#service', mount).addEventListener('change', (e) => { filters.service = e.target.value; renderAccounts(mount); });
  $$('th[data-sort]', mount).forEach((th) => {
    const go = () => {
      const key = th.dataset.sort;
      sorting.dir = sorting.key === key ? -sorting.dir : 1;
      sorting.key = key;
      renderAccounts(mount);
    };
    th.addEventListener('click', go);
    th.addEventListener('keydown', (e) => { if (e.key === 'Enter') go(); });
  });
  $('#billing', mount).addEventListener('change', (e) => { filters.billing = e.target.value; renderAccounts(mount); });
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
            ${r.in_sync ? '' : `<button class="sm" data-resync="${esc(r.service.slug)}">Resend</button>`}</td></tr>`).join('')
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
      <ul class="audit-feed">${a.audit.map((e) => `<li><div><strong>${esc(e.action)}</strong>${e.detail ? ` <span class="muted">· ${esc(e.detail)}</span>` : ''}</div>
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

const SEV_FLOOR = { critical: 'Critical only', high: 'Critical and high', medium: 'Everything (critical, high and medium)' };

function alertsCard(al) {
  const c = al.settings;
  const to = [c.all_staff ? 'all active staff' : null, ...(c.emails || [])].filter(Boolean).join(', ') || 'nobody';
  return `<div class="card" style="margin-top:16px">
    <div class="card-head"><h2>Alerts</h2><div class="actions">
      <button class="sm" id="alertTest">Send Test Alert</button><button class="sm" id="alertEdit">Edit</button></div></div>
    <dl class="kv">
      <dt>Status</dt><dd>${c.enabled ? tag('ok', 'On') : tag('mute', 'Off')}</dd>
      <dt>Alert on</dt><dd>${esc(SEV_FLOOR[c.min_severity] || c.min_severity)}</dd>
      <dt>Email</dt><dd>${esc(to)}</dd>
      <dt>Webhook</dt><dd>${c.has_webhook ? esc(c.webhook_hint) : '—'}</dd>
      <dt>Reminders</dt><dd>${c.reminder_hours ? `every ${c.reminder_hours} h while open` : 'off'}</dd>
      <dt>Wait before alerting</dt><dd>${c.grace_minutes} min</dd>
      <dt>Open now</dt><dd>${al.open.length}</dd>
    </dl></div>`;
}

function wireAlerts(mount, al, reload) {
  $('#alertTest', mount)?.addEventListener('click', (e) => submitting(e.currentTarget, async () => {
    try { toast((await api.post('/staff/alerts/test')).message, 'ok'); } catch (error) { toast(error.message, 'bad'); }
  }));
  $('#alertEdit', mount)?.addEventListener('click', () => guard(async () => {
    const c = al.settings;
    const ok = await dialog({
      title: 'Alert Settings', submitLabel: 'Save', wide: true,
      body: `<div class="grid cols-2">
        ${field({ name: 'enabled', label: 'Alerts', value: String(c.enabled), boolean: true, required: true,
          options: [{ value: 'true', label: 'On' }, { value: 'false', label: 'Off' }] })}
        ${field({ name: 'min_severity', label: 'Alert on', value: c.min_severity, required: true,
          options: Object.entries(SEV_FLOOR).map(([value, label]) => ({ value, label })) })}
        ${field({ name: 'all_staff', label: 'Email all active staff', value: String(c.all_staff), boolean: true, required: true,
          options: [{ value: 'true', label: 'Yes' }, { value: 'false', label: 'No, only the addresses below' }] })}
        ${field({ name: 'emails', label: 'Also email', value: (c.emails || []).join(', '), placeholder: 'ops@yourcompany.com, oncall@…',
          help: 'Comma separated.' })}
        ${field({ name: 'webhook_url', label: 'Slack or Teams webhook', placeholder: c.has_webhook ? `Set (${c.webhook_hint}); leave blank to keep` : 'https://hooks.slack.com/services/…',
          help: 'Any webhook that takes {"text": "..."}. Type "none" to remove it.' })}
        ${field({ name: 'reminder_hours', label: 'Remind every (hours)', type: 'number', value: c.reminder_hours, required: true,
          help: '0 turns reminders off.' })}
        ${field({ name: 'grace_minutes', label: 'Wait before alerting (minutes)', type: 'number', value: c.grace_minutes, required: true,
          help: 'A problem that clears within this time is never announced.' })}
      </div>`,
      onSubmit: (form) => {
        const v = readForm(form);
        const body = { enabled: v.enabled, min_severity: v.min_severity, all_staff: v.all_staff,
          emails: String(v.emails || '').split(/[,\s]+/).filter(Boolean),
          reminder_hours: Number(v.reminder_hours || 0), grace_minutes: Number(v.grace_minutes || 0) };
        const hook = String(v.webhook_url || '').trim();
        if (hook.toLowerCase() === 'none') body.webhook_url = ''; else if (hook) body.webhook_url = hook;
        return api.put('/staff/alerts', body);
      },
    });
    if (ok) { toast('Alert settings saved.', 'ok'); reload(); }
  }));
}

async function renderTeam(mount) {
  const [rows, al] = await Promise.all([api.get('/staff/team'), api.get('/staff/alerts')]);
  mount.innerHTML = `<div class="card">
    <div class="card-head"><h2>Staff</h2><div class="actions"><button class="primary sm" id="add">Add Staff</button></div></div>
    <div class="table-wrap"><table><thead><tr><th>Name</th><th>Email</th><th>Last sign-in</th><th>Status</th></tr></thead>
    <tbody>${rows.map((s) => `<tr class="click" data-staff="${esc(s.id)}">
      <td><b>${esc(s.full_name || '—')}</b>${s.id === state.me.id ? ' <span class="hint">(you)</span>' : ''}${s.is_static ? ' <span class="pill mute">From settings</span>' : ''}</td>
      <td>${esc(s.email)}</td><td>${esc(s.last_login_at ? fmtAgo(s.last_login_at) : 'never')}</td>
      <td>${s.is_active ? '<span class="pill ok">Active</span>' : '<span class="pill mute">Disabled</span>'}</td></tr>`).join('')}</tbody>
    </table></div></div>${alertsCard(al)}`;
  wireAlerts(mount, al, () => renderTeam(mount));
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
// The per-service dashboard (renderServiceDashboard) is switched off for now:
// add { pattern: '/service/:slug', ... } here and the nav entries back to restore it.
const DETAIL = [{ pattern: '/accounts/:id', title: 'Account', render: renderAccount }];

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
    mountShell({
      nav: [{ label: 'Console', items: PAGES }],
      user: '',  // the top-bar chip already names the signed-in staff member
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
