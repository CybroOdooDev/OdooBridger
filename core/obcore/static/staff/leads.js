/* Staff console → Leads: website enquiries and demo requests, worked as a
 * pipeline (New → Contacted → Qualified → Won / Lost), on a board you drag
 * cards across or as a list. Demo is a sub-stage of Contacted. Every move and
 * note is in the lead's history. A won lead converts into an account.
 *
 * The board shows the three open stages as columns and one compact Closed
 * column (the latest Won and Lost), so closed leads never crowd the pipeline.
 * Search, service, owner and created-date filters work on the board and the
 * list. The four figures on top cover the whole pipeline over the last 30 days
 * (see services/leads.trends), whatever the filters say. */

import { money } from "/assets/api.js";
import { $, $$, empty, esc, field, fmtAgo, guard, loading, readForm, kpi, moneyInput, parseMoney, toast, viewSwitchHtml } from '/assets/ui.js';
import { dialog } from '/assets/shell.js';
import { detectCountryEntry } from '/site/geo.js';
import { multiSelect } from '/site/multiselect.js';
import { phoneCode } from '/site/phone-code.js';
import { api } from './client.js';
import { compare } from './compare.js';

const STAGES = [['new', 'New'], ['contacted', 'Contacted'], ['qualified', 'Qualified'], ['won', 'Won'], ['lost', 'Lost']];
const OPEN_STAGES = ['new', 'contacted', 'qualified'];
const LABEL = Object.fromEntries(STAGES);
const DEMOS = [['pending', 'Pending'], ['scheduled', 'Scheduled'], ['completed', 'Completed']];
const DEMO_LABEL = Object.fromEntries(DEMOS);
const TONE = { new: 'pending', contacted: 'trialing', qualified: 'trialing', won: 'success', lost: 'skipped' };
const FLOW = ['new', 'contacted', 'qualified', 'won'];
const LOST_REASONS = ['Price', 'Timing', 'No response', 'Chose a competitor', 'Not a fit', 'Other'];
const CLOSED_SHOWN = 5;
const STALE_AMBER_DAYS = 3;
const STALE_RED_DAYS = 7;
const RANGES = [['', 'Any time'], ['7', 'Last 7 days'], ['30', 'Last 30 days'], ['90', 'Last 90 days']];
const pill = (tone, text) => `<span class="pill ${esc(tone)}">${esc(text)}</span>`;
const demoPill = (r) => (r.demo_status ? `<span class="pill demo-pill ${esc(r.demo_status)}">Demo ${esc(DEMO_LABEL[r.demo_status]).toLowerCase()}</span>` : '');
const wantsDemo = (r) => r.topic === 'Demo' || !!r.preferred_date || !!r.demo_status;
const inStage = (r) => r.stage_changed_at || r.created_at;
const isOpen = (r) => OPEN_STAGES.includes(r.stage);
const daysSince = (iso) => (Date.now() - new Date(iso).getTime()) / 86400000;

const VIEW_KEY = 'ob.leads.view';
const getView = () => { try { return localStorage.getItem(VIEW_KEY) === 'list' ? 'list' : 'board'; } catch { return 'board'; } };
const setView = (v) => { try { localStorage.setItem(VIEW_KEY, v); } catch { /* convenience */ } };

// What the page knows about the staff and the catalog; filled by renderLeads.
let serviceNames = {};
let servicePrice = {};          // slug -> the default package's monthly price, for an estimate
let staffList = [];
let meId = null;
const filters = { q: '', service: '', owner: '', range: '', stage: '' };

const servicesLine = (r) => (r.services || []).map((s) => serviceNames[s] || s).join(', ');
const detailsLine = (r) => Object.entries(r.details || {}).filter(([, v]) => v)
  .map(([k, v]) => `${k.replace(/_/g, ' ')}: ${v}`).join(' · ');
function demoWhen(r) {
  if (!r.preferred_date) return '';
  return `${esc(r.preferred_date)}${r.preferred_window ? ` · ${esc(r.preferred_window.toLowerCase())}` : ''}${r.timezone ? ` <span class="hint">(${esc(r.timezone)})</span>` : ''}`;
}

/** The amount on a lead's card: what staff entered, else a guess from the services it asked about
 *  (the default package of each), marked as an estimate. */
function valueOf(r) {
  if (r.value_cents) return { cents: r.value_cents, est: false };
  const guess = (r.services || []).reduce((t, s) => t + (servicePrice[s] || 0), 0);
  return guess ? { cents: guess, est: true } : null;
}
const valueText = (r) => { const v = valueOf(r); return v ? `${v.est ? '~' : ''}${money(v.cents)}/mo${v.est ? ' est.' : ''}` : ''; };

const staffName = (s) => s.full_name || s.email;
const initials = (s) => staffName(s).split(/[\s@.]+/).filter(Boolean).slice(0, 2).map((w) => w[0].toUpperCase()).join('');
const ownerOf = (r) => staffList.find((s) => s.id === r.owner_staff_id) || null;
function ownerChip(r) {
  const o = ownerOf(r);
  return o ? `<span class="av" role="img" aria-label="Responsible: ${esc(staffName(o))}" data-tip="Responsible: ${esc(staffName(o))}">${esc(initials(o))}</span>`
    : '<span class="av none" role="img" aria-label="No one responsible" data-tip="No one is responsible yet. Open the lead to assign someone.">?</span>';
}

/** How stale an open lead is, by the time since its last activity (a move, note or demo change). */
function staleTone(r) {
  if (!isOpen(r)) return '';
  const d = daysSince(r.last_activity_at || inStage(r));
  return d >= STALE_RED_DAYS ? 'bad' : d >= STALE_AMBER_DAYS ? 'warn' : '';
}
function ageLine(r) {
  const tone = staleTone(r);
  const tip = `${fmtAgo(inStage(r)).replace(' ago', '')} in ${LABEL[r.stage]}. Amber after ${STALE_AMBER_DAYS} days without activity, red after ${STALE_RED_DAYS}.`;
  return `<span class="bc-age-t ${tone}" data-tip="${esc(tip)}">${esc(fmtAgo(r.last_activity_at || inStage(r)))}</span>`;
}

/* ---- follow-up date: a calendar day, compared with the viewer's today ---- */
const parseDay = (s) => { const [y, m, d] = s.split('-').map(Number); return new Date(y, m - 1, d); };
const localDay = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
const daysFromToday = (s) => { const t = new Date(); return Math.round((parseDay(s) - new Date(t.getFullYear(), t.getMonth(), t.getDate())) / 86400000); };
const dayText = (s, withYear = false) => parseDay(s).toLocaleDateString(undefined, { weekday: 'short', day: 'numeric', month: 'short', ...(withYear ? { year: 'numeric' } : {}) });
const FOLLOW_RED_DAYS = 7;
/** What the card says about the planned follow-up: the day, amber once it has passed, red after a week. */
function followUp(r) {
  if (!r.follow_up_on) {
    const stale = staleTone(r);
    return { text: 'Not set', tone: stale ? 'warn' : 'none',
      tip: stale ? `No follow-up planned and no activity for ${STALE_AMBER_DAYS}+ days. Open the lead to set one.` : 'No follow-up planned. Open the lead to set one.' };
  }
  const d = daysFromToday(r.follow_up_on);
  const tip = `Follow-up planned for ${dayText(r.follow_up_on, true)}.${d < 0 ? ` Amber once it has passed, red after ${FOLLOW_RED_DAYS} days.` : ''}`;
  if (d < 0) return { text: `Overdue ${-d} d`, tone: -d >= FOLLOW_RED_DAYS ? 'bad' : 'warn', tip };
  if (d === 0) return { text: 'Today', tone: 'today', tip };
  if (d === 1) return { text: 'Tomorrow', tone: '', tip };
  return { text: dayText(r.follow_up_on), tone: '', tip };
}

/* ---- the four figures ---------------------------------------------------------------------- */
const metric = (value, previous, basis) => ({
  value, previous, basis: previous == null ? '' : basis,
  change_pct: previous ? Math.round(((value - previous) / previous) * 1000) / 10 : null,
});
const points = (value, previous) => (value == null || previous == null ? '' : value === previous
  ? '<span class="cmp flat">No change</span> <span class="cmp-b">vs previous 30 days</span>'
  : `<span class="cmp ${value > previous ? 'up good' : 'down bad'}">${value > previous ? '▲' : '▼'} ${Math.abs(Math.round((value - previous) * 10) / 10)} pts</span> <span class="cmp-b">vs previous 30 days</span>`);

function summaryHtml(t, rows) {
  const openRows = rows.filter(isOpen);
  const noAmount = openRows.filter((r) => !r.value_cents).length;
  const join = (...parts) => parts.filter(Boolean).join(' · ');
  const add = '<button type="button" class="link" data-add-lead>Add a lead</button>';
  const conv = t.conversion;
  return `<div class="kpis four" style="margin-bottom:16px">
    ${kpi({ icon: 'mail', tone: 'acc', label: 'Open leads', value: t.open_leads.value,
      note: join(t.open_leads.value === 0 ? `No open leads. ${add}` : '', compare(metric(t.open_leads.value, t.open_leads.previous, '30 days ago'))) })}
    ${kpi({ icon: 'money', tone: 'ok', label: 'Pipeline value', value: money(t.pipeline_cents.value),
      title: 'Expected monthly revenue of the open leads, from the amount on each lead.',
      note: join(t.open_leads.value === 0 ? 'Nothing in the pipeline' : noAmount ? `${noAmount} open lead${noAmount === 1 ? ' has' : 's have'} no amount` : 'Expected monthly revenue',
        compare(metric(t.pipeline_cents.value, t.pipeline_cents.previous, '30 days ago'), true, (v) => money(v))) })}
    ${kpi({ icon: 'trophy', tone: 'ok', label: 'Won, 30 days', value: t.won.value,
      note: join(`${t.won.all_time} won in all`, compare(metric(t.won.value, t.won.previous, 'previous 30 days'))) })}
    ${kpi({ icon: 'percent', tone: 'acc', label: 'Conversion, 30 days', value: conv.value == null ? '—' : `${Math.round(conv.value)}%`,
      title: 'Of the leads that came in during the last 30 days, the share that are now won. Won leads divided by all leads created.',
      note: join(conv.total ? `${conv.won} of ${conv.total} new lead${conv.total === 1 ? '' : 's'} won${conv.total < 5 ? ' (few leads, read with care)' : ''}` : 'No new leads yet', points(conv.value, conv.previous)) })}
  </div>`;
}

/* ---- filters ------------------------------------------------------------------------------- */
function applyFilters(all) {
  const q = filters.q.trim().toLowerCase();
  const since = filters.range ? Date.now() - Number(filters.range) * 86400000 : null;
  return all.filter((r) => (!q || [r.name, r.email, r.company].some((x) => (x || '').toLowerCase().includes(q)))
    && (!filters.service || (r.services || []).includes(filters.service))
    && (!filters.owner || (filters.owner === 'none' ? !r.owner_staff_id : r.owner_staff_id === (filters.owner === 'me' ? meId : filters.owner)))
    && (since == null || new Date(r.created_at).getTime() >= since)
    && (!filters.stage || (filters.stage === 'closed' ? !isOpen(r) : filters.stage === 'open' ? isOpen(r) : r.stage === filters.stage)));
}
const filtered = (view) => Object.entries(filters).some(([k, v]) => v && (k !== 'stage' || view === 'list'));

/* ---- the board ----------------------------------------------------------------------------- */
const GRIP = '<svg viewBox="0 0 24 24" width="14" height="14" fill="currentColor" aria-hidden="true"><circle cx="9" cy="6" r="1.6"/><circle cx="15" cy="6" r="1.6"/><circle cx="9" cy="12" r="1.6"/><circle cx="15" cy="12" r="1.6"/><circle cx="9" cy="18" r="1.6"/><circle cx="15" cy="18" r="1.6"/></svg>';

/** One labelled line on a card: a small label, then the value. Every card has the same five. */
const cardRow = (label, value, tip = '') => `<div class="bc-line"><span class="bc-l"${tip ? ` data-tip="${esc(tip)}"` : ''}>${esc(label)}</span><span class="bc-v">${value}</span></div>`;

/** The services a lead asked about on one line: the first by name, the rest as a count ("BioBridge +2"),
 *  so a card is the same height however many there are. The full list is in the tooltip. */
function servicesCell(r) {
  const names = (r.services || []).map((s) => serviceNames[s] || s);
  if (!names.length) return '<span class="muted">None chosen</span>';
  return `<span class="bc-svc"${names.length > 1 ? ` data-tip="${esc(names.join(', '))}"` : ''}><span class="bc-svc-n">${esc(names[0])}</span>${names.length > 1 ? `<b class="bc-more">+${names.length - 1}</b>` : ''}</span>`;
}

function boardCard(r) {
  const demo = r.demo_status && isOpen(r) ? demoPill(r) : '';
  const v = valueOf(r);
  const f = followUp(r);
  return `<div class="board-card" draggable="true" data-id="${esc(r.id)}" tabindex="0" role="button" aria-label="${esc(r.name)}, ${esc(r.company)}">
    <div class="bc-top"><span class="bc-grip" aria-hidden="true" data-tip="Drag to another column">${GRIP}</span>
      <strong class="bc-name">${esc(r.name)}</strong>${ownerChip(r)}</div>
    <div class="hint bc-co">${esc(r.company)}</div>
    <div class="bc-lines">
      ${cardRow('Topic', `${esc(r.topic === 'Demo' ? 'Demo request' : r.topic)}${demo}`, 'What they asked for')}
      ${cardRow('Services', servicesCell(r), 'The services they asked about')}
      ${cardRow('Revenue', v ? `<span class="bc-val${v.est ? ' est' : ''}"${v.est ? ' data-tip="Estimated from the default package of the services they asked about. Enter an amount on the lead to replace it."' : ''}>${esc(valueText(r))}</span>` : '<span class="muted">—</span>', 'Expected revenue per month')}
      ${cardRow('Follow-up', `<span class="bc-fu ${f.tone}" data-tip="${esc(f.tip)}">${esc(f.text)}</span>`, 'The day to follow up')}
      ${cardRow('Activity', `<span class="bc-age">${ageLine(r)}</span>`, 'Time since the last move, note or change')}
    </div>
    ${r.stage === 'qualified' ? '<button type="button" class="sm bc-convert" data-convert>Convert to account</button>' : ''}
  </div>`;
}

/** A closed lead, one compact line: no drag handle, no extras. Won links to its account. */
function closedItem(r) {
  const val = valueText(r);
  const tail = r.stage === 'won'
    ? (r.account_id ? `<a class="bc-acct" href="#/accounts/${esc(r.account_id)}" data-tip="Open the account this lead became">→ ${esc(r.company)} account</a>` : '<span class="hint" data-tip="Won, but not converted to an account from here">No account linked</span>')
    : (r.lost_reason ? `<span class="hint">${esc(r.lost_reason)}</span>` : '<span class="hint">No reason recorded</span>');
  return `<div class="closed-item" data-id="${esc(r.id)}" draggable="${r.stage === 'lost'}" tabindex="0" role="button" aria-label="${esc(r.name)}, ${esc(r.company)}">
    <div class="ci-top"><strong>${esc(r.name)}</strong><span class="hint">${esc(fmtAgo(inStage(r)))}</span></div>
    <div class="hint">${esc(r.company)}${val ? ` · ${esc(val)}` : ''}</div>${tail}</div>`;
}

function boardHtml(rows, all) {
  const narrowed = rows.length !== all.length;
  const openCols = OPEN_STAGES.map((key) => {
    const here = rows.filter((r) => r.stage === key);
    const emptyText = narrowed ? 'No matching leads' : `No leads in ${LABEL[key]}`;
    return `<section class="board-col" data-stage="${key}" aria-label="${esc(LABEL[key])}">
      <header><span class="bc-title">${esc(LABEL[key])}</span><span class="count">${here.length}</span></header>
      <div class="board-drop">${here.map(boardCard).join('')
        || `<div class="bc-empty">${esc(emptyText)}${key === 'new' && !narrowed ? '<button type="button" class="sm" data-add-lead>Add lead</button>' : ''}</div>`}
        <div class="drop-hint">Drop to move to ${esc(LABEL[key])}</div></div></section>`;
  }).join('');
  const zone = (key) => {
    const here = rows.filter((r) => r.stage === key);
    return `<div class="closed-zone ${key}" data-stage="${key}">
      <h5><span>${esc(LABEL[key])}</span><span class="count">${here.length}</span></h5>
      ${here.slice(0, CLOSED_SHOWN).map(closedItem).join('') || `<div class="bc-empty slim">${narrowed ? 'None match' : `No ${LABEL[key].toLowerCase()} leads yet`}</div>`}
      ${here.length > CLOSED_SHOWN ? `<button type="button" class="link" data-closed-more="${key}">View all ${here.length}</button>` : ''}
      <div class="drop-hint">${key === 'won' ? 'Drop a qualified lead to create its account' : 'Drop to mark lost'}</div></div>`;
  };
  const closed = rows.filter((r) => !isOpen(r)).length;
  return `<div class="board">${openCols}
    <section class="board-col closed-col" aria-label="Closed">
      <header><span class="bc-title">Closed</span><span class="count">${closed}</span></header>
      <div class="board-drop">${zone('won')}${zone('lost')}</div></section></div>`;
}

const STAGE_FILTERS = [['', 'All stages'], ['open', 'Open'], ['closed', 'Closed'], ...STAGES];

function listHtml(rows, all) {
  if (!rows.length) {
    return `<div class="card">${all.length ? empty('No leads match', 'Try a different search or clear the filters.') : empty('No leads yet', 'Requests from the website’s contact form appear here.')}</div>`;
  }
  return `<div class="card"><div class="table-wrap"><table>
    <thead><tr><th>Lead</th><th>Services</th><th>Stage</th><th>Responsible</th><th>Follow-up</th><th>Last activity</th><th class="num">Value</th></tr></thead>
    <tbody>${rows.map((r) => `<tr class="click" data-id="${esc(r.id)}">
      <td><strong>${esc(r.name)}</strong> <span class="hint">· ${esc(r.company)}</span><div class="hint">${esc(r.email)}</div></td>
      <td>${esc(servicesLine(r) || '—')}</td>
      <td>${pill(TONE[r.stage] || 'mute', LABEL[r.stage] || r.stage)} ${isOpen(r) ? demoPill(r) : ''}${r.stage === 'lost' && r.lost_reason ? `<div class="hint">${esc(r.lost_reason)}</div>` : ''}</td>
      <td>${ownerOf(r) ? esc(staffName(ownerOf(r))) : '<span class="muted">Unassigned</span>'}</td>
      <td class="nowrap">${isOpen(r) ? `<span class="bc-fu ${followUp(r).tone}" data-tip="${esc(followUp(r).tip)}">${esc(followUp(r).text)}</span>` : '<span class="muted">—</span>'}</td>
      <td class="nowrap">${isOpen(r) ? ageLine(r) : esc(fmtAgo(inStage(r)))}</td>
      <td class="num">${valueText(r) ? esc(valueText(r)) : '—'}</td></tr>`).join('')}</tbody>
  </table></div></div>`;
}

function statusBar(stage) {
  const at = FLOW.indexOf(stage);
  const closed = stage === 'won';
  // Won isn't a stage you set by hand: a Qualified lead becomes Won when its account is created.
  const title = (key) => closed ? ' disabled title="Won leads are closed"'
    : key === 'won' ? (stage === 'qualified' ? ' title="Create the account to win this lead"' : ' disabled title="Qualify the lead first, then convert it to an account"')
      : ` title="Move to ${esc(LABEL[key])}"`;
  const step = (key, cls) => `<button type="button" class="wiz-step ${cls}" data-stage="${key}" aria-current="${key === stage ? 'step' : 'false'}"${title(key)}>
    <span class="wdot">${cls === 'done' ? '&#10003;' : ''}</span><span class="lbl">${esc(LABEL[key])}</span></button>`;
  return `<div class="wiz-bar" role="group" aria-label="Lead stage">
    ${FLOW.map((k, i) => step(k, stage === 'lost' ? '' : (i < at ? 'done' : i === at ? 'current' : ''))).join('<span class="wiz-line"></span>')}
    <span class="wiz-gap"></span>${step('lost', stage === 'lost' ? 'current lost' : 'lost')}</div>`;
}

function demoRow(r) {
  if (r.stage === 'new' || r.stage === 'won' || !wantsDemo(r)) return '';
  return `<span class="wiz-demo-label">Demo</span>
    <div class="seg" role="group" aria-label="Demo status">${DEMOS.map(([v, l]) => `
      <button type="button" class="seg-btn ${v} ${r.demo_status === v ? 'on' : ''}" data-demo="${v}" aria-pressed="${r.demo_status === v}">${esc(l)}</button>`).join('')}</div>`;
}

/** Dot colour by kind of entry: a stage move, a note someone wrote, or a system record (owner, demo, conversion). */
const eventType = (e) => (e.kind === 'note' ? 'note' : e.kind === 'stage' ? 'stage' : 'sys');
const HISTORY_KEY = '<ul class="ev-key" aria-label="Colour key"><li class="ev-stage">Stage</li><li class="ev-note">Note</li><li class="ev-sys">System</li></ul>';

function eventLine(e) {
  const who = `<div class="hint">${esc(e.actor || 'someone')} · ${esc(fmtAgo(e.created_at))}</div>`;
  const li = `<li class="ev-${eventType(e)}">`;
  if (e.kind === 'note') return `${li}<div>${esc(e.note)}</div>${who}</li>`;
  if (e.kind === 'followup') return `${li}<div>${esc(e.note || 'Follow-up changed')}</div>${who}</li>`;
  if (e.kind === 'owner') return `${li}<div>${esc(e.note || 'Responsible changed')}</div>${who}</li>`;
  if (e.kind === 'convert') return `${li}<div><strong>Converted</strong> · ${esc(e.note || '')}</div>${who}</li>`;
  if (e.kind === 'demo') {
    return `${li}<div>${e.to ? `Demo ${e.from ? `${esc((DEMO_LABEL[e.from] || e.from).toLowerCase())} → ` : 'set to '}<strong>${esc((DEMO_LABEL[e.to] || e.to).toLowerCase())}</strong>` : 'Demo status cleared'}</div>${who}</li>`;
  }
  return `${li}<div>${e.from ? `Moved ${esc(LABEL[e.from] || e.from)} → <strong>${esc(LABEL[e.to] || e.to)}</strong>` : `Came in as <strong>${esc(LABEL[e.to] || e.to)}</strong>`}${e.note ? ` · ${esc(e.note)}` : ''}</div>${who}</li>`;
}

function panelHtml(r) {
  return `<div class="wiz-backdrop" data-close></div>
    <div class="wiz" role="dialog" aria-modal="true" aria-label="Lead: ${esc(r.name)}">
      <header><div>
        <h3>${esc(r.name)} <span class="hint">· ${esc(r.company)}</span>${r.stage === 'won' ? ' <span class="pill ok">Won · closed</span>' : ''}</h3>
        <div class="hint"><a href="mailto:${esc(r.email)}">${esc(r.email)}</a>${r.phone ? ` · ${esc(r.phone)}` : ''}${r.country ? ` · ${esc(r.country)}` : ''} · came in ${esc(fmtAgo(r.created_at))}</div>
      </div>
      <div class="row" style="gap:8px">
        ${r.account_id ? `<a class="btn sm" href="#/accounts/${esc(r.account_id)}">Open account</a>`
          : r.stage === 'qualified' ? '<button class="sm primary" id="wConvert">Convert to account</button>'
          : r.stage === 'won' ? ''
          : '<span data-tip="Only a qualified lead can be converted. Move it to the Qualified stage first."><button class="sm" disabled>Convert to account</button></span>'}
        <button class="link wiz-x" type="button" data-close aria-label="Close">&times;</button></div></header>
      <div class="wiz-status" id="wStatus">${statusBar(r.stage)}</div>
      <div class="wiz-demo" id="wDemo">${demoRow(r)}</div>
      <div class="wiz-body"><div class="wiz-cols">
        <section>
          <h4>Request</h4>
          <div class="wiz-facts">
            <div><span class="hint">Topic</span><span class="pill mute">${esc(r.topic)}</span></div>
            ${servicesLine(r) ? `<div><span class="hint">Services</span><span>${esc(servicesLine(r))}</span></div>` : ''}
            ${r.preferred_date ? `<div><span class="hint">Wants a demo</span><strong>${demoWhen(r)}</strong></div>` : ''}
            ${detailsLine(r) ? `<div><span class="hint">Setup</span><span>${esc(detailsLine(r))}</span></div>` : ''}
            <div><span class="hint">Source</span><span>${r.source === 'staff' ? 'Added by staff' : 'Website form'}</span></div>
            <div><span class="hint">Responsible</span>${r.stage === 'won'
              ? `<span>${ownerOf(r) ? esc(staffName(ownerOf(r))) : 'Unassigned'}</span>`
              : `<span class="owner-pick"><select id="wOwner" aria-label="Responsible"><option value="">Unassigned</option>${staffList.map((m) =>
                `<option value="${esc(m.id)}" ${m.id === r.owner_staff_id ? 'selected' : ''}>${esc(staffName(m))}${m.id === meId ? ' (you)' : ''}</option>`).join('')}</select>
                <button type="button" class="link${r.owner_staff_id === meId ? ' hidden' : ''}" id="wAssignMe">Assign to me</button></span>`}</div>
          </div>
          ${r.message ? `<p class="wiz-msg">${esc(r.message)}</p>` : ''}
          <div class="grid cols-2" style="margin-top:12px">
            <div><label for="wValue">Expected revenue per month</label>
              ${r.stage === 'won' ? `<div class="wiz-fixed">${r.value_cents != null ? `${money(r.value_cents)}/mo` : '—'}</div>` : moneyInput({ id: 'wValue', value_cents: r.value_cents })}</div>
            <div><label for="wDemoAt">Demo date</label>
              ${r.stage === 'won' ? `<div class="wiz-fixed">${r.demo_at ? esc(new Date(r.demo_at).toLocaleString()) : '—'}</div>`
                : `<input id="wDemoAt" type="datetime-local" value="${r.demo_at ? r.demo_at.slice(0, 16) : ''}">`}</div>
          </div>
          ${r.stage !== 'won' ? `<div id="wFollowWrap" class="${r.stage === 'lost' ? 'hidden' : ''}" style="margin-top:12px"><label for="wFollow">Follow up on</label>
            <div class="follow-pick"><input id="wFollow" type="date" value="${esc(r.follow_up_on || '')}">
              <span class="follow-presets" role="group" aria-label="Quick dates">
                <button type="button" class="link" data-follow="1">Tomorrow</button>
                <button type="button" class="link" data-follow="3">In 3 days</button>
                <button type="button" class="link" data-follow="7">Next week</button>
                <button type="button" class="link" data-follow="">Clear</button></span></div></div>` : ''}
          <div id="wLostWrap" class="${r.stage === 'lost' ? '' : 'hidden'}" style="margin-top:12px">
            <label for="wLost">Why was it lost?</label>
            <input id="wLost" type="text" maxlength="160" value="${esc(r.lost_reason || '')}" placeholder="Price, timing, no response…">
          </div>
        </section>
        <section>
          <h4>History</h4>
          ${HISTORY_KEY}
          <div class="row" style="gap:8px;margin-bottom:10px">
            <input id="wNote" type="text" maxlength="4000" placeholder="Add to the history — “called, voicemail”" style="flex:1">
            <button class="sm" id="wAddNote">Add</button></div>
          <ul class="timeline" id="wTimeline">${(r.events || []).map(eventLine).join('')}</ul>
        </section>
      </div></div>
      <footer class="wiz-foot">
        <span class="wiz-saved" id="wSaved" role="status" aria-live="polite"></span>
        <div class="row" style="gap:8px">
          <button type="button" data-close>Close</button>
          ${r.stage === 'won' ? '' : '<button type="button" class="primary hidden" id="wSave">Save changes</button>'}
        </div>
      </footer>
    </div>`;
}

async function newLeadDialog(services) {
  let picker = null;
  let dial = null;
  const opened = dialog({
    title: 'New lead', submitLabel: 'Add lead', wide: true,
    body: `<div class="grid cols-2">
      ${field({ name: 'name', label: 'Name', required: true })}${field({ name: 'company', label: 'Company', required: true })}
      ${field({ name: 'email', label: 'Email', type: 'email', required: true })}
      <div class="field"><label for="leadPhone">Contact number <span class="opt">optional</span></label>
        <div class="phone-row"><div id="leadDial"></div><input id="leadPhone" name="phone" type="tel" placeholder="Phone number"></div></div>
      <div class="field"><label>Interested services <span class="opt">optional</span></label><div id="leadServices"></div></div>
      <div class="field"><label for="leadValue">Expected revenue per month <span class="opt">optional</span></label>
        ${moneyInput({ id: 'leadValue', name: 'value' })}</div>
    </div>
    ${field({ name: 'message', label: 'Notes', type: 'textarea' })}`,
    onSubmit: (form) => {
      const v = readForm(form);
      const number = (v.phone || '').trim();
      return api.post('/staff/leads', {
        topic: 'Sales question', name: v.name, company: v.company, email: v.email,
        phone: number ? `${dial.dial()} ${number}` : null,
        country: number ? dial.country() : null, message: v.message || null,
        value_cents: parseMoney(v.value),
        services: picker.values(),
      });
    },
  });
  picker = multiSelect($('#leadServices'), services.map((s) => ({ value: s.slug, label: s.name })),
    { placeholder: 'Select one or more services' });
  dial = phoneCode($('#leadDial'), detectCountryEntry());
  return opened;
}

/** Ask why a lead was lost, so the funnel can be fixed later. Resolves { reason } or null. */
function askLostReason(lead) {
  return dialog({
    title: 'Mark as lost', submitLabel: 'Mark lost', danger: true,
    body: `<p>Why did ${esc(lead.name)} (${esc(lead.company)}) not go ahead? The reasons add up to show which part of the funnel to fix.</p>
      ${field({ name: 'reason', label: 'Reason', required: true, options: LOST_REASONS.map((r) => ({ value: r, label: r })) })}
      ${field({ name: 'detail', label: 'Details' })}`,
    onSubmit: (form) => {
      const v = readForm(form);
      return Promise.resolve({ reason: (v.detail || '').trim() ? `${v.reason}: ${v.detail.trim()}`.slice(0, 160) : v.reason });
    },
  });
}

/** Turn a lead into an account: its owner, company and services carry over. Resolves the API answer or null. */
function convertDialog(lead, services) {
  const picks = services.filter((s) => s.is_active);
  return dialog({
    title: `Create an account for ${lead.company}`, submitLabel: 'Create account',
    body: `<p>${esc(lead.name)} &lt;${esc(lead.email)}&gt; becomes the owner and gets an email to set their password. The lead moves to Won and links to the account.</p>
      <label>Services to start (as trials)</label>
      ${picks.map((s) => `<label style="font-weight:400"><input type="checkbox" data-svc="${esc(s.slug)}" ${(lead.services || []).includes(s.slug) ? 'checked' : ''}> ${esc(s.name)}</label>`).join('')}`,
    onSubmit: (form) => api.post(`/staff/leads/${lead.id}/convert`, {
      services: $$('[data-svc]', form).filter((c) => c.checked).map((c) => ({ service: c.dataset.svc })),
    }),
  });
}

export async function renderLeads(mount, route = { query: {} }) {
  mount.innerHTML = loading();
  const [pipeline, services, trends, me] = await Promise.all([
    api.get('/staff/leads/pipeline'), api.get('/staff/services'), api.get('/staff/leads/trends?days=30'),
    meId ? Promise.resolve({ id: meId }) : api.get('/auth/staff/me')]);
  meId = me.id;
  staffList = pipeline.staff || [];
  serviceNames = Object.fromEntries(services.map((s) => [s.slug, s.name]));
  servicePrice = Object.fromEntries(services.map((s) => {
    const live = (s.packages || []).filter((p) => p.is_active);
    const pick = live.find((p) => p.is_default) || live.sort((a, b) => a.monthly_price_cents - b.monthly_price_cents)[0];
    return [s.slug, pick ? pick.monthly_price_cents : 0];
  }));
  let all = Object.values(pipeline.stages).flat();
  let byId = new Map(all.map((r) => [r.id, r]));
  let view = getView();
  let shown = [];

  mount.innerHTML = `
    <div id="leadKpis">${summaryHtml(trends, all)}</div>
    <div class="filterbar" id="leadBar">
      <label class="fb-search"><svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>
        <input id="lq" type="search" placeholder="Search name, company or email" value="${esc(filters.q)}" aria-label="Search leads"></label>
      <select id="lService" aria-label="Service"><option value="">All services</option>${services.map((s) =>
        `<option value="${esc(s.slug)}" ${filters.service === s.slug ? 'selected' : ''}>${esc(s.name)}</option>`).join('')}</select>
      <select id="lOwner" aria-label="Responsible"><option value="">Anyone responsible</option><option value="me" ${filters.owner === 'me' ? 'selected' : ''}>My leads</option>
        <option value="none" ${filters.owner === 'none' ? 'selected' : ''}>Unassigned</option>${staffList.filter((m) => m.id !== meId).map((m) =>
        `<option value="${esc(m.id)}" ${filters.owner === m.id ? 'selected' : ''}>${esc(staffName(m))}</option>`).join('')}</select>
      <select id="lRange" aria-label="Created" data-tip="When the lead came in">${RANGES.map(([v, l]) =>
        `<option value="${v}" ${filters.range === v ? 'selected' : ''}>${l}</option>`).join('')}</select>
      <select id="lStage" aria-label="Stage" class="${view === 'list' ? '' : 'hidden'}">${STAGE_FILTERS.map(([v, l]) =>
        `<option value="${v}" ${filters.stage === v ? 'selected' : ''}>${l}</option>`).join('')}</select>
      <button class="link hidden" id="lClear">Clear filters</button>
      <span class="fb-count muted" id="lCount"></span>
      <span class="fb-spacer"></span>
      <span id="lView">${viewSwitchHtml([['board', 'Board'], ['list', 'List']], view)}</span>
      <button class="primary" id="newLead">New lead</button>
    </div>
    <div id="leadsBody"></div>
    <div id="leadPanel"></div>`;

  const reload = async () => {
    const fresh = await api.get('/staff/leads/pipeline');
    const t = await api.get('/staff/leads/trends?days=30');
    staffList = fresh.staff || staffList;
    all = Object.values(fresh.stages).flat();
    byId = new Map(all.map((r) => [r.id, r]));
    $('#leadKpis', mount).innerHTML = summaryHtml(t, all);
    bindAdd();
    draw();
  };
  const bindAdd = () => $$('[data-add-lead]', mount).forEach((b) => { b.onclick = async () => { if (await newLeadDialog(services)) reload(); }; });

  /* ---- paint the body: board or list, filtered ---- */
  const openPanel = async (id, { focusLost = false } = {}) => {
    const lead = await api.get(`/staff/leads/${id}`);
    const host = $('#leadPanel', mount);
    host.innerHTML = panelHtml(lead);
    let changed = false;
    const close = () => { document.removeEventListener('keydown', onKey); host.innerHTML = ''; if (changed) reload(); };
    const onKey = (e) => { if (e.key === 'Escape' && !$('.wiz-backdrop:not(#leadPanel .wiz-backdrop)')) close(); };
    document.addEventListener('keydown', onKey);
    $$('[data-close]', host).forEach((el) => el.addEventListener('click', close));
    const refreshHistory = async () => {
      const fresh = await api.get(`/staff/leads/${id}`);
      $('#wTimeline', host).innerHTML = fresh.events.map(eventLine).join('');
    };
    /** The result of a change shows at the foot of the panel, where the eye already is, not in a toast. */
    const say = (text) => { const out = $('#wSaved', host); if (out) out.textContent = text; };
    const act = async (fn, message) => {
      const ok = await guard(async () => { await fn(); return true; });
      if (ok && message) say(message);
      return ok;
    };
    // Save changes shows only while one of the three editable fields differs from what was last saved.
    const snap = () => ({ value: parseMoney($('#wValue', host)?.value), demo: $('#wDemoAt', host)?.value || '',
      follow: $('#wFollow', host)?.value || '',
      lost: lead.stage === 'lost' ? ($('#wLost', host)?.value || '').trim() : '' });
    const base = snap();
    const syncSave = () => {
      const btn = $('#wSave', host);
      if (!btn) return;
      const now = snap();
      const dirty = Object.keys(now).some((k) => now[k] !== base[k]);
      btn.classList.toggle('hidden', !dirty);
      if (dirty) say('');
    };
    $$('[data-follow]', host).forEach((b) => b.addEventListener('click', () => {
      const input = $('#wFollow', host);
      if (b.dataset.follow === '') input.value = '';
      else { const d = new Date(); d.setDate(d.getDate() + Number(b.dataset.follow)); input.value = localDay(d); }
      input.dispatchEvent(new Event('input', { bubbles: true }));
    }));
    ['#wValue', '#wDemoAt', '#wFollow', '#wLost'].forEach((sel) => ['input', 'change'].forEach((ev) => $(sel, host)?.addEventListener(ev, syncSave)));
    const paintDemo = () => {
      const box = $('#wDemo', host);
      box.innerHTML = demoRow(lead);
      $$('[data-demo]', box).forEach((b) => b.addEventListener('click', () => {
        const to = b.dataset.demo === lead.demo_status ? null : b.dataset.demo;
        act(async () => {
          Object.assign(lead, await api.patch(`/staff/leads/${id}`, { demo_status: to }));
          changed = true;
          $('#wStatus', host).innerHTML = statusBar(lead.stage);
          bindBar();
          paintDemo();
          await refreshHistory();
        }, to ? `Demo ${DEMO_LABEL[to].toLowerCase()}` : 'Demo status cleared');
      }));
    };
    const bindBar = () => $$('.wiz-step', host).forEach((b) => b.addEventListener('click', async () => {
      const to = b.dataset.stage;
      if (to === lead.stage || lead.stage === 'won') return;
      if (to === 'won') { convertFlow(); return; }
      let reason = null;
      if (to === 'lost') {
        const asked = await askLostReason(lead);
        if (!asked) return;
        reason = asked.reason;
      }
      act(async () => {
        Object.assign(lead, await api.patch(`/staff/leads/${id}`, reason ? { stage: to, lost_reason: reason } : { stage: to }));
        changed = true;
        $('#wStatus', host).innerHTML = statusBar(lead.stage);
        bindBar();
        paintDemo();
        if (lead.stage === 'won') {   // now closed: show it again, read-only
          document.removeEventListener('keydown', onKey);
          await reload();
          await openPanel(id);
          return;
        }
        $('#wLostWrap', host).classList.toggle('hidden', lead.stage !== 'lost');
        if (lead.stage === 'lost') $('#wLost', host).value = lead.lost_reason || '';
        $('#wFollowWrap', host)?.classList.toggle('hidden', lead.stage === 'lost');
        if ($('#wFollow', host)) { $('#wFollow', host).value = lead.follow_up_on || ''; base.follow = $('#wFollow', host).value; }   // closing clears it
        base.lost = snap().lost;   // the reason was just saved with the move; value and date edits stay pending
        syncSave();
        await refreshHistory();
      }, `Moved to ${LABEL[to]}`);
    }));
    // Creating the account is what wins a Qualified lead: it moves to Won, closed, linked to the account.
    async function convertFlow() {
      const ok = await convertDialog(lead, services);
      if (ok) { toast(`${ok.account.name} created. The lead is now Won.`, 'ok'); document.removeEventListener('keydown', onKey); host.innerHTML = ''; reload(); window.location.hash = `#/accounts/${ok.account.id}`; }
    }
    bindBar();
    paintDemo();
    const setOwner = (value) => act(async () => {
      Object.assign(lead, await api.patch(`/staff/leads/${id}`, { owner_staff_id: value || null }));
      changed = true;
      const pick = $('#wOwner', host);
      if (pick) pick.value = lead.owner_staff_id || '';
      $('#wAssignMe', host)?.classList.toggle('hidden', lead.owner_staff_id === meId);
      await refreshHistory();
    }, 'Responsible saved');
    $('#wOwner', host)?.addEventListener('change', (e) => setOwner(e.target.value));
    $('#wAssignMe', host)?.addEventListener('click', () => setOwner(meId));
    $('#wSave', host)?.addEventListener('click', () => act(async () => {
      const value = $('#wValue', host).value;
      const demoAt = $('#wDemoAt', host).value;
      const body = { value_cents: parseMoney(value) };
      if (demoAt) body.demo_at = new Date(demoAt).toISOString();
      if ($('#wFollow', host)) body.follow_up_on = $('#wFollow', host).value || null;
      if (lead.stage === 'lost') { body.stage = 'lost'; body.lost_reason = $('#wLost', host).value; }
      Object.assign(lead, await api.patch(`/staff/leads/${id}`, body));
      changed = true;
      Object.assign(base, snap());
      $('#wSave', host).classList.add('hidden');
      await refreshHistory();
    }, 'Saved changes'));
    const note = $('#wNote', host);
    const addNote = () => note.value.trim() && act(async () => {
      await api.post(`/staff/leads/${id}/notes`, { note: note.value.trim() });
      note.value = '';
      changed = true;
      await refreshHistory();
    }, 'Note added');
    $('#wAddNote', host).addEventListener('click', addNote);
    note.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); addNote(); } });
    $('#wConvert', host)?.addEventListener('click', convertFlow);
    if (focusLost) $('#wLost', host)?.focus();
  };

  /** Move a lead to a stage (from a drop). Lost asks why first; Won is closed and can't be reopened. */
  const dropTo = async (id, to) => {
    const lead = byId.get(id);
    if (!lead || lead.stage === to) return;
    if (lead.stage === 'won') { toast('Won leads are closed and stay in Won.', 'warn'); return; }
    if (to === 'won') {   // Won happens by creating the account
      if (lead.stage !== 'qualified') { toast('Only a qualified lead can be won. Move it to the Qualified stage, then convert it to an account.', 'warn'); return; }
      const ok = await convertDialog(lead, services);
      if (ok) { toast(`${ok.account.name} created. The lead is now Won.`, 'ok'); window.location.hash = `#/accounts/${ok.account.id}`; }
      return;
    }
    let body = { stage: to };
    if (to === 'lost') {
      const asked = await askLostReason(lead);
      if (!asked) return;
      body = { stage: to, lost_reason: asked.reason };
    }
    if (await guard(() => api.patch(`/staff/leads/${id}`, body))) await reload();
  };

  const draw = () => {
    shown = applyFilters(all);
    const body = $('#leadsBody', mount);
    body.innerHTML = view === 'board' ? boardHtml(shown, all) : listHtml(shown, all);
    const narrowed = filtered(view);
    $('#lCount', mount).textContent = `${narrowed ? `${shown.length} of ${all.length}` : all.length} lead${all.length === 1 ? '' : 's'}`;
    $('#lClear', mount).classList.toggle('hidden', !narrowed);
    bindAdd();
    $$('tr[data-id]', body).forEach((tr) => tr.addEventListener('click', () => openPanel(tr.dataset.id)));
    $$('.board-card, .closed-item', body).forEach((card) => {
      const open = () => openPanel(card.dataset.id);
      card.addEventListener('click', (e) => { if (!e.target.closest('a, button')) open(); });
      card.addEventListener('keydown', (e) => { if (e.key === 'Enter' && e.target === card) open(); });
      card.addEventListener('dragstart', (e) => { e.dataTransfer.setData('text/plain', card.dataset.id); card.classList.add('dragging'); body.querySelector('.board')?.classList.add('is-dragging'); });
      card.addEventListener('dragend', () => { card.classList.remove('dragging'); body.querySelector('.board')?.classList.remove('is-dragging'); });
    });
    $$('[data-convert]', body).forEach((b) => b.addEventListener('click', async (e) => {
      e.stopPropagation();
      const lead = byId.get(b.closest('[data-id]').dataset.id);
      const ok = await convertDialog(lead, services);
      if (ok) { toast(`${ok.account.name} created.`, 'ok'); window.location.hash = `#/accounts/${ok.account.id}`; }
    }));
    $$('[data-closed-more]', body).forEach((b) => b.addEventListener('click', () => {
      filters.stage = b.dataset.closedMore;
      view = 'list'; setView('list');
      syncBar();
      draw();
    }));
    $$('[data-stage]', body).forEach((col) => {
      col.addEventListener('dragover', (e) => { e.preventDefault(); col.classList.add('over'); });
      col.addEventListener('dragleave', (e) => { if (!col.contains(e.relatedTarget)) col.classList.remove('over'); });
      col.addEventListener('drop', (e) => {
        e.preventDefault();
        col.classList.remove('over');
        body.querySelector('.board')?.classList.remove('is-dragging');
        const id = e.dataTransfer.getData('text/plain');
        const to = col.dataset.stage;
        setTimeout(() => dropTo(id, to), 0);
      });
    });
  };

  /* ---- the bar ---- */
  const syncBar = () => {
    $('#lView', mount).innerHTML = viewSwitchHtml([['board', 'Board'], ['list', 'List']], view);
    bindView();
    $('#lStage', mount).classList.toggle('hidden', view !== 'list');
    $('#lStage', mount).value = filters.stage;
  };
  const bindView = () => $$('[data-view]', mount).forEach((b) => b.addEventListener('click', () => {
    view = b.dataset.view; setView(view);
    if (view === 'board') filters.stage = '';
    syncBar(); draw();
  }));
  let timer;
  $('#lq', mount).addEventListener('input', (e) => { clearTimeout(timer); timer = setTimeout(() => { filters.q = e.target.value; draw(); }, 200); });
  [['lService', 'service'], ['lOwner', 'owner'], ['lRange', 'range'], ['lStage', 'stage']].forEach(([id, key]) =>
    $(`#${id}`, mount).addEventListener('change', (e) => { filters[key] = e.target.value; draw(); }));
  $('#lClear', mount).addEventListener('click', () => {
    Object.assign(filters, { q: '', service: '', owner: '', range: '', stage: '' });
    $('#lq', mount).value = '';
    ['lService', 'lOwner', 'lRange', 'lStage'].forEach((id) => { $(`#${id}`, mount).value = ''; });
    draw();
  });
  $('#newLead', mount).addEventListener('click', async () => { if (await newLeadDialog(services)) reload(); });
  bindView();
  bindAdd();
  draw();
  if (route.query?.open) openPanel(route.query.open);
}
