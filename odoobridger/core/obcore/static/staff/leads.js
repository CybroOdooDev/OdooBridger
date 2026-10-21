/* Staff console → Leads: website enquiries and demo requests, worked as a
 * pipeline (New → Contacted → Qualified → Won / Lost), on a board you drag
 * cards across or as a list. Demo is a sub-stage of Contacted. Every move and
 * note is in the lead's history. A won lead converts into an account. */

import { money } from "/assets/api.js";
import { $, $$, empty, esc, field, fmtAgo, guard, loading, readForm, stat, toast, viewSwitchHtml } from '/assets/ui.js';
import { dialog } from '/assets/shell.js';
import { api } from './client.js';

const STAGES = [['new', 'New'], ['contacted', 'Contacted'], ['qualified', 'Qualified'], ['won', 'Won'], ['lost', 'Lost']];
const LABEL = Object.fromEntries(STAGES);
const DEMOS = [['pending', 'Pending'], ['scheduled', 'Scheduled'], ['completed', 'Completed']];
const DEMO_LABEL = Object.fromEntries(DEMOS);
const TONE = { new: 'pending', contacted: 'trialing', qualified: 'trialing', won: 'success', lost: 'skipped' };
const FLOW = ['new', 'contacted', 'qualified', 'won'];
const pill = (tone, text) => `<span class="pill ${esc(tone)}">${esc(text)}</span>`;
const demoPill = (r) => (r.demo_status ? `<span class="pill demo-pill ${esc(r.demo_status)}">Demo ${esc(DEMO_LABEL[r.demo_status]).toLowerCase()}</span>` : '');
const wantsDemo = (r) => r.topic === 'Demo' || !!r.preferred_date || !!r.demo_status;
const inStage = (r) => r.stage_changed_at || r.created_at;

const VIEW_KEY = 'ob.leads.view';
const getView = () => { try { return localStorage.getItem(VIEW_KEY) === 'list' ? 'list' : 'board'; } catch { return 'board'; } };
const setView = (v) => { try { localStorage.setItem(VIEW_KEY, v); } catch { /* convenience */ } };

let serviceNames = {};
const servicesLine = (r) => (r.services || []).map((s) => serviceNames[s] || s).join(', ');
const detailsLine = (r) => Object.entries(r.details || {}).filter(([, v]) => v)
  .map(([k, v]) => `${k.replace(/_/g, ' ')}: ${v}`).join(' · ');
function demoWhen(r) {
  if (!r.preferred_date) return '';
  return `${esc(r.preferred_date)}${r.preferred_window ? ` · ${esc(r.preferred_window.toLowerCase())}` : ''}${r.timezone ? ` <span class="hint">(${esc(r.timezone)})</span>` : ''}`;
}

function summaryHtml(rows) {
  const count = (s) => rows.filter((r) => r.stage === s).length;
  const open = rows.filter((r) => ['new', 'contacted', 'qualified'].includes(r.stage));
  const won = count('won');
  const lost = count('lost');
  const value = open.reduce((t, r) => t + (r.value_cents || 0), 0);
  return `<div class="grid cols-4" style="margin-bottom:16px">
    ${stat({ label: 'Open Leads', value: open.length, note: 'still in play' })}
    ${stat({ label: 'Pipeline Value', value: money(value), note: 'per month, open leads' })}
    ${stat({ label: 'Won', value: won, note: `${lost} lost` })}
    ${stat({ label: 'Conversion', value: won + lost ? `${Math.round((won / (won + lost)) * 100)}%` : '—', note: 'won ÷ (won + lost)' })}
  </div>`;
}

function boardCard(r) {
  return `<div class="board-card" draggable="true" data-id="${esc(r.id)}" tabindex="0" role="button" aria-label="${esc(r.name)}, ${esc(r.company)}">
    <strong class="bc-name">${esc(r.name)}</strong>
    <div class="hint bc-co">${esc(r.company)}</div>
    <div class="bc-topic"><span class="pill mute">${esc(r.topic)}</span>${r.demo_status ? ` ${demoPill(r)}` : ''}</div>
    ${servicesLine(r) ? `<div class="hint">${esc(servicesLine(r))}</div>` : ''}
    ${r.value_cents ? `<div class="hint">${money(r.value_cents)}/mo</div>` : ''}
    <div class="hint bc-age">${esc(fmtAgo(inStage(r)))} in ${esc(LABEL[r.stage] || r.stage)}</div>
  </div>`;
}

function boardHtml(rows) {
  return `<div class="board">${STAGES.map(([key, label]) => {
    const here = rows.filter((r) => r.stage === key);
    return `<section class="board-col" data-stage="${key}" aria-label="${esc(label)}">
      <header><span class="bc-title">${esc(label)}</span><span class="count">${here.length}</span></header>
      <div class="board-drop">${here.map(boardCard).join('')}</div></section>`;
  }).join('')}</div>`;
}

function listHtml(rows) {
  if (!rows.length) return `<div class="card">${empty('No Leads Yet', 'Requests from the website’s contact form appear here.')}</div>`;
  return `<div class="card"><div class="table-wrap"><table>
    <thead><tr><th>Lead</th><th>Services</th><th>Stage</th><th>Came in</th><th class="num">Value</th></tr></thead>
    <tbody>${rows.map((r) => `<tr class="click" data-id="${esc(r.id)}">
      <td><strong>${esc(r.name)}</strong> <span class="hint">· ${esc(r.company)}</span><div class="hint">${esc(r.email)}</div></td>
      <td>${esc(servicesLine(r) || '—')}</td>
      <td>${pill(TONE[r.stage] || 'mute', LABEL[r.stage] || r.stage)} ${demoPill(r)}</td>
      <td class="nowrap">${esc(fmtAgo(r.created_at))}</td><td class="num">${r.value_cents ? money(r.value_cents) : '—'}</td></tr>`).join('')}</tbody>
  </table></div></div>`;
}

function statusBar(stage) {
  const at = FLOW.indexOf(stage);
  const step = (key, cls) => `<button type="button" class="wiz-step ${cls}" data-stage="${key}" aria-current="${key === stage ? 'step' : 'false'}" title="Move to ${esc(LABEL[key])}">
    <span class="wdot">${cls === 'done' ? '&#10003;' : ''}</span><span class="lbl">${esc(LABEL[key])}</span></button>`;
  return `<div class="wiz-bar" role="group" aria-label="Lead stage">
    ${FLOW.map((k, i) => step(k, stage === 'lost' ? '' : (i < at ? 'done' : i === at ? 'current' : ''))).join('<span class="wiz-line"></span>')}
    <span class="wiz-gap"></span>${step('lost', stage === 'lost' ? 'current lost' : 'lost')}</div>`;
}

function demoRow(r) {
  if (r.stage === 'new' || !wantsDemo(r)) return '';
  return `<span class="wiz-demo-label">Demo</span>
    <div class="seg" role="group" aria-label="Demo status">${DEMOS.map(([v, l]) => `
      <button type="button" class="seg-btn ${v} ${r.demo_status === v ? 'on' : ''}" data-demo="${v}" aria-pressed="${r.demo_status === v}">${esc(l)}</button>`).join('')}</div>`;
}

function eventLine(e) {
  const who = `<div class="hint">${esc(e.actor || 'someone')} · ${esc(fmtAgo(e.created_at))}</div>`;
  if (e.kind === 'note') return `<li><div>${esc(e.note)}</div>${who}</li>`;
  if (e.kind === 'convert') return `<li><div><strong>Converted</strong> · ${esc(e.note || '')}</div>${who}</li>`;
  if (e.kind === 'demo') {
    return `<li><div>${e.to ? `Demo ${e.from ? `${esc((DEMO_LABEL[e.from] || e.from).toLowerCase())} → ` : 'set to '}<strong>${esc((DEMO_LABEL[e.to] || e.to).toLowerCase())}</strong>` : 'Demo status cleared'}</div>${who}</li>`;
  }
  return `<li><div>${e.from ? `Moved ${esc(LABEL[e.from] || e.from)} → <strong>${esc(LABEL[e.to] || e.to)}</strong>` : `Came in as <strong>${esc(LABEL[e.to] || e.to)}</strong>`}${e.note ? ` · ${esc(e.note)}` : ''}</div>${who}</li>`;
}

function panelHtml(r) {
  return `<div class="wiz-backdrop" data-close></div>
    <div class="wiz" role="dialog" aria-modal="true" aria-label="Lead: ${esc(r.name)}">
      <header><div>
        <h3>${esc(r.name)} <span class="hint">· ${esc(r.company)}</span></h3>
        <div class="hint"><a href="mailto:${esc(r.email)}">${esc(r.email)}</a>${r.phone ? ` · ${esc(r.phone)}` : ''}${r.country ? ` · ${esc(r.country)}` : ''} · came in ${esc(fmtAgo(r.created_at))}</div>
      </div>
      <div class="row" style="gap:8px">
        ${r.account_id ? `<a class="btn sm" href="#/accounts/${esc(r.account_id)}">Open Account</a>` : '<button class="sm primary" id="wConvert">Convert To Account</button>'}
        <button class="sm" data-close aria-label="Close">Close</button></div></header>
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
            ${r.source === 'staff' ? '<div><span class="hint">Source</span><span>Added by staff</span></div>' : ''}
          </div>
          ${r.message ? `<p class="wiz-msg">${esc(r.message)}</p>` : ''}
          <div class="grid cols-2" style="margin-top:12px">
            <div><label for="wValue">Expected value per month</label>
              <input id="wValue" type="number" min="0" step="1" value="${r.value_cents != null ? r.value_cents / 100 : ''}" placeholder="0"></div>
            <div><label for="wDemoAt">Demo date</label>
              <input id="wDemoAt" type="datetime-local" value="${r.demo_at ? r.demo_at.slice(0, 16) : ''}"></div>
          </div>
          <div id="wLostWrap" class="${r.stage === 'lost' ? '' : 'hidden'}" style="margin-top:12px">
            <label for="wLost">Why was it lost? <span class="opt">optional</span></label>
            <input id="wLost" type="text" maxlength="160" value="${esc(r.lost_reason || '')}" placeholder="Too expensive, chose a competitor, no response…">
          </div>
          <div class="row" style="margin-top:12px"><button class="primary" id="wSave">Save</button></div>
        </section>
        <section>
          <h4>History</h4>
          <div class="row" style="gap:8px;margin-bottom:10px">
            <input id="wNote" type="text" maxlength="4000" placeholder="Add to the history — “called, voicemail”" style="flex:1">
            <button class="sm" id="wAddNote">Add</button></div>
          <ul class="timeline" id="wTimeline">${(r.events || []).map(eventLine).join('')}</ul>
        </section>
      </div></div>
    </div>`;
}

async function newLeadDialog(services) {
  return dialog({
    title: 'New Lead', submitLabel: 'Add Lead', wide: true,
    body: `<div class="grid cols-2">
      ${field({ name: 'name', label: 'Name', required: true })}${field({ name: 'company', label: 'Company', required: true })}
      ${field({ name: 'email', label: 'Email', type: 'email', required: true })}${field({ name: 'phone', label: 'Phone' })}
      ${field({ name: 'country', label: 'Country' })}${field({ name: 'value', label: 'Expected value per month', type: 'number' })}
    </div>
    <label>Services</label>
    <div class="row" style="gap:14px;flex-wrap:wrap;margin-bottom:12px">${services.map((s) =>
      `<label style="font-weight:400"><input type="checkbox" data-svc="${esc(s.slug)}"> ${esc(s.name)}</label>`).join('')}</div>
    ${field({ name: 'message', label: 'Notes', type: 'textarea' })}`,
    onSubmit: (form) => {
      const v = readForm(form);
      return api.post('/staff/leads', {
        topic: 'Sales question', name: v.name, company: v.company, email: v.email, phone: v.phone || null,
        country: v.country || null, message: v.message || null,
        value_cents: v.value == null ? null : Math.round(v.value * 100),
        services: $$('[data-svc]', form).filter((c) => c.checked).map((c) => c.dataset.svc),
      });
    },
  });
}

export async function renderLeads(mount, route = { query: {} }) {
  const view = getView();
  mount.innerHTML = loading();
  const [pipeline, services] = await Promise.all([api.get('/staff/leads/pipeline'), api.get('/staff/services')]);
  serviceNames = Object.fromEntries(services.map((s) => [s.slug, s.name]));
  const rows = Object.values(pipeline.stages).flat();
  const byId = new Map(rows.map((r) => [r.id, r]));
  mount.innerHTML = `
    ${summaryHtml(rows)}
    <div class="row" style="margin-bottom:14px;gap:8px;flex-wrap:wrap;align-items:center">
      ${viewSwitchHtml([['board', 'Board'], ['list', 'List']], view)}
      <span style="flex:1"></span><button class="primary" id="newLead">New Lead</button>
    </div>
    <div id="leadsBody">${view === 'board' ? boardHtml(rows) : listHtml(rows)}</div>
    <div id="leadPanel"></div>`;
  const redraw = () => renderLeads(mount);
  $$('[data-view]', mount).forEach((b) => b.addEventListener('click', () => { setView(b.dataset.view); redraw(); }));
  $('#newLead', mount).addEventListener('click', async () => { if (await newLeadDialog(services)) redraw(); });

  const openPanel = async (id, { focusLost = false } = {}) => {
    const lead = await api.get(`/staff/leads/${id}`);
    const host = $('#leadPanel', mount);
    host.innerHTML = panelHtml(lead);
    let changed = false;
    const close = () => { document.removeEventListener('keydown', onKey); host.innerHTML = ''; if (changed) redraw(); };
    const onKey = (e) => { if (e.key === 'Escape') close(); };
    document.addEventListener('keydown', onKey);
    $$('[data-close]', host).forEach((el) => el.addEventListener('click', close));
    const refreshHistory = async () => {
      const fresh = await api.get(`/staff/leads/${id}`);
      $('#wTimeline', host).innerHTML = fresh.events.map(eventLine).join('');
    };
    const paintDemo = () => {
      const box = $('#wDemo', host);
      box.innerHTML = demoRow(lead);
      $$('[data-demo]', box).forEach((b) => b.addEventListener('click', () => {
        const to = b.dataset.demo === lead.demo_status ? null : b.dataset.demo;
        guard(async () => {
          Object.assign(lead, await api.patch(`/staff/leads/${id}`, { demo_status: to }));
          changed = true;
          $('#wStatus', host).innerHTML = statusBar(lead.stage);
          bindBar();
          paintDemo();
          await refreshHistory();
        }, to ? `Demo ${DEMO_LABEL[to].toLowerCase()}` : 'Demo status cleared');
      }));
    };
    const bindBar = () => $$('.wiz-step', host).forEach((b) => b.addEventListener('click', () => {
      const to = b.dataset.stage;
      if (to === lead.stage) return;
      guard(async () => {
        Object.assign(lead, await api.patch(`/staff/leads/${id}`, { stage: to }));
        changed = true;
        $('#wStatus', host).innerHTML = statusBar(lead.stage);
        bindBar();
        paintDemo();
        $('#wLostWrap', host).classList.toggle('hidden', lead.stage !== 'lost');
        if (lead.stage === 'lost') $('#wLost', host).focus();
        await refreshHistory();
      }, `Moved to ${LABEL[to]}`);
    }));
    bindBar();
    paintDemo();
    $('#wSave', host).addEventListener('click', () => guard(async () => {
      const value = $('#wValue', host).value;
      const demoAt = $('#wDemoAt', host).value;
      const body = { value_cents: value === '' ? null : Math.round(Number(value) * 100) };
      if (demoAt) body.demo_at = new Date(demoAt).toISOString();
      if (lead.stage === 'lost') { body.stage = 'lost'; body.lost_reason = $('#wLost', host).value; }
      Object.assign(lead, await api.patch(`/staff/leads/${id}`, body));
      changed = true;
      await refreshHistory();
    }, 'Saved'));
    const note = $('#wNote', host);
    const addNote = () => guard(async () => {
      if (!note.value.trim()) return;
      await api.post(`/staff/leads/${id}/notes`, { note: note.value.trim() });
      note.value = '';
      await refreshHistory();
    });
    $('#wAddNote', host).addEventListener('click', addNote);
    note.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); addNote(); } });
    $('#wConvert', host)?.addEventListener('click', async () => {
      const picks = services.filter((s) => s.is_active);
      const ok = await dialog({
        title: `Create An Account For ${lead.company}`, submitLabel: 'Create Account',
        body: `<p>${esc(lead.name)} &lt;${esc(lead.email)}&gt; becomes the owner and gets an email to set their password. The lead moves to Won.</p>
          <label>Services to start (as trials)</label>
          ${picks.map((s) => `<label style="font-weight:400"><input type="checkbox" data-svc="${esc(s.slug)}" ${(lead.services || []).includes(s.slug) ? 'checked' : ''}> ${esc(s.name)}</label>`).join('')}`,
        onSubmit: (form) => api.post(`/staff/leads/${id}/convert`, {
          services: $$('[data-svc]', form).filter((c) => c.checked).map((c) => ({ service: c.dataset.svc })),
        }),
      });
      if (ok) { toast(`${ok.account.name} created.`, 'ok'); close(); window.location.hash = `#/accounts/${ok.account.id}`; }
    });
    if (focusLost) $('#wLost', host)?.focus();
  };

  const body = $('#leadsBody', mount);
  $$('tr[data-id]', body).forEach((tr) => tr.addEventListener('click', () => openPanel(tr.dataset.id)));
  $$('.board-card', body).forEach((card) => {
    card.addEventListener('click', () => openPanel(card.dataset.id));
    card.addEventListener('keydown', (e) => { if (e.key === 'Enter') openPanel(card.dataset.id); });
    card.addEventListener('dragstart', (e) => { e.dataTransfer.setData('text/plain', card.dataset.id); card.classList.add('dragging'); });
    card.addEventListener('dragend', () => card.classList.remove('dragging'));
  });
  $$('.board-col', body).forEach((col) => {
    col.addEventListener('dragover', (e) => { e.preventDefault(); col.classList.add('over'); });
    col.addEventListener('dragleave', (e) => { if (!col.contains(e.relatedTarget)) col.classList.remove('over'); });
    col.addEventListener('drop', (e) => {
      e.preventDefault();
      col.classList.remove('over');
      const id = e.dataTransfer.getData('text/plain');
      const lead = byId.get(id);
      const to = col.dataset.stage;
      if (!lead || lead.stage === to) return;
      setTimeout(() => guard(async () => {
        await api.patch(`/staff/leads/${id}`, { stage: to });
        await redraw();
        if (to === 'lost') openPanel(id, { focusLost: true });
      }), 0);
    });
  });
  if (route.query?.open) openPanel(route.query.open);
}
