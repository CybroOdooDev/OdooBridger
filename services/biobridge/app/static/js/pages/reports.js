/* Reports: proof that attendance reached Odoo, and what still needs a hand.
 * Four tabs over one date range; every table exports to CSV. */

import { api } from '../api.js';
import { $, empty, esc, loading, pill, readForm, stat, todayISO, triggerLabel } from '../ui.js';

const TABS = [
  { key: 'exceptions', label: 'Exceptions' },
  { key: 'terminals', label: 'Terminal Health' },
  { key: 'daily', label: 'Daily Summary' },
  { key: 'sync', label: 'Sync History' },
];

const PRESETS = () => {
  const d = new Date();
  const iso = (x) => `${x.getFullYear()}-${String(x.getMonth() + 1).padStart(2, '0')}-${String(x.getDate()).padStart(2, '0')}`;
  return [
    { label: 'Today', from: todayISO(), to: todayISO() },
    { label: 'Yesterday', from: todayISO(-1), to: todayISO(-1) },
    { label: 'Last 7 Days', from: todayISO(-6), to: todayISO() },
    { label: 'This Month', from: iso(new Date(d.getFullYear(), d.getMonth(), 1)), to: todayISO() },
    { label: 'Last 30 Days', from: todayISO(-29), to: todayISO() },
  ];
};

const DOWNLOAD = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
  + 'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 4v11M7 10l5 5 5-5M5 20h14"/></svg>';

const num = (v) => (v === null || v === undefined ? '—' : Number(v).toLocaleString());
const time = (v) => (v ? String(v).replace('T', ' ').slice(0, 16) : '—');
const secs = (ms) => (ms === null || ms === undefined ? '—' : `${(ms / 1000).toFixed(1)} s`);

/* --- CSV ---------------------------------------------------------------------- */
/* Each table keeps its columns and rows, so Export writes exactly what is shown. */
const tables = new Map();

function table(id, title, columns, rows, { note = '', blank = 'Nothing to show for this range.', show = Infinity } = {}) {
  tables.set(id, { title, columns, rows });
  // Long lists show their latest rows; Export CSV always writes them all.
  const shown = rows.slice(0, show);
  if (shown.length < rows.length) note = [note, `showing ${shown.length}, export for all`].filter(Boolean).join(' · ');
  return `
    <div class="card" style="margin-bottom:14px">
      <div class="card-head">
        <h2>${esc(title)} <span class="hint">${num(rows.length)}${note ? ` · ${esc(note)}` : ''}</span></h2>
        ${rows.length ? `<button class="sm" data-export="${esc(id)}">${DOWNLOAD} Export CSV</button>` : ''}
      </div>
      ${rows.length ? `
        <div class="scroll"><table>
          <thead><tr>${columns.map((c) => `<th class="${c.num ? 'num' : ''}">${esc(c.label)}</th>`).join('')}</tr></thead>
          <tbody>${shown.map((r) => `<tr>${columns.map((c) =>
            `<td class="${c.num ? 'num' : ''}${c.mono ? ' mono nowrap' : ''}">${c.html ? c.html(r) : esc(c.show ? c.show(r) : (r[c.key] ?? '—'))}</td>`).join('')}</tr>`).join('')}
          </tbody>
        </table></div>` : `<p class="hint" style="margin:0">${esc(blank)}</p>`}
    </div>`;
}

function exportCsv(id, range) {
  const t = tables.get(id);
  if (!t) return;
  const cell = (v) => {
    const s = v === null || v === undefined ? '' : String(v);
    return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  const lines = [t.columns.map((c) => cell(c.label)).join(',')]
    .concat(t.rows.map((r) => t.columns.map((c) => cell(c.csv ? c.csv(r) : (c.show ? c.show(r) : r[c.key]))).join(',')));
  const blob = new Blob([`﻿${lines.join('\r\n')}`], { type: 'text/csv;charset=utf-8' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `biobridge-${t.title.toLowerCase().replace(/[^a-z0-9]+/g, '-')}-${range.from}-to-${range.to}.csv`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

/* --- the four reports ---------------------------------------------------------- */
const STATE = {
  active: ['ok', 'Active'], silent: ['warn', 'Silent'], missing: ['bad', 'Missing'],
  no_punches: ['mute', 'No Punches Yet'], disabled: ['mute', 'Disabled'],
};
const statePill = (s) => { const [tone, label] = STATE[s] || ['mute', s]; return `<span class="pill ${tone}">${esc(label)}</span>`; };

function renderExceptions(d) {
  const t = d.totals;
  const shift = [
    { key: 'employee', label: 'Employee', show: (r) => r.employee || '—' },
    { key: 'emp_code', label: 'Badge', mono: true },
    { key: 'date', label: 'Date' },
    { key: 'check_in', label: 'Check in', show: (r) => time(r.check_in) },
    { key: 'check_out', label: 'Check out', show: (r) => time(r.check_out) },
    { key: 'terminal', label: 'Terminal', show: (r) => r.terminal || '—' },
    { key: 'odoo_id', label: 'Odoo ID', num: true },
  ];
  return `
    <div class="grid cols-4" style="margin-bottom:14px">
      ${stat({ label: 'Unmatched badges', value: num(t.unmatched_badges), tone: t.unmatched_badges ? 'warn' : '',
               note: `${num(t.unmatched_punches)} punches held`, href: '#/settings/odoo?show=unmapped' })}
      ${stat({ label: 'Failed punches', value: num(t.failed), tone: t.failed ? 'bad' : '', href: '#/activity?state=error' })}
      ${stat({ label: 'Held by plan', value: num(t.held), tone: t.held ? 'warn' : '', href: '#/activity?state=held' })}
      ${stat({ label: 'Missing check-outs', value: num(t.missing_check_out), tone: t.missing_check_out ? 'warn' : '',
               note: `open over ${d.max_shift_hours} h` })}
    </div>
    ${table('unmatched', 'Unmatched Badges', [
      { key: 'emp_code', label: 'Badge', mono: true },
      { key: 'device_name', label: 'Name on device', show: (r) => r.device_name || '—' },
      { key: 'punches', label: 'Punches', num: true },
      { key: 'first_punch', label: 'First punch', show: (r) => time(r.first_punch) },
      { key: 'last_punch', label: 'Last punch', show: (r) => time(r.last_punch) },
    ], d.unmatched, { blank: 'Every badge in this range is matched to an Odoo employee.' })}
    ${table('failed', 'Failed Punches', [
      { key: 'punch_time', label: 'Punch time', show: (r) => time(r.punch_time) },
      { key: 'employee', label: 'Employee', show: (r) => r.employee || '—' },
      { key: 'emp_code', label: 'Badge', mono: true },
      { key: 'terminal', label: 'Terminal', show: (r) => r.terminal || '—' },
      { key: 'attempts', label: 'Attempts', num: true },
      { key: 'error', label: 'Error', show: (r) => r.error || '—' },
    ], d.failed, { note: d.failed.length < t.failed ? `showing the latest ${d.failed.length}` : '',
                   blank: 'Odoo accepted every punch in this range.' })}
    ${t.held ? table('held', 'Held By Plan', [
      { key: 'terminal', label: 'Terminal' },
      { key: 'serial', label: 'Serial', mono: true },
      { key: 'punches', label: 'Punches', num: true },
      { key: 'oldest', label: 'Oldest', show: (r) => time(r.oldest) },
    ], d.held) : ''}
    ${table('missing', 'Missing Check-Outs', shift, d.missing_check_out,
      { blank: `No shift has stayed open longer than ${d.max_shift_hours} hours.` })}
    ${table('autoclosed', 'Closed By The Shift Limit', shift, d.auto_closed,
      { blank: 'No shift needed closing by the shift limit.' })}
    ${table('orphan', 'Check-Outs Without A Check-In', shift, d.orphan_check_out,
      { blank: 'Every check-out had a check-in.' })}`;
}

function renderTerminals(d) {
  const t = d.totals;
  return `
    <div class="grid cols-4" style="margin-bottom:14px">
      ${stat({ label: 'Terminals', value: num(t.terminals) })}
      ${stat({ label: 'Active', value: num(t.active), tone: 'ok' })}
      ${stat({ label: 'Silent', value: num(t.silent), tone: t.silent ? 'warn' : '', note: `no punch in ${d.silent_hours} h` })}
      ${stat({ label: 'Missing', value: num(t.missing), tone: t.missing ? 'bad' : '', note: 'no longer listed by its connection' })}
    </div>
    ${table('terminals', 'Terminals', [
      { key: 'name', label: 'Terminal' },
      { key: 'serial', label: 'Serial', mono: true },
      { key: 'connection', label: 'Connection', show: (r) => r.connection || '—' },
      { key: 'area', label: 'Area', show: (r) => r.area || '—' },
      { key: 'state', label: 'Status', html: (r) => statePill(r.state), csv: (r) => (STATE[r.state] || [, r.state])[1] },
      { key: 'last_punch', label: 'Last punch', show: (r) => time(r.last_punch) },
      { key: 'last_contact', label: 'Last contact', show: (r) => time(r.last_contact) },
      { key: 'punches', label: 'Punches in range', num: true, show: (r) => num(r.punches), csv: (r) => r.punches },
      { key: 'employees', label: 'Employees in range', num: true, show: (r) => num(r.employees), csv: (r) => r.employees },
    ], d.terminals, { blank: 'No terminals yet. Import them from Terminals.' })}`;
}

function renderDaily(d) {
  const t = d.totals;
  return `
    <div class="grid cols-4" style="margin-bottom:14px">
      ${stat({ label: 'Punches', value: num(t.punches) })}
      ${stat({ label: 'Synced to Odoo', value: num(t.synced), tone: 'ok' })}
      ${stat({ label: 'Not synced', value: num(t.pending + t.unmapped + t.error + t.held),
               tone: (t.unmapped + t.error + t.held) ? 'warn' : '', note: 'pending, unmatched, failed or held' })}
      ${stat({ label: 'Shifts', value: num(t.shifts), note: `${num(t.open_shifts)} open` })}
    </div>
    ${table('daily', 'Days', [
      { key: 'date', label: 'Date' },
      { key: 'punches', label: 'Punches', num: true },
      { key: 'synced', label: 'Synced', num: true },
      { key: 'pending', label: 'Pending', num: true },
      { key: 'unmapped', label: 'Unmatched', num: true },
      { key: 'error', label: 'Failed', num: true },
      { key: 'held', label: 'Held', num: true },
      { key: 'skipped', label: 'Skipped', num: true },
      { key: 'employees', label: 'Employees', num: true },
      { key: 'terminals', label: 'Terminals', num: true },
      { key: 'shifts', label: 'Shifts', num: true },
      { key: 'open_shifts', label: 'Open shifts', num: true },
    ], d.days)}`;
}

function renderSync(d) {
  const t = d.totals;
  return `
    <div class="grid cols-4" style="margin-bottom:14px">
      ${stat({ label: 'Sync runs', value: num(t.runs) })}
      ${stat({ label: 'Success rate', value: t.success_rate === null ? '—' : `${t.success_rate}%`,
               tone: t.failed ? 'warn' : 'ok', note: `${num(t.failed)} failed · ${num(t.partial)} partial` })}
      ${stat({ label: 'Average duration', value: secs(t.avg_duration_ms) })}
      ${stat({ label: 'Attendance created', value: num(t.attendances_created), note: `${num(t.attendances_closed)} closed` })}
    </div>
    ${table('syncdays', 'By Day', [
      { key: 'date', label: 'Date' },
      { key: 'runs', label: 'Runs', num: true },
      { key: 'failed', label: 'Failed', num: true },
      { key: 'punches_new', label: 'New punches', num: true },
      { key: 'attendances_created', label: 'Created', num: true },
      { key: 'attendances_closed', label: 'Closed', num: true },
    ], d.days)}
    ${table('runs', 'Runs', [
      { key: 'started_at', label: 'Started', show: (r) => time(r.started_at) },
      { key: 'status', label: 'Result', html: (r) => pill(r.status), csv: (r) => r.status },
      { key: 'trigger', label: 'Trigger', show: (r) => triggerLabel(r.trigger) },
      { key: 'duration_ms', label: 'Duration', num: true, show: (r) => secs(r.duration_ms), csv: (r) => r.duration_ms },
      { key: 'punches_fetched', label: 'Fetched', num: true },
      { key: 'punches_new', label: 'New', num: true },
      { key: 'attendances_created', label: 'Created', num: true },
      { key: 'attendances_closed', label: 'Closed', num: true },
      { key: 'errors', label: 'Errors', num: true },
      { key: 'error', label: 'Error', show: (r) => r.error || '' },
    ], d.runs, { note: d.runs.length < t.runs ? `the latest ${d.runs.length} of ${num(t.runs)}` : '', show: 50 })}`;
}

const RENDER = { exceptions: renderExceptions, terminals: renderTerminals, daily: renderDaily, sync: renderSync };

/* --- the page ------------------------------------------------------------------ */
export async function render(mount, route) {
  const tab = RENDER[route.query.tab] ? route.query.tab : 'exceptions';
  const from = route.query.from || todayISO(-6);
  const to = route.query.to || todayISO();
  const link = (o) => `#/reports?${new URLSearchParams({ tab, from, to, ...o })}`;

  mount.innerHTML = `
    <div class="tabs">${TABS.map((t) => `<a href="${link({ tab: t.key })}" class="${t.key === tab ? 'active' : ''}">${esc(t.label)}</a>`).join('')}</div>
    <div class="card filter-bar" style="margin-bottom:14px">
      <form id="range" class="filter-row">
        <div class="chips" role="group" aria-label="Range">
          ${PRESETS().map((p) => `<a class="chip ${p.from === from && p.to === to ? 'on' : ''}" href="${link({ from: p.from, to: p.to })}">${esc(p.label)}</a>`).join('')}
        </div>
        <label class="inline-field"><span>From</span><input type="date" name="from" value="${esc(from)}" max="${esc(todayISO())}"></label>
        <label class="inline-field"><span>To</span><input type="date" name="to" value="${esc(to)}" max="${esc(todayISO())}"></label>
        <button type="submit" class="primary">Show</button>
      </form>
    </div>
    <div id="report">${loading()}</div>`;

  $('#range', mount).addEventListener('submit', (e) => {
    e.preventDefault();
    const v = readForm(e.target);
    window.location.hash = link({ from: v.from, to: v.to });
  });

  let data;
  try {
    data = await api.get(`/reports/${tab}?${new URLSearchParams({ date_from: from, date_to: to })}`);
  } catch (err) {
    $('#report', mount).innerHTML = empty('This Report Could Not Load', err.message || 'Try again in a moment.');
    return;
  }
  tables.clear();
  $('#report', mount).innerHTML = `${RENDER[tab](data)}
    <p class="hint">Times are in your account's time zone (${esc(data.timezone || 'UTC')}).</p>`;
  $('#report', mount).addEventListener('click', (e) => {
    const b = e.target.closest('[data-export]');
    if (b) exportCsv(b.dataset.export, { from, to });
  });
}
