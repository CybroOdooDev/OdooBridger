/* The three read-heavy screens: attendance, employees, activity. */

import { api, auth } from '../api.js';
import {
  $, busy, empty, esc, field, fmtAgo, fmtHours, fmtLocal, fmtUtc, guard, loading, pill,
  readForm, stat, toast, todayISO, triggerLabel } from '../ui.js';

const TRASH_ICON = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
  + 'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
  + '<path d="M4 7h16M10 11v6M14 11v6M6 7l1 12a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-12M9 7V4h6v3"/></svg>';

const RETRY_ICON = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
  + 'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
  + '<path d="M21 12a9 9 0 1 1-2.64-6.36M21 4v5h-5"/></svg>';

/* --- Attendance ----------------------------------------------------------- */

/** Quick ranges: the question is almost always "today", "yesterday" or "this
 * week", and picking two dates for it was two date pickers too many. */
function rangePresets() {
  const today = todayISO();
  const d = new Date();
  const monthStart = new Date(d.getFullYear(), d.getMonth(), 1);
  const iso = (x) => `${x.getFullYear()}-${String(x.getMonth() + 1).padStart(2, '0')}-${String(x.getDate()).padStart(2, '0')}`;
  return [
    { label: 'Today', from: today, to: today },
    { label: 'Yesterday', from: todayISO(-1), to: todayISO(-1) },
    { label: 'Last 7 Days', from: todayISO(-6), to: today },
    { label: 'This Month', from: iso(monthStart), to: today },
    { label: 'Last 30 Days', from: todayISO(-29), to: today },
  ];
}

export async function renderAttendance(mount, route) {
  const from = route.query.from || todayISO(-6);
  const to = route.query.to || todayISO();
  const emp = route.query.emp || '';
  const q = route.query.q || '';
  const linkFor = (overrides) => {
    const params = new URLSearchParams(Object.entries({ from, to, emp, q, ...overrides }).filter(([, v]) => v));
    return `#/attendance?${params}`;
  };

  mount.innerHTML = `
    <div class="card filter-bar" style="margin-bottom:14px">
      <form id="filters" class="filter-row filter-end">
        <label class="inline-field"><span>From</span><input type="date" name="from" value="${esc(from)}"></label>
        <label class="inline-field"><span>To</span><input type="date" name="to" value="${esc(to)}"></label>
        <div class="search-box">
          <input type="search" name="q" value="${esc(q || emp)}" placeholder="Name or badge" autocomplete="off" aria-label="Search by name or badge">
        </div>
        <button type="submit" class="primary icon-btn" aria-label="Search" title="Search">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="M20 20l-3.5-3.5"/></svg>
        </button>
      </form>
    </div>
    <div id="rows">${loading()}</div>`;

  $('#filters', mount).addEventListener('submit', (event) => {
    event.preventDefault();
    const values = readForm(event.target);
    window.location.hash = linkFor({ from: values.from, to: values.to, q: values.q, emp: '' });
  });

  const params = new URLSearchParams({ date_from: from, date_to: to, limit: '500' });
  if (emp) params.set('emp_code', emp);
  const all = await api.get(`/attendance?${params}`);

  const draw = (needle) => {
    const n = needle.trim().toLowerCase();
    const rows = n
      ? all.filter((r) => (r.employee_name || '').toLowerCase().includes(n) || String(r.emp_code).toLowerCase().includes(n))
      : all;
    const hours = rows.reduce((sum, r) => sum + (r.worked_hours || 0), 0);
    const people = new Set(rows.map((r) => r.emp_code)).size;
    const open = rows.filter((r) => !r.check_out_local).length;
    $('#rows', mount).innerHTML = rows.length ? `
      <div class="grid cols-3" style="margin-bottom:14px">
        ${stat({ label: 'Employees', value: people })}
        ${stat({ label: 'Total Worked Hours', value: hours.toFixed(1) })}
        ${stat({ label: 'Open Attendances', value: open, tone: open ? 'warn' : '' })}
      </div>
      <div class="card">
        <h2>Shifts <span class="hint">${rows.length}${n ? ` matching “${esc(needle.trim())}”` : ''}${
          all.length >= 500 ? ' · showing the latest 500 — narrow the dates for more' : ''}</span></h2>
        <div class="scroll">
          <table>
            <thead><tr>
              <th>Employee</th><th>Date</th><th>In</th><th>Out</th>
              <th class="num" title="Punch to punch. Odoo's own Worked Hours can read lower: from 17 onward it subtracts the break in the employee's working schedule.">Elapsed</th>
              <th>Device</th><th>Flags</th><th class="num">Odoo</th>
            </tr></thead>
            <tbody>
              ${rows.map((r) => `
                <tr>
                  <td><div>${esc(r.employee_name || '—')}</div><div class="hint mono">${esc(r.emp_code)}</div></td>
                  <td>${esc(r.shift_date || '—')}</td>
                  <td>${esc(fmtLocal(r.check_in_local)?.slice(11) || '—')}</td>
                  <td>${r.check_out_local ? esc(fmtLocal(r.check_out_local).slice(11)) : '<span class="pill warn">open</span>'}</td>
                  <td class="num">${esc(fmtHours(r.worked_hours))}</td>
                  <td class="mono">${esc(r.device_serial || '—')}</td>
                  <td>${flags(r)}</td>
                  <td class="num mono">${esc(r.odoo_attendance_id ?? '—')}</td>
                </tr>`).join('')}
            </tbody>
          </table>
        </div>
      </div>` : empty(
        n ? `No one matching “${needle.trim()}”` : 'No Attendance In This Range',
        n ? 'Check the spelling, or clear the search.' : 'Pick a wider range above, or run a sync.'
      );
  };
  draw(q || '');

  // Search narrows what is already loaded, as you type — no Show press needed.
  const box = mount.querySelector('input[name=q]');
  box.addEventListener('input', () => draw(box.value));
}

function flags(record) {
  const out = [];
  if (record.overtime_hours > 0) {
    const ot = Number(record.overtime_hours);
    const worked = Number(record.worked_hours || 0);
    out.push(`<span class="pill" title="One shift here. In Odoo, its overtime rule split it into ${(worked - ot).toFixed(2)} h attendance and ${ot.toFixed(2)} h overtime.">A + OT ${ot.toFixed(2)} h</span>`);
  }
  if (record.origin === 'odoo') out.push('<span class="pill" title="Found in Odoo for a matched employee: entered there by hand, from a kiosk, or by another app. Not built from punches.">added in Odoo</span>');
  if (record.is_auto_closed) out.push('<span class="pill warn">auto-closed</span>');
  if (record.is_orphan_out) out.push('<span class="pill bad">orphan out</span>');
  return out.join(' ') || '<span class="pill ok">clean</span>';
}

/* --- Employees ------------------------------------------------------------ */

/* The badges still waiting for an Odoo employee. Lives on Settings → Odoo
 * connection (matching is configuration, and needs Odoo to search), reached
 * from the Employees page's "Show unmapped employees". */
export function unmappedCard(attention) {
  if (!attention.length) {
    return `
      <div class="card" id="unmapped" style="margin-top:18px">
        <h2>Unmapped Employees <span class="hint">None</span></h2>
        ${empty('Every Badge Is Matched', 'New badges show up here after a sync, until they are matched.')}
      </div>`;
  }
  // Badges where the badge and the name disagree come first: a person has to
  // decide those (the server sends them first too; this keeps any caller right).
  attention = [...attention].sort((a, b) => Number(!!b.suggested_employee_id) - Number(!!a.suggested_employee_id));
  const review = attention.filter((m) => m.suggested_employee_id).length;
  return `
    <div class="card" id="unmapped" style="margin-top:18px">
      <h2>Unmapped Employees <span class="hint">${attention.length} badge${attention.length === 1 ? '' : 's'} holding attendance${review ? ` · ${review} to review` : ''}</span></h2>
      <p style="color:var(--muted);margin:0 0 12px;font-size:13px">
        These badges have punched but no Odoo employee carries them, so their
        attendance is held. Pick the employee each one belongs to — or, for
        good, set the badge as that employee's <strong>Badge ID</strong> in Odoo
        and matching becomes automatic.
        ${review ? `Badges marked <strong>Needs Review</strong> are listed first: the badge and the name point at different people, so they are not matched until you confirm.` : ''}
      </p>
      <div class="scroll" style="overflow:visible">
        <table>
          <thead><tr><th>Badge</th><th>Name On The Device</th><th>Status</th><th>Match To Odoo Employee</th></tr></thead>
          <tbody>
            ${attention.map((m) => `
              <tr>
                <td class="mono">${esc(m.emp_code)}</td>
                <td>${esc(m.source_name || '—')}${m.match_note ? `<div class="hint">${esc(m.match_note)}</div>` : ''}</td>
                <td>${m.suggested_employee_id
                    ? `${pill('unmapped', 'needs review')}<div class="hint">${esc(SUGGESTION_KIND[m.suggestion_kind] || '')}</div>`
                    : pill(m.status === 'unmapped' ? 'unmapped' : m.status, m.status === 'unmapped' ? 'not matched' : m.status)}</td>
                <td>
                  ${auth.canWrite && m.suggested_employee_id ? `
                    <div class="suggest-box">
                      <span class="hint">Suggested</span> <strong>${esc(m.suggested_employee_name || `Employee #${m.suggested_employee_id}`)}</strong>
                      <button class="sm primary" data-confirm="${esc(m.id)}" data-emp="${esc(m.suggested_employee_id)}" data-name="${esc(m.suggested_employee_name || '')}">Confirm Match</button>
                      <button class="sm link" data-notsame="${esc(m.id)}" title="Not the same person: stop suggesting this employee for this badge.">Not The Same Person</button>
                    </div>` : ''}
                  ${auth.canWrite ? `
                    <div class="match-row">
                      <div class="picker" data-picker="${esc(m.id)}">
                        <input type="search" placeholder="Search Odoo employees…" autocomplete="off"
                               aria-label="Odoo employee for badge ${esc(m.emp_code)}" value="${esc(m.source_name && String(m.source_name).toLowerCase() !== 'unknown badge' ? m.source_name : '')}">
                        <ul class="picker-list hidden" role="listbox"></ul>
                      </div>
                      <button class="sm primary" data-map="${esc(m.id)}" disabled>Match</button>
                      <button class="sm link" data-ignore="${esc(m.id)}" title="Stop holding attendance for this badge — its punches are skipped.">Ignore</button>
                      <span class="remove-slot"><button type="button" class="icon-btn" data-remove="${esc(m.id)}" aria-label="Delete badge ${esc(m.emp_code)}"
                              title="Delete — removes this badge from the list. Its held punches are skipped, and it won't come back on the next sync.">${TRASH_ICON}</button></span>
                    </div>` : '<span class="hint">Your role cannot match badges.</span>'}
                </td>
              </tr>`).join('')}
          </tbody>
        </table>
      </div>
    </div>`;
}

/* Badges deleted from the list above. They are kept (so a sync does not bring
 * them back); Restore puts one back in the unmatched list. */
export function deletedBadgesCard(removed) {
  if (!removed.length) return '';
  return `
    <details class="card" id="deletedBadges" style="margin-top:18px">
      <summary><h2 style="display:inline">Deleted Badges <span class="hint">${removed.length}</span></h2></summary>
      <p style="color:var(--muted);margin:10px 0 12px;font-size:13px">
        These badges were deleted from the unmatched list, so their punches are skipped.
        Restore one to match it again.
      </p>
      <div class="scroll" style="overflow:visible"><table>
        <thead><tr><th>Badge</th><th>Name On The Device</th><th></th></tr></thead>
        <tbody>${removed.map((m) => `<tr>
          <td class="mono">${esc(m.emp_code)}</td>
          <td>${esc(m.source_name || '—')}</td>
          <td class="actions-cell">${auth.canWrite ? `<button class="sm" data-restore="${esc(m.id)}">Restore</button>` : ''}</td>
        </tr>`).join('')}</tbody>
      </table></div>
    </details>`;
}

const SUGGESTION_KIND = {
  badge_only: 'Same badge, different name',
  name_only: 'Same name, different badge',
};

export function wireUnmapped(mount, rerender) {
  mount.querySelectorAll('[data-restore]').forEach((button) => {
    button.addEventListener('click', () =>
      busy(button, async () => {
        const done = await guard(async () => {
          const result = await api.post(`/mappings/${button.dataset.restore}/restore`, {});
          await rerender();
          return result;
        });
        if (done) toast(done.message, 'ok');
      })
    );
  });
  mount.querySelectorAll('[data-picker]').forEach((box) => wirePicker(box, mount, rerender));
  mount.querySelectorAll('[data-confirm]').forEach((button) => {
    button.addEventListener('click', () =>
      busy(button, () =>
        guard(async () => {
          await api.patch(`/mappings/${button.dataset.confirm}`, {
            odoo_employee_id: Number(button.dataset.emp),
            ...(button.dataset.name ? { odoo_employee_name: button.dataset.name } : {}),
          });
          await rerender();
        }, `Matched${button.dataset.name ? ` to ${button.dataset.name}` : ''} — held punches sync on the next run`)
      )
    );
  });
  mount.querySelectorAll('[data-notsame]').forEach((button) => {
    button.addEventListener('click', () =>
      busy(button, () =>
        guard(async () => {
          await api.patch(`/mappings/${button.dataset.notsame}`, { dismiss_suggestion: true });
          await rerender();
        }, 'Suggestion Dismissed')
      )
    );
  });
  mount.querySelectorAll('[data-ignore]').forEach((button) => {
    button.addEventListener('click', () =>
      busy(button, () =>
        guard(async () => {
          await api.patch(`/mappings/${button.dataset.ignore}`, { status: 'ignored' });
          await rerender();
        }, 'Badge Ignored')
      )
    );
  });

  // Two clicks, like deleting a punch: the first arms the icon, the second deletes.
  mount.querySelectorAll('[data-remove]').forEach((button) => {
    let armed = null;
    const icon = button.innerHTML;
    button.addEventListener('click', () => {
      if (!armed) {
        button.textContent = 'Confirm Delete';
        button.classList.remove('icon-btn');
        button.classList.add('sm', 'danger');
        armed = setTimeout(() => {
          armed = null;
          button.innerHTML = icon;
          button.classList.remove('sm', 'danger');
          button.classList.add('icon-btn');
        }, 4000);
        return;
      }
      clearTimeout(armed);
      armed = null;
      busy(button, () => guard(async () => {
        await api.del(`/mappings/${button.dataset.remove}`);
        await rerender();
      }, 'Badge Deleted'));
    });
  });
}

export const needsMatch = (m) => ['unmapped', 'ambiguous'].includes(m.status);

const FUNNEL = '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 5h18l-7 8v6l-4-2v-4z"/></svg>';

// The Employees filters live for the session, so coming back to the page
// keeps what you had narrowed to.
const EMP_FILTERS = { department: new Set(), manager: new Set(), company: new Set() };
let empSearch = '';

export async function renderEmployees(mount) {
  mount.innerHTML = loading();
  const mappings = await api.get('/mappings?limit=500');
  const waiting = mappings.filter(needsMatch).length;
  const resolved = mappings.filter((m) => !needsMatch(m));

  const distinct = (pairs) => [...new Map(pairs.filter(([v]) => v != null && v !== ''))
    .entries()].sort((a, b) => String(a[1]).localeCompare(String(b[1])));
  const companies = distinct(resolved.map((m) => [String(m.odoo_company_id ?? ''), m.odoo_company_name || (m.odoo_company_id != null ? `Company #${m.odoo_company_id}` : '')]));
  const departments = distinct(resolved.map((m) => [m.odoo_department_name, m.odoo_department_name]));
  const managers = distinct(resolved.map((m) => [m.odoo_manager_id != null ? String(m.odoo_manager_id) : '', m.odoo_manager_name || (m.odoo_manager_id != null ? `Employee #${m.odoo_manager_id}` : '')]));
  // A company column and filter only earn their place when more than one
  // company's employees are listed.
  const showCompany = companies.length > 1;
  const showDept = departments.length > 0;
  const showManager = managers.length > 0;
  const GROUPS = [
    ['department', 'Department', departments, showDept],
    ['manager', 'Manager', managers, showManager],
    ['company', 'Company', companies, showCompany],
  ].filter((g) => g[3]);
  // Drop stale selections (a company switched off, a manager who left).
  for (const [key, , options] of GROUPS) {
    const valid = new Set(options.map(([v]) => v));
    EMP_FILTERS[key].forEach((v) => { if (!valid.has(v)) EMP_FILTERS[key].delete(v); });
  }
  if (!showCompany) EMP_FILTERS.company.clear();
  if (!showDept) EMP_FILTERS.department.clear();
  if (!showManager) EMP_FILTERS.manager.clear();
  const activeCount = () => GROUPS.reduce((n, [key]) => n + EMP_FILTERS[key].size, 0);

  const menuHtml = () => `
    ${GROUPS.map(([key, title, options]) => `
      <fieldset class="flt-group"><legend>${esc(title)}</legend>
        <div class="flt-scroll">
          ${options.map(([v, label]) => `<label class="flt-opt"><input type="checkbox" data-eflt="${key}" value="${esc(v)}"
            ${EMP_FILTERS[key].has(v) ? 'checked' : ''}> <span>${esc(label)}</span></label>`).join('')}
        </div>
      </fieldset>`).join('')}
    <div class="flt-foot"><button type="button" class="sm" data-eflt-clear ${activeCount() ? '' : 'disabled'}>Clear All Filters</button></div>`;

  mount.innerHTML = `
    <div class="card">
      <div class="card-head" style="position:static;justify-content:flex-end">
        <div class="actions">
          ${resolved.length ? `<input type="search" id="matchedFilter" class="compact-search" style="width:300px"
            placeholder="Search by name, badge or Odoo ID" aria-label="Search employees" value="${esc(empSearch)}">` : ''}
          ${GROUPS.length ? `<span class="flt-wrap" style="margin-left:0">
            <button class="sm flt-btn ${activeCount() ? 'on' : ''}" id="empFltBtn" aria-haspopup="true" aria-expanded="false" title="Filters">
              ${FUNNEL}<span>Filters</span>${activeCount() ? `<span class="flt-count">${activeCount()}</span>` : ''}</button>
            <div class="flt-menu flt-menu-right hidden" id="empFltMenu">${menuHtml()}</div></span>` : ''}
          ${waiting ? `<a class="btn sm warn-link" href="#/settings/odoo?show=unmapped">
            Show Unmapped Employees (${waiting})</a>` : ''}
        </div>
      </div>
      ${resolved.length ? `
        <div class="scroll">
          <table id="matchedTable">
            <thead><tr><th>Employee</th>${showDept ? '<th>Department</th>' : ''}${showManager ? '<th>Manager</th>' : ''}${showCompany ? '<th>Company</th>' : ''}<th>Badge</th><th>Matched By</th><th>Status</th><th>Open Shift</th><th>Last Punch</th></tr></thead>
            <tbody>
              ${resolved.map((m) => `
                <tr data-search="${esc(`${m.odoo_employee_name || ''} ${m.emp_code} ${m.odoo_employee_id ?? ''} ${m.source_name || ''}`.toLowerCase())}"
                    data-odoo="${esc(m.odoo_employee_id ?? '')}"
                    data-department="${esc(m.odoo_department_name ?? '')}"
                    data-manager="${esc(m.odoo_manager_id ?? '')}"
                    data-company="${esc(m.odoo_company_id ?? '')}">
                  <td><div>${esc(m.odoo_employee_name || '—')}</div>${m.odoo_employee_id ? `<div class="hint mono">Odoo #${esc(m.odoo_employee_id)}</div>` : ''}</td>
                  ${showDept ? `<td>${esc(m.odoo_department_name || '—')}</td>` : ''}
                  ${showManager ? `<td>${esc(m.odoo_manager_name || '—')}</td>` : ''}
                  ${showCompany ? `<td>${esc(m.odoo_company_name || '—')}</td>` : ''}
                  <td class="mono">${esc(m.emp_code)}</td>
                  <td style="text-transform:capitalize">${esc(m.match_method || '—')}</td>
                  <td>${pill(m.status)}</td>
                  <td>${m.open_attendance_id
                    ? `<span class="pill warn">open #${esc(m.open_attendance_id)}</span>`
                    : '<span class="pill mute">none</span>'}</td>
                  <td>${esc(m.last_punch_at ? fmtAgo(m.last_punch_at) : '—')}</td>
                </tr>`).join('')}
              <tr id="empNone" class="hidden"><td colspan="12" class="hint" style="text-align:center;padding:18px">No employees match.</td></tr>
            </tbody>
          </table>
        </div>` : empty('Nothing Matched Yet', waiting
          ? 'Badges have punched but none is matched yet — use “Show unmapped employees” above.'
          : 'Badges appear here after the first sync.')}
    </div>`;

  // One pass applies the search and every filter together. Within a filter,
  // ticking several values means "any of these"; across filters, "all of".
  const applyFilters = () => {
    const n = ($('#matchedFilter', mount)?.value || '').trim().toLowerCase();
    empSearch = n ? $('#matchedFilter', mount).value : '';
    let shown = 0;
    mount.querySelectorAll('#matchedTable tbody tr[data-search]').forEach((tr) => {
      const hide = (n && !tr.dataset.search.includes(n))
        || EMP_FILTERS.department.size && !EMP_FILTERS.department.has(tr.dataset.department)
        || EMP_FILTERS.manager.size && !EMP_FILTERS.manager.has(tr.dataset.manager)
        || EMP_FILTERS.company.size && !EMP_FILTERS.company.has(tr.dataset.company);
      tr.classList.toggle('hidden', Boolean(hide));
      if (!hide) shown += 1;
    });
    $('#empNone', mount)?.classList.toggle('hidden', shown > 0);
    const filtering = Boolean(n) || activeCount() > 0;
    const count = $('#empCount', mount);
    if (count) count.textContent = filtering
      ? `${shown} of ${resolved.length} shown` : `${resolved.length} matched to Odoo`;
  };

  const btn = $('#empFltBtn', mount);
  const menu = $('#empFltMenu', mount);
  const syncButton = () => {
    btn.classList.toggle('on', activeCount() > 0);
    btn.querySelector('.flt-count')?.remove();
    if (activeCount()) btn.insertAdjacentHTML('beforeend', `<span class="flt-count">${activeCount()}</span>`);
    menu.querySelector('[data-eflt-clear]').disabled = activeCount() === 0;
  };
  if (btn && menu) {
    menu.addEventListener('change', (event) => {
      const box = event.target.closest('[data-eflt]');
      if (!box) return;
      const set = EMP_FILTERS[box.dataset.eflt];
      if (box.checked) set.add(box.value); else set.delete(box.value);
      syncButton();
      applyFilters();
    });
    menu.querySelector('[data-eflt-clear]').addEventListener('click', () => {
      Object.values(EMP_FILTERS).forEach((set) => set.clear());
      menu.querySelectorAll('[data-eflt]').forEach((box) => { box.checked = false; });
      syncButton();
      applyFilters();
    });
    btn.addEventListener('click', (event) => {
      event.stopPropagation();
      const open = menu.classList.toggle('hidden') === false;
      btn.setAttribute('aria-expanded', String(open));
    });
    const away = (event) => {
      if (!document.body.contains(btn)) { document.removeEventListener('click', away); return; }
      if (!menu.classList.contains('hidden') && !menu.contains(event.target)) {
        menu.classList.add('hidden');
        btn.setAttribute('aria-expanded', 'false');
      }
    };
    document.addEventListener('click', away);
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && !menu.classList.contains('hidden')) {
        menu.classList.add('hidden');
        btn.setAttribute('aria-expanded', 'false');
      }
    });
  }
  $('#matchedFilter', mount)?.addEventListener('input', applyFilters);
  applyFilters();
}

/** Search-as-you-type over the connected Odoo's employees.
 *
 * Picking one enables Match. If Odoo cannot be searched (not connected, or
 * refusing), typing a numeric Odoo id still works — the old way, kept as the
 * fallback rather than the only way. */
function wirePicker(box, mount, rerender) {
  const id = box.dataset.picker;
  const input = box.querySelector('input');
  const list = box.querySelector('.picker-list');
  const matchButton = mount.querySelector(`[data-map="${id}"]`);
  let picked = null;
  let timer = null;
  let seq = 0;

  const setPicked = (emp) => {
    picked = emp;
    matchButton.disabled = !picked;
    box.classList.toggle('chosen', Boolean(picked));
  };

  const show = (html) => {
    list.innerHTML = html;
    list.classList.toggle('hidden', !html);
  };

  const search = async () => {
    const q = input.value.trim();
    const mine = ++seq;
    let rows;
    try {
      rows = await api.get(`/odoo-employees?q=${encodeURIComponent(q)}&limit=12`);
    } catch (error) {
      if (mine !== seq) return;
      if (/^\d+$/.test(q)) {
        setPicked({ id: Number(q), name: null });
        show(`<li class="picker-note">Odoo can’t be searched right now (${esc(error.message)}). Match will use Odoo id ${esc(q)}.</li>`);
      } else {
        show(`<li class="picker-note">Odoo can’t be searched right now: ${esc(error.message)}. You can type the employee’s Odoo id instead.</li>`);
      }
      return;
    }
    if (mine !== seq) return;
    show(rows.length ? rows.map((r) => `
      <li role="option" data-emp="${esc(r.id)}" data-name="${esc(r.name)}" class="${r.matched_badge ? 'taken' : ''}">
        <span>${esc(r.name)}${r.department ? ` <span class="hint">· ${esc(r.department)}</span>` : ''}</span>
        <span class="hint">${r.matched_badge ? `has badge ${esc(r.matched_badge)}` : `#${esc(r.id)}`}</span>
      </li>`).join('') : `<li class="picker-note">No Odoo employee matches “${esc(q)}”.</li>`);
  };

  input.addEventListener('input', () => {
    setPicked(null);
    clearTimeout(timer);
    timer = setTimeout(search, 220);
  });
  input.addEventListener('focus', () => { if (!picked) search(); });
  input.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') show('');
    if (event.key === 'Enter') {
      event.preventDefault();
      const first = list.querySelector('li[data-emp]:not(.taken)');
      if (first) first.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
      else if (picked) matchButton.click();
    }
  });
  input.addEventListener('blur', () => setTimeout(() => show(''), 150));
  list.addEventListener('mousedown', (event) => {
    const li = event.target.closest('li[data-emp]');
    if (!li) return;
    event.preventDefault();
    if (li.classList.contains('taken')) return;
    setPicked({ id: Number(li.dataset.emp), name: li.dataset.name });
    input.value = li.dataset.name;
    show('');
  });

  matchButton.addEventListener('click', () => {
    if (!picked) return;
    busy(matchButton, () =>
      guard(async () => {
        await api.patch(`/mappings/${id}`, {
          odoo_employee_id: picked.id,
          ...(picked.name ? { odoo_employee_name: picked.name } : {}),
        });
        await rerender();
      }, `Matched${picked.name ? ` to ${picked.name}` : ''} — held punches sync on the next run`)
    );
  });
}

/* --- Activity ------------------------------------------------------------- */

const STATE_LABEL = {
  pending: 'Pending', synced: 'Synced', unmapped: 'Unmatched badge', error: 'Error',
  skipped: 'Skipped', deleted: 'Deleted', held: 'Held (device limit)',
};

function wireRunLogs(mount) {
  mount.querySelectorAll('[data-log]').forEach((button) => {
    button.addEventListener('click', () => {
      const open = $(`#log-${button.dataset.log}`, mount).classList.toggle('hidden') === false;
      button.textContent = open ? 'Hide Log' : 'Open Log';
      button.setAttribute('aria-expanded', String(open));
    });
  });
}
/** The Activity list's checkboxes: pick punches, then move them all to pending or delete them.
 *  ("Rerun punches" moves them back to pending.) Delete arms first, like the single-row delete: a second click within a few seconds confirms. */
/* The selection outlives a re-render of the same list, so "Show more" (which only raises the
 * limit) keeps it: ticked punches stay ticked, and a ticked header box takes in the new rows too.
 * Any other filter starts afresh. */
const BULK = { key: null, ids: new Set(), allOnPage: false, everything: null };
const resetBulk = (key = null) => Object.assign(BULK, { key, ids: new Set(), allOnPage: false, everything: null });

/** "3,422 punches" with nothing narrowed, "1,234 matching" with a filter or state tab, and
 *  "· showing 100" while the page holds fewer than that. Falls back to the page length. */
function ledgerCount(totals, shown, narrowed) {
  const total = totals ? totals.matching : shown;
  const n = Number(total).toLocaleString();
  const label = narrowed ? `${n} matching` : `${n} punch${total === 1 ? '' : 'es'} in total`;
  return shown < total ? `${label} · showing ${Number(shown).toLocaleString()}` : label;
}

function wireBulk(mount, rerender, { filter, mayHaveMore, total: sectionTotal }) {
  const boxes = [...mount.querySelectorAll('[data-sel]')];
  const key = JSON.stringify(filter);
  if (BULK.key !== key) resetBulk(key);
  boxes.forEach((b) => { b.checked = (Boolean(BULK.everything) || BULK.allOnPage || BULK.ids.has(b.dataset.sel)); });
  if (BULK.everything?.excludeSynced) boxes.forEach((b) => { if (b.dataset.synced) b.checked = false; });
  const all = $('#selAll', mount);
  const bar = $('#bulkBar', mount);
  const warn = $('#bulkWarn', mount);
  const pendingBtn = $('#bulkPending', mount);
  const deleteBtn = $('#bulkDelete', mount);
  const everythingBtn = $('#bulkEverything', mount);
  const fmt = (n) => Number(n).toLocaleString();
  const plural = (n, one, many) => `${fmt(n)} ${n === 1 ? one : many}`;
  //: Set once "Select every matching punch" is chosen: the counts across every page.
  let everything = BULK.everything;
  const chosen = () => boxes.filter((b) => b.checked);
  const remember = () => {
    const picked = chosen();
    BULK.ids = new Set(picked.map((b) => b.dataset.sel));
    BULK.allOnPage = picked.length > 0 && picked.length === boxes.length;
    BULK.everything = everything;
  };
  // How many of the selection each action applies to, and how many are already in Odoo.
  const counts = () => {
    if (everything) {
      return { total: everything.matching, rerun: everything.rerun, del: everything.delete, synced: everything.synced || 0 };
    }
    const picked = chosen();
    return { total: picked.length, rerun: picked.filter((b) => b.dataset.rerun).length,
             del: picked.filter((b) => b.dataset.del).length, synced: picked.filter((b) => b.dataset.synced).length };
  };
  const hideWarn = () => { warn.hidden = true; warn.innerHTML = ''; };

  const paint = () => {
    remember();
    const picked = chosen();
    const { total, rerun, del } = counts();
    bar.hidden = !picked.length && !everything;
    if (bar.hidden) hideWarn();
    const wholePage = picked.length > 0 && picked.length === boxes.length;
    $('#bulkCount', mount).textContent = everything
      ? `All ${fmt(total)} matching punches selected${everything.excludeSynced ? ' (synced left out)' : ''}`
      : wholePage && mayHaveMore ? `All ${picked.length} on this page selected` : `${picked.length} selected`;
    everythingBtn.hidden = !(wholePage && mayHaveMore && !everything);
    everythingBtn.textContent = sectionTotal ? `Select all ${fmt(sectionTotal)}` : 'Select all';
    if (pendingBtn) {
      pendingBtn.textContent = `Rerun punches (${fmt(rerun)})`;
      pendingBtn.disabled = !total;
      pendingBtn.title = 'Queue them again for the next sync. Deleted punches are restored.';
    }
    if (deleteBtn) {
      deleteBtn.textContent = `Delete (${fmt(del)})`;
      deleteBtn.disabled = !total;
      deleteBtn.title = 'Take them out of the queue. They won’t be pushed or come back on the next sync.';
    }
    all.checked = Boolean(everything) || wholePage;
    all.indeterminate = !everything && picked.length > 0 && picked.length < boxes.length;
    boxes.forEach((b) => b.closest('tr').classList.toggle('selected', b.checked));
  };

  /* Before acting: say what will and won't happen. Synced punches are already Odoo attendance, so
   * they are never rerun or deleted; the warning names them and offers to take them out of the
   * selection. Delete always asks here, synced or not, so this box is also its confirmation. */
  const ask = (action) => {
    const c = counts();
    const n = action === 'delete' ? c.del : c.rerun;
    const verb = action === 'delete' ? 'deleted' : 'rerun';
    const other = c.total - c.synced - n;   // pending, held or already deleted: not touched either
    if (action === 'pending' && !c.synced && !other) { hideWarn(); send(pendingBtn, 'pending'); return; }
    const lines = [];
    if (c.synced) {
      lines.push(`<strong>${plural(c.synced, 'selected punch is', 'selected punches are')} already synced to Odoo</strong>
        and can't be ${verb}. ${c.synced === 1 ? 'It is' : 'They are'} part of an Odoo attendance record;
        to change ${c.synced === 1 ? 'it' : 'them'}, edit the attendance in Odoo.`);
    }
    if (other > 0) {
      lines.push(`${plural(other, 'other punch', 'other punches')} ${other === 1 ? 'is' : 'are'} ${action === 'delete'
        ? 'pending, held or deleted already' : 'pending or held'} and ${other === 1 ? 'is' : 'are'} left as ${other === 1 ? 'it is' : 'they are'}.`);
    }
    if (action === 'delete' && n) {
      lines.push(`${plural(n, 'punch', 'punches')} will be taken out of the queue. ${n === 1 ? 'It' : 'They'} won't be pushed to Odoo or come back on the next sync.`);
    }
    if (!n) lines.push(`None of the selected punches can be ${verb}.`);
    warn.className = `bulk-warn${c.synced || !n ? ' warn' : ''}`;
    warn.innerHTML = `
      <div class="bulk-warn-text">${lines.map((l) => `<p>${l}</p>`).join('')}</div>
      <div class="bulk-warn-actions">
        ${c.synced ? '<button type="button" class="sm" data-w="deselect">Deselect synced punches</button>' : ''}
        ${n ? `<button type="button" class="sm ${action === 'delete' ? 'danger' : 'primary'}" data-w="go">${
          action === 'delete' ? `Delete ${plural(n, 'punch', 'punches')}` : `Rerun ${plural(n, 'punch', 'punches')}`}</button>` : ''}
        <button type="button" class="sm link" data-w="cancel">Cancel</button>
      </div>`;
    warn.hidden = false;
    warn.querySelector('[data-w="go"]')?.addEventListener('click', (e) => send(e.currentTarget, action));
    warn.querySelector('[data-w="cancel"]').addEventListener('click', hideWarn);
    warn.querySelector('[data-w="deselect"]')?.addEventListener('click', async (e) => {
      e.currentTarget.disabled = true;
      await deselectSynced();
      ask(action);   // show what is left, ready to go
    });
    (warn.querySelector('[data-w="go"]') || warn.querySelector('[data-w="cancel"]')).focus();
  };

  async function deselectSynced() {
    boxes.forEach((b) => { if (b.dataset.synced) b.checked = false; });
    if (everything) {
      const q = new URLSearchParams(Object.entries({ ...filter, exclude_synced: 'true' }).filter(([, v]) => v));
      const fresh = await guard(() => api.get(`/punches/selectable?${q}`));
      everything = fresh && fresh.matching ? { ...fresh, excludeSynced: true } : null;
    }
    paint();
  }

  // Any change to the ticks on the page goes back to "just these".
  boxes.forEach((b) => b.addEventListener('change', () => { everything = null; hideWarn(); paint(); }));
  all.addEventListener('change', () => {
    everything = null;
    hideWarn();
    if (!boxes.length) { if (all.checked) selectEverything(); else paint(); return; }
    boxes.forEach((b) => { b.checked = all.checked; }); paint();
  });
  $('#bulkClear', mount).addEventListener('click', () => { everything = null; boxes.forEach((b) => { b.checked = false; }); hideWarn(); paint(); });
  async function selectEverything() {
    everythingBtn.disabled = true;
    all.disabled = true;
    const q = new URLSearchParams(Object.entries(filter).filter(([, v]) => v));
    const fresh = await guard(() => api.get(`/punches/selectable?${q}`));
    everythingBtn.disabled = false;
    all.disabled = false;
    if (fresh && !fresh.matching) toast('No punch matches these filters.', 'ok');
    everything = fresh && fresh.matching > boxes.length ? fresh : null;
    hideWarn(); paint();
  }
  everythingBtn.addEventListener('click', selectEverything);

  const send = async (button, action) => {
    const fits = (b) => (action === 'delete' ? b.dataset.del : b.dataset.rerun);
    const body = everything
      ? { filter: { ...filter, exclude_synced: Boolean(everything.excludeSynced) }, action }
      : { ids: chosen().filter(fits).map((b) => b.dataset.sel), action };
    const label = button.textContent;
    button.disabled = true;
    button.textContent = 'Working…';
    const r = await guard(() => api.post('/punches/bulk', body));
    if (r) { toast(r.message, 'ok'); resetBulk(key); await rerender(); return; }
    button.disabled = false;
    button.textContent = label;
  };
  pendingBtn?.addEventListener('click', () => ask('pending'));
  deleteBtn?.addEventListener('click', () => ask('delete'));
  paint();
}

export async function renderActivity(mount, route) {
  const stateFilter = route.query.state || '';
  const badge = route.query.emp_code || '';
  const terminal = route.query.terminal_sn || '';
  const runId = route.query.run_id || '';
  const from = route.query.date_from || '';
  const to = route.query.date_to || '';
  // Two views of one history: the punches themselves (what most visits are
  // for) and the sync runs that brought them in.
  const view = route.query.view === 'runs' ? 'runs' : 'punches';
  const limit = Math.min(Number(route.query.limit) || 100, 500);
  mount.innerHTML = loading();

  // Everything lives in the query string, so a filtered ledger is a URL you
  // can paste into a ticket.
  const params = new URLSearchParams({ limit: String(limit) });
  if (stateFilter) params.set('state', stateFilter);
  if (badge) params.set('emp_code', badge);
  if (terminal) params.set('terminal_sn', terminal);
  if (runId) params.set('run_id', runId);
  if (from) params.set('date_from', from);
  if (to) params.set('date_to', to);

  // The total behind the page: same filters, no limit (the list itself stops at ``limit``).
  const countParams = new URLSearchParams(params);
  countParams.delete('limit');
  const [runs, punches, devices, totals] = await Promise.all([
    api.get('/sync/runs?limit=20'),
    api.get(`/punches?${params}`),
    api.get('/devices').catch(() => []),
    view === 'punches' ? api.get(`/punches/selectable?${countParams}`).catch(() => null) : null,
  ]);

  const states = ['', 'pending', 'synced', 'unmapped', 'error', 'held', 'skipped', 'deleted'];

  /** Keep the state tab while changing a filter, and vice versa. */
  const linkFor = (overrides) => {
    const q = new URLSearchParams();
    const merged = { state: stateFilter, emp_code: badge, terminal_sn: terminal,
                     run_id: runId, date_from: from, date_to: to, view: view === 'runs' ? 'runs' : '',
                     ...overrides };
    Object.entries(merged).forEach(([k, v]) => { if (v) q.set(k, v); });
    const s = q.toString();
    return `#/activity${s ? `?${s}` : ''}`;
  };

  const filtered = Boolean(badge || terminal || runId || from || to);
  const shownRun = runId ? runs.find((r) => r.id === runId) : null;

  const viewTabs = `
    <div class="tabs">
      <a href="#/activity" class="${view === 'punches' ? 'active' : ''}">Punches</a>
      <a href="#/activity?view=runs" class="${view === 'runs' ? 'active' : ''}">Sync runs</a>
    </div>`;

  const runsCard = `
    <div class="card" style="margin-bottom:14px">
      <h2>Sync Runs <span class="hint">the last ${runs.length}</span></h2>
      ${runs.length ? `
        <div class="scroll">
          <table>
            <thead><tr><th>Started</th><th>Result</th><th class="num">Fetched</th><th class="num">New</th><th class="num">Created</th><th class="num">Closed</th><th class="num">Errors</th><th>Trigger</th><th></th></tr></thead>
            <tbody>
              ${runs.map((r) => `
                <tr>
                  <td><div>${esc(fmtAgo(r.started_at))}</div><div class="hint mono">${esc(fmtUtc(r.started_at))} UTC</div></td>
                  <td>${pill(r.status)}</td>
                  <td class="num">${esc(r.punches_fetched)}</td>
                  <td class="num">${esc(r.punches_new)}</td>
                  <td class="num">${esc(r.attendances_created)}</td>
                  <td class="num">${esc(r.attendances_closed)}</td>
                  <td class="num">${esc(r.error_count)}</td>
                  <td>${esc(triggerLabel(r.triggered_by))}</td>
                  <td class="actions-cell"><div class="row-actions">
                    ${r.punches_new
                      ? `<a class="btn sm" href="${linkFor({
                          run_id: r.id, state: '', emp_code: '', view: '',
                          terminal_sn: '', date_from: '', date_to: '' })}"
                         >${esc(r.punches_new)} punch${r.punches_new === 1 ? '' : 'es'}</a>`
                      : '<span class="hint">No new punches</span>'}
                    <button class="sm" data-log="${esc(r.id)}" aria-expanded="false">Open Log</button>
                  </div></td>
                </tr>
                <tr class="hidden" id="log-${esc(r.id)}">
                  <td colspan="9">
                    ${r.error_message ? `<p class="err" style="margin-top:0">${esc(r.error_message)}</p>` : ''}
                    <pre class="log">${esc((r.log || []).join('\n') || 'No log lines recorded.')}</pre>
                  </td>
                </tr>`).join('')}
            </tbody>
          </table>
        </div>` : empty('No Runs Yet', 'Press Sync now at the top to run one.')}
    </div>`;

  if (view === 'runs') {
    mount.innerHTML = viewTabs + runsCard;
    wireRunLogs(mount);
    return;
  }

  // Every punch can be selected (for an export, say); each action then counts only the punches it
  // applies to: Rerun the ones that never reached Odoo, Delete those that aren't deleted already.
  const REQUEUE = ['error', 'skipped', 'unmapped', 'deleted'];
  const DELETABLE = ['error', 'skipped', 'unmapped'];
  const canRerun = (p) => !p.odoo_attendance_id && REQUEUE.includes(p.process_state);
  const canDelete = (p) => !p.odoo_attendance_id && DELETABLE.includes(p.process_state);
  const anySelectable = punches.length > 0;

  mount.innerHTML = `
    ${viewTabs}
    <div class="card">
      <h2>Punch Ledger <span class="hint" id="punchTotal">${ledgerCount(totals, punches.length, filtered || stateFilter)}</span></h2>
      ${shownRun ? `
        <div class="banner" style="margin:0 0 12px">
          <strong>The sync that started ${esc(fmtUtc(shownRun.started_at))}</strong>
          Read ${esc(shownRun.punches_fetched)} punch${shownRun.punches_fetched === 1 ? '' : 'es'}
          from the device platform and added ${esc(shownRun.punches_new)} to the ledger${
            shownRun.punches_fetched > shownRun.punches_new
              ? `. The other ${
                  esc(shownRun.punches_fetched - shownRun.punches_new)
                } ${shownRun.punches_fetched - shownRun.punches_new === 1
                  ? 'was' : 'were'} already here — every run deliberately
                 re-reads a window of known punches, because devices upload
                 late and their clocks drift. Those stay listed under the run
                 that first saw them.`
              : '.'}
          <a href="${linkFor({ run_id: '' })}">Show the whole ledger</a>
        </div>` : ''}
      <div class="chips" role="group" aria-label="State">
        ${states.map((s) => `
          <a href="${linkFor({ state: s, limit: '' })}" class="chip ${s === stateFilter ? 'on' : ''}">
            ${esc(s ? STATE_LABEL[s] : 'All')}</a>`).join('')}
      </div>

      <form id="punchFilters" class="filter-row" style="margin:14px 0 6px">
        <label class="inline-field"><span>Device</span>
          <select name="terminal_sn">
            <option value="">Any Device</option>
            ${devices.map((d) => `<option value="${esc(d.serial_number)}"${
              d.serial_number === terminal ? ' selected' : ''
            }>${esc(d.alias || d.serial_number)}</option>`).join('')}
          </select></label>
        <label class="inline-field"><span>Badge</span>
          <input type="text" name="emp_code" value="${esc(badge)}" placeholder="any" style="width:120px"></label>
        <label class="inline-field"><span>From</span>
          <input type="date" name="date_from" value="${esc(from)}"></label>
        <label class="inline-field"><span>To</span>
          <input type="date" name="date_to" value="${esc(to)}"></label>
        <div class="row-actions">
          <button class="primary sm" id="applyPunches">Apply</button>
          ${filtered ? `<a class="btn sm" href="${linkFor({
            emp_code: '', terminal_sn: '', date_from: '', date_to: '' })}">Clear</a>` : ''}
        </div>
      </form>
      ${punches.length ? `
        ${anySelectable ? `
        <div class="bulk-bar" id="bulkBar" hidden role="region" aria-label="Selected punches">
          <span class="bulk-count" id="bulkCount" aria-live="polite"></span>
          <button type="button" class="sm link" id="bulkEverything" hidden></button>
          ${auth.canWrite ? `<button type="button" class="sm" id="bulkPending"></button>
          <button type="button" class="sm danger-outline" id="bulkDelete"></button>` : ''}
          <button type="button" class="sm link" id="bulkClear">Clear selection</button>
          <div class="bulk-warn" id="bulkWarn" hidden role="alert"></div>
        </div>` : ''}
        <div class="scroll">
          <table class="punch-table">
            <thead><tr><th>${anySelectable ? `<span class="sel-cell"><input type="checkbox" id="selAll" aria-label="Select every punch on this page"><span>Punch Time</span></span>` : 'Punch Time'}</th><th>Badge</th><th>Dir</th><th>Device</th><th>State</th><th class="num">Odoo</th><th>Detail</th><th></th></tr></thead>
            <tbody>
              ${punches.map((p) => `
                <tr data-row="${esc(p.id)}">
                  <td>${anySelectable ? `<span class="sel-cell"><input type="checkbox" data-sel="${esc(p.id)}"
                               ${canRerun(p) ? 'data-rerun="1"' : ''} ${canDelete(p) ? 'data-del="1"' : ''} ${p.odoo_attendance_id || p.process_state === 'synced' ? 'data-synced="1"' : ''}
                               aria-label="Select the ${esc(p.emp_code)} punch at ${esc(fmtUtc(p.punch_time_local || p.punch_time_utc))}"><span>` : ''}<div class="mono">${esc(fmtUtc(p.punch_time_local || p.punch_time_utc))}</div>
                      <div class="hint mono">${esc(fmtUtc(p.punch_time_utc))} UTC</div>${anySelectable ? '</span></span>' : ''}</td>
                  <td class="mono">${esc(p.emp_code)}</td>
                  <td>${esc(p.direction)}</td>
                  <td class="mono">${esc(p.terminal_sn || '—')}</td>
                  <td>${pill(p.process_state)}</td>
                  <td class="num mono">${esc(p.odoo_attendance_id ?? '—')}</td>
                  <td style="color:var(--muted);font-size:12.5px">${esc(p.error_message || '—')}</td>
                  <td class="actions-cell"><div class="row-actions">
                    ${auth.canWrite && !p.odoo_attendance_id && ['error', 'skipped', 'unmapped'].includes(p.process_state)
                      ? `<button type="button" class="icon-btn retry" data-retry="${esc(p.id)}" aria-label="Retry punch"
                                 title="Retry — queue it again for the next sync.">${RETRY_ICON}</button>
                         <button type="button" class="icon-btn" data-delete="${esc(p.id)}" aria-label="Delete punch"
                                 title="Delete — removes it from the queue. It won't be pushed, and won't come back on the next sync.">${TRASH_ICON}</button>` : ''}
                    ${auth.canWrite && p.process_state === 'deleted'
                      ? `<button class="sm" data-retry="${esc(p.id)}"
                                 title="Put it back in the queue for the next sync">Restore</button>` : ''}
                  </div></td>
                </tr>`).join('')}
            </tbody>
          </table>
        </div>
        ${punches.length >= limit && limit < 500 ? `
          <div class="row" style="justify-content:center;margin-top:12px">
            <a class="btn" id="showMore" href="${linkFor({ limit: String(Math.min(limit + 100, 500)) })}">Show more</a>
          </div>` : punches.length >= 500 ? '<div class="hint" style="text-align:center;margin-top:10px">Showing the latest 500 — narrow the filters to see older ones.</div>' : ''}
        ` : empty(
          'No Punches',
          filtered
            ? 'Nothing matches these filters. Widen the dates, or clear them.'
            : stateFilter
              ? `Nothing in the ${stateFilter} state.`
              : 'Run a sync to pull some.'
        )}
    </div>`;

  $('#punchFilters', mount).addEventListener('submit', (event) => {
    event.preventDefault();
    const values = readForm(event.target);
    window.location.hash = linkFor(values);
  });

  wireRunLogs(mount);
  // Show more loads the next rows under these without jumping back to the top; the selection
  // carries over (see BULK).
  $('#showMore', mount)?.addEventListener('click', async (event) => {
    event.preventDefault();
    const href = event.currentTarget.getAttribute('href');
    const y = window.scrollY;
    history.replaceState(null, '', href);
    const query = Object.fromEntries(new URLSearchParams(href.split('?')[1] || ''));
    await renderActivity(mount, { ...route, query });
    window.scrollTo(0, y);
  });

  if (anySelectable) {
    wireBulk(mount, () => renderActivity(mount, route), {
      // What "every matching punch" means: the filters on screen, across all pages.
      filter: { state: stateFilter || null, emp_code: badge || null, terminal_sn: terminal || null,
                run_id: runId || null, date_from: from || null, date_to: to || null },
      // A full page means there may be more beyond it.
      mayHaveMore: punches.length >= limit,
      total: totals?.matching,
    });
  }

  mount.querySelectorAll('[data-retry]').forEach((button) => {
    // An icon button has no text for busy() to swap, so it is disabled while the request runs.
    const iconOnly = button.classList.contains('icon-btn');
    button.addEventListener('click', () => {
      const run = () => guard(async () => {
        await api.post(`/punches/${button.dataset.retry}/retry`);
        await renderActivity(mount, route);
      }, 'Queued For The Next Sync');
      if (!iconOnly) return busy(button, run);
      button.disabled = true;
      button.classList.add('working');
      return run().finally(() => { button.disabled = false; button.classList.remove('working'); });
    });
  });

  // Two clicks, no confirm() dialog (nothing in this app uses one): the first
  // arms the button for a few seconds, the second deletes.
  mount.querySelectorAll('[data-delete]').forEach((button) => {
    let armed = null;
    const icon = button.innerHTML;
    button.addEventListener('click', () => {
      if (!armed) {
        // Armed: the muted icon turns into a plain "Confirm delete" button for
        // a few seconds, then goes back.
        button.textContent = 'Confirm Delete';
        button.classList.remove('icon-btn');
        button.classList.add('sm', 'danger');
        armed = setTimeout(() => {
          armed = null;
          button.innerHTML = icon;
          button.classList.remove('sm', 'danger');
          button.classList.add('icon-btn');
        }, 4000);
        return;
      }
      clearTimeout(armed);
      armed = null;
      busy(button, () =>
        guard(async () => {
          await api.del(`/punches/${button.dataset.delete}`);
          await renderActivity(mount, route);
        }, 'Punch Deleted')
      );
    });
  });
}
