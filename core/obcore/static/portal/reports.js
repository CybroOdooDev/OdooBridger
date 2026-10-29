/* The customer's reports: billing history, plan and usage, account activity.
 * Every table exports to CSV, writing exactly the rows it shows. */

import { fmtDate, money } from '/assets/api.js';
import { $, banner, empty, esc, loading } from '/assets/ui.js';

const DOWNLOAD = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
  + 'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 4v11M7 10l5 5 5-5M5 20h14"/></svg>';

const INVOICE = { paid: ['ok', 'Paid'], open: ['warn', 'Due'], void: ['mute', 'Void'], uncollectible: ['bad', 'Unpaid'] };
const invoicePill = (s) => { const [t, l] = INVOICE[s] || ['mute', s || '—']; return `<span class="pill ${t}">${esc(l)}</span>`; };
/* An instant, in the viewer's own time zone. */
const when = (v) => {
  if (!v) return '—';
  const d = new Date(v);
  if (Number.isNaN(d.getTime())) return String(v).replace('T', ' ').slice(0, 16);
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
};
const cents = (v) => (v === null || v === undefined ? '' : (v / 100).toFixed(2));

const tables = new Map();

function table(id, title, columns, rows, { note = '', blank = 'Nothing to show yet.' } = {}) {
  tables.set(id, { title, columns, rows });
  return `
    <div class="card" style="margin-bottom:14px">
      <div class="card-head">
        <h2>${esc(title)} <span class="hint">${rows.length}${note ? ` · ${esc(note)}` : ''}</span></h2>
        ${rows.length ? `<button class="sm" data-export="${esc(id)}">${DOWNLOAD} Export CSV</button>` : ''}
      </div>
      ${rows.length ? `<div class="table-wrap"><table>
        <thead><tr>${columns.map((c) => `<th class="${c.num ? 'num' : ''}">${esc(c.label)}</th>`).join('')}</tr></thead>
        <tbody>${rows.map((r) => `<tr>${columns.map((c) =>
          `<td class="${c.num ? 'num' : ''}">${c.html ? c.html(r) : esc(c.show ? c.show(r) : (r[c.key] ?? '—'))}</td>`).join('')}</tr>`).join('')}</tbody>
      </table></div>` : `<p class="muted" style="margin:0">${esc(blank)}</p>`}
    </div>`;
}

function exportCsv(id) {
  const t = tables.get(id);
  if (!t) return;
  const cell = (v) => {
    const s = v === null || v === undefined ? '' : String(v);
    return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  const lines = [t.columns.map((c) => cell(c.label)).join(',')]
    .concat(t.rows.map((r) => t.columns.map((c) => cell(c.csv ? c.csv(r) : (c.show ? c.show(r) : r[c.key]))).join(',')));
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([`﻿${lines.join('\r\n')}`], { type: 'text/csv;charset=utf-8' }));
  a.download = `odoobridger-${t.title.toLowerCase().replace(/[^a-z0-9]+/g, '-')}-${new Date().toISOString().slice(0, 10)}.csv`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

const stat = (label, value, note = '', tone = '') => `
  <div class="stat"><div class="k">${esc(label)}</div><div class="v ${tone}">${esc(value)}</div>${note ? `<div class="note">${esc(note)}</div>` : ''}</div>`;

/* --- Billing -------------------------------------------------------------------- */
async function billingTab(api) {
  const d = await api.get('/portal/reports/billing');
  const t = d.totals;
  const cur = t.currency || undefined;
  return `
    ${d.error ? banner('Billing Is Unavailable Right Now', d.error, 'warn') : ''}
    <div class="grid cols-4" style="margin-bottom:14px">
      ${stat('Paid to date', money(t.paid_cents, cur), `${t.invoices} invoice${t.invoices === 1 ? '' : 's'}`)}
      ${stat('Outstanding', money(t.outstanding_cents, cur), '', t.outstanding_cents ? 'bad' : '')}
      ${stat('Current plan', `${money(d.monthly_total_cents)}/mo`, d.interval === 'year' ? 'billed yearly' : 'billed monthly')}
      ${stat(d.cancel_at_period_end ? 'Ends on' : 'Next renewal', d.renews_at ? fmtDate(d.renews_at) : '—',
             d.pays_online ? '' : 'not paying online yet')}
    </div>
    ${table('invoices', 'Invoices', [
      { key: 'created', label: 'Date', show: (r) => (r.created ? fmtDate(r.created) : '—'), csv: (r) => (r.created || '').slice(0, 10) },
      { key: 'number', label: 'Invoice', show: (r) => r.number || '—' },
      { key: 'lines', label: 'For', show: (r) => (r.lines || []).map((l) => l.description).filter(Boolean).join('; ') || '—' },
      { key: 'amount_due', label: 'Amount', num: true, show: (r) => money(r.amount_due, r.currency), csv: (r) => cents(r.amount_due) },
      { key: 'amount_paid', label: 'Paid', num: true, show: (r) => money(r.amount_paid, r.currency), csv: (r) => cents(r.amount_paid) },
      { key: 'currency', label: 'Currency', show: (r) => String(r.currency || '').toUpperCase() },
      { key: 'status', label: 'Status', html: (r) => invoicePill(r.status), csv: (r) => (INVOICE[r.status] || [, r.status])[1] },
      { key: 'pdf', label: '', html: (r) => (r.invoice_pdf ? `<a href="${esc(r.invoice_pdf)}" target="_blank" rel="noopener">PDF</a>` : ''),
        csv: (r) => r.invoice_pdf || '' },
    ], d.invoices, { blank: 'No invoices yet. They appear here once you pay online.' })}`;
}

/* --- Plan & Usage --------------------------------------------------------------- */
const STATUS = { trialing: ['', 'Free Trial'], active: ['ok', 'Active'], past_due: ['bad', 'Payment Needed'], suspended: ['bad', 'Suspended'] };

function meter(l) {
  if (l.used === null || l.used === undefined) return `<span class="muted">${l.limit ? `of ${l.limit}` : '—'}</span>`;
  if (!l.limit) return `${l.used} <span class="muted">· unlimited</span>`;
  const tone = l.percent >= 100 ? 'bad' : l.percent >= 80 ? 'warn' : 'ok';
  return `<div style="min-width:140px"><div>${l.used} <span class="muted">of ${l.limit}</span></div>
    <div class="seg-bar" style="height:6px;margin:4px 0 0"><span class="seg ${tone === 'ok' ? 'ok' : tone}" style="width:${Math.min(100, l.percent)}%;background:var(--${tone})"></span></div></div>`;
}

async function usageTab(api) {
  const { services } = await api.get('/portal/reports/usage');
  if (!services.length) return empty('No Services Yet', 'Add a service to see its plan and usage here.');
  const labels = [...new Set(services.flatMap((s) => s.limits.map((l) => l.label)))];
  const rows = services.map((s) => ({ ...s, ...Object.fromEntries(s.limits.map((l) => [l.label, l])) }));
  return `
    ${services.some((s) => s.limits.some((l) => l.percent >= 100))
      ? banner('A Plan Limit Is Reached', 'Anything beyond it waits until the plan allows more. Change the package from Services.', 'warn',
        { href: '#/services', label: 'Services' }) : ''}
    ${table('usage', 'Plan & Usage', [
      { key: 'service', label: 'Service' },
      { key: 'package', label: 'Package', show: (r) => r.package || '—' },
      { key: 'status', label: 'Status', html: (r) => { const [t, l] = STATUS[r.status] || ['mute', r.status]; return `<span class="pill ${t}">${esc(l)}</span>`; },
        csv: (r) => (STATUS[r.status] || [, r.status])[1] },
      { key: 'price_cents', label: 'Price', num: true,
        show: (r) => (r.price_cents === null ? 'Custom' : `${money(r.price_cents, r.currency)}/${r.interval === 'year' ? 'yr' : 'mo'}`),
        csv: (r) => cents(r.price_cents) },
      { key: 'trial_ends_at', label: 'Trial ends', show: (r) => (r.status === 'trialing' && r.trial_ends_at ? fmtDate(r.trial_ends_at) : '—'),
        csv: (r) => (r.status === 'trialing' ? (r.trial_ends_at || '').slice(0, 10) : '') },
      ...labels.map((label) => ({
        key: label, label,
        html: (r) => (r[label] ? meter(r[label]) : '—'),
        csv: (r) => (r[label] ? `${r[label].used ?? ''}${r[label].limit ? ` of ${r[label].limit}` : ''}` : ''),
      })),
      { key: 'sync', label: 'Sync every', show: (r) => (r.sync_interval_minutes ? `${r.sync_interval_minutes} min` : '—') },
      { key: 'last_sync', label: 'Last sync', show: (r) => (r.last_sync?.started_at ? when(r.last_sync.started_at) : '—') },
    ], rows)}
    ${services.some((s) => s.usage_error) ? '<p class="muted" style="font-size:13px">Some usage figures could not be read just now. Try again in a minute.</p>' : ''}`;
}

/* --- Activity ------------------------------------------------------------------- */
const RANGES = [{ days: 30, label: 'Last 30 Days' }, { days: 90, label: 'Last 90 Days' }, { days: 365, label: 'Last 12 Months' }];

async function activityTab(api, route) {
  const days = RANGES.some((r) => String(r.days) === route.query.days) ? Number(route.query.days) : 90;
  const d = await api.get(`/portal/reports/activity?days=${days}`);
  return `
    <div class="row" style="gap:6px;margin-bottom:14px;flex-wrap:wrap">
      ${RANGES.map((r) => `<a class="btn sm${r.days === days ? ' primary-link' : ''}" href="#/reports?tab=activity&days=${r.days}">${esc(r.label)}</a>`).join('')}
    </div>
    ${table('activity', 'Account Activity', [
      { key: 'at', label: 'When', show: (r) => when(r.at) },
      { key: 'who', label: 'Who' },
      { key: 'action', label: 'What' },
      { key: 'detail', label: 'Details', show: (r) => r.detail || '—' },
      { key: 'ip', label: 'IP address', show: (r) => r.ip || '—' },
    ], d.events, { note: d.truncated ? 'the latest 1,000' : '', blank: 'Nothing happened on the account in this range.' })}`;
}

/* --- page ----------------------------------------------------------------------- */
export async function renderReports(mount, route, { api, canManage }) {
  const tabs = [
    { key: 'billing', label: 'Billing', render: () => billingTab(api) },
    { key: 'usage', label: 'Plan & Usage', render: () => usageTab(api) },
    ...(canManage() ? [{ key: 'activity', label: 'Activity', render: () => activityTab(api, route) }] : []),
  ];
  const tab = tabs.find((t) => t.key === route.query.tab) || tabs[0];
  mount.innerHTML = `
    <nav class="tabs" aria-label="Reports">${tabs.map((t) =>
      `<a href="#/reports?tab=${t.key}"${t === tab ? ' class="active" aria-current="page"' : ''}>${esc(t.label)}</a>`).join('')}</nav>
    <div id="report">${loading()}</div>`;
  tables.clear();
  const body = $('#report', mount);
  try {
    body.innerHTML = await tab.render();
  } catch (error) {
    body.innerHTML = banner('This Report Could Not Load', error.message || 'Try again in a moment.', 'bad');
    return;
  }
  body.addEventListener('click', (e) => {
    const b = e.target.closest('[data-export]');
    if (b) exportCsv(b.dataset.export);
  });
}
