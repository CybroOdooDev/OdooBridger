/* Overview: is it working, what needs attention, and the Sync now button. */

import { api, auth, runSync } from '../api.js';
import { pairingReviewed } from '../setup-state.js';
import {
  $, banner, busy, empty, esc, fmtAgo, fmtIn, guard, loading, pill, stat, fmtUtc, triggerLabel } from '../ui.js';

/* Getting started: the five things between signing up and attendance
 * arriving in Odoo, in order, each done from the guided setup (the one button above). Shown until
 * the first four are done — the fifth (matching badges) keeps coming back as
 * new people punch, and has its own banner for that. */
function setupChecklist({ health, devices, run, unmapped }) {
  const connected = (state) => state && state !== 'missing';
  const steps = [
    {
      done: connected(health.odoo),
      title: 'Connect Odoo',
      body: 'Where attendance is written. You need the server URL, database, login and an API key.',
    },
    {
      done: connected(health.source),
      title: 'Add A Biometric Connection',
      body: 'A BioTime server, or a device by its IP address.',
    },
    {
      // Every account has working defaults; "done" = someone looked, or it was already syncing.
      done: pairingReviewed() || Boolean(run),
      title: 'Pairing Rules',
      body: 'How punches become check-ins and check-outs. The defaults suit most sites.',
    },
    {
      done: Boolean(run),
      title: 'Run The First Sync',
      body: 'Pulls punches and writes attendance. After this it runs on its own schedule.',
    },
    {
      done: Boolean(run) && unmapped === 0,
      title: 'Match Unknown Badges',
      body: 'Badges no Odoo employee carries yet are held until they are matched.',
    },
  ];
  if (steps.slice(0, 4).every((s) => s.done)) return '';
  const next = steps.findIndex((s) => !s.done);
  const doneCount = steps.filter((s) => s.done).length;
  return `
    <div class="card checklist" style="margin-bottom:14px">
      <div class="card-head">
        <h2>BioBridge Setup</h2>
        <div class="row" style="gap:12px;flex-wrap:nowrap">
          ${doneCount ? `<div class="progress" aria-hidden="true"><span style="width:${(doneCount / steps.length) * 100}%"></span></div>` : ''}
          ${auth.canWrite ? `<div class="setup-start">
            <a class="btn primary-link sm" href="#/get-started">${doneCount ? 'Continue Setup' : 'Initial Setup'}</a>
            <div class="setup-pointer" role="note"><span class="setup-pointer-arrow" aria-hidden="true"></span>${
              doneCount ? 'Continue Here' : 'Start Here'}</div>
          </div>` : ''}
        </div>
      </div>
      <ol class="steps-list">
        ${steps.map((s, i) => `
          <li class="${s.done ? 'done' : i === next ? 'next' : ''}">
            <span class="step-mark">${s.done ? '✓' : i + 1}</span>
            <div class="step-text"><strong>${esc(s.title)}</strong><span>${esc(s.body)}</span></div>
          </li>`).join('')}
      </ol>
    </div>`;
}

/* A broadcast mark: a dot with radio waves, which ripple while the scheduler is live. */
const LIVE = '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" '
  + 'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
  + '<circle cx="12" cy="12" r="2" fill="currentColor"/>'
  + '<path class="wave w1" d="M8.5 15.5a5 5 0 0 1 0-7M15.5 8.5a5 5 0 0 1 0 7"/>'
  + '<path class="wave w2" d="M5.6 18.4a9 9 0 0 1 0-12.8M18.4 5.6a9 9 0 0 1 0 12.8"/></svg>';

/* The scheduler's state for the Scheduler Info card: an icon in its title bar,
 * coloured by state, saying what it is on hover. */
function scheduleIcon(schedule) {
  if (!schedule) return '';
  const every = schedule.effective_interval_minutes;
  const [tone, tip] = !schedule.running
    ? ['bad', 'Scheduler down — automatic syncs are paused']
    : !schedule.next_run_at
      ? ['mute', 'Scheduler idle — automatic sync is off']
      : ['ok', `Scheduler live — syncs every ${every} min${schedule.interval_widened ? ' (slowed to fit your plan)' : ''}`];
  return `<span class="sched-icon ${tone}" tabindex="0" role="img" aria-label="${esc(tip)}" data-tip="${esc(tip)}">${LIVE}</span>`;
}

/* ...and its rows: the last sync (with its result) and the next one. */
function scheduleRows(schedule, run) {
  const last = run
    ? `${pill(run.status)} <span data-tip="${esc(fmtUtc(run.started_at))} UTC">${esc(fmtAgo(run.started_at))}</span>`
    : 'Never';
  const lastRow = `<tr><td>Last sync</td><td style="text-align:right">${last}</td></tr>`;
  if (!schedule) return lastRow;
  let next;
  if (!schedule.running) {
    next = 'Paused until the scheduler is back';
  } else if (!schedule.next_run_at) {
    next = 'Automatic sync is off';
  } else {
    next = fmtIn(schedule.next_run_at);
  }
  return `
    ${lastRow}
    <tr><td>Next sync</td><td style="text-align:right">${esc(next)}</td></tr>`;
}

/* The one word for all biometric connections: the worst state among them. */
function worstOf(sources, fallback) {
  for (const st of ['failed', 'degraded', 'unverified', 'connected']) {
    if ((sources || []).some((x) => x.status === st)) return st;
  }
  return fallback;
}

/* "failed" alone says little once there are several biometric connections:
 * say how many are down, and which. Counted here from the connection list, so
 * it does not depend on the dashboard payload carrying it. */
function sourceDetail(sources) {
  if (!sources || sources.length < 2) return '';
  const failed = sources.filter((x) => x.status === 'failed');
  if (!failed.length) return '';   // only a failure is worth a line
  const names = failed.map((x) => esc(x.name || x.provider)).join(', ');
  return `<tr><td colspan="2" class="hint">${failed.length} of ${sources.length} biometric connections failed. Needs attention: ${names}.</td></tr>`;
}

export async function render(mount) {
  mount.innerHTML = loading();
  const [data, runs, devices, sources] = await Promise.all([
    api.get('/dashboard'),
    api.get('/sync/runs?limit=5').catch(() => []),
    api.get('/devices').catch(() => []),
    api.get('/sources').catch(() => []),
  ]);

  const health = data.connection_health || {};
  const needsSetup = health.odoo === 'missing' || health.source === 'missing';
  const run = data.last_run;
  const checklist = setupChecklist({
    health, devices: devices.length, run, unmapped: data.unmapped_employees,
  });

  // Failures and warnings (stopped account, renewals, failed punches, unmatched
  // badges, connections…) live in one place — the bell in the top bar — rather
  // than being repeated on this page.
  const today = new Date().toISOString().slice(0, 10);
  mount.innerHTML = `
    <div>
    <div class="rail-main">
    ${checklist}

    <div class="grid cols-4" style="margin-bottom:14px">
      ${stat({ label: 'Punches today', value: data.punches_today, href: `#/activity?date_from=${today}` })}
      ${stat({
        label: 'Pending', value: data.punches_pending,
        tone: data.punches_pending > 0 ? 'warn' : '',
        note: 'awaiting the next run', href: '#/activity?state=pending',
      })}
      ${stat({
        label: 'Unmatched Employee Badges', value: data.unmapped_employees,
        tone: data.unmapped_employees > 0 ? 'warn' : '', href: '#/settings/odoo?show=unmapped',
      })}
      ${stat({
        label: 'Errors', value: data.punches_error,
        tone: data.punches_error > 0 ? 'bad' : '', href: '#/activity?state=error',
      })}
    </div>

    <div class="grid cols-2">
      <div class="card">
        <h2>Connections</h2>
        <table>
          <tbody>
            <tr class="conn-row"><td><a class="conn-link" href="#/settings/odoo">Odoo</a></td><td style="text-align:right"><a class="conn-link" href="#/settings/odoo">${pill(health.odoo)}</a></td></tr>
            <tr class="conn-row"><td><a class="conn-link" href="#/settings/biometric">Biometric</a></td><td style="text-align:right"><a class="conn-link" href="#/settings/biometric">${pill(worstOf(sources, health.source))}</a></td></tr>
            ${sourceDetail(sources)}
          </tbody>
        </table>
        <div class="row" style="margin-top:14px">
          <a class="btn" href="#/settings/biometric">Manage Connections</a>
        </div>
      </div>

      <div class="card">
        <h2 class="h2-split">Scheduler Info ${scheduleIcon(data.schedule)}</h2>
        <table>
          <tbody>
            ${scheduleRows(data.schedule, run)}
            ${run ? `
              <tr><td>New punches</td><td class="num" style="text-align:right">${esc(run.punches_new)}</td></tr>
              <tr><td>Attendance created</td><td class="num" style="text-align:right">${esc(run.attendances_created)}</td></tr>
              <tr><td>Attendance closed</td><td class="num" style="text-align:right">${esc(run.attendances_closed)}</td></tr>` : ''}
          </tbody>
        </table>
        ${run?.error_message ? banner('Error', run.error_message, 'bad') : ''}
        ${run ? '' : empty('No Sync Has Run Yet', 'Connect both sides, then press Sync now.')}
        ${auth.canWrite ? `
          <div class="row" style="margin-top:14px">
            <button class="primary" id="syncNow" ${needsSetup ? 'disabled' : ''}>Sync Now</button>
            <a class="btn" href="#/activity">View History</a>
          </div>` : ''}
      </div>
    </div>

    ${runs.length ? `
      <div class="card" style="margin-top:14px">
        <h2>Recent Runs</h2>
        <div class="scroll">
          <table>
            <thead><tr><th>Started</th><th>Result</th><th class="num">New</th><th class="num">Created</th><th class="num">Closed</th><th>Trigger</th></tr></thead>
            <tbody>
              ${runs.map((r) => `
                <tr>
                  <td>${esc(fmtAgo(r.started_at))}</td>
                  <td>${pill(r.status)}</td>
                  <td class="num">${esc(r.punches_new)}</td>
                  <td class="num">${esc(r.attendances_created)}</td>
                  <td class="num">${esc(r.attendances_closed)}</td>
                  <td>${esc(triggerLabel(r.triggered_by))}</td>
                </tr>`).join('')}
            </tbody>
          </table>
        </div>
      </div>` : ''}
    </div>
    </div>
  `;

  // The checklist's "Sync now" is the top bar's, pressed from here.

  const button = $('#syncNow', mount);
  if (button) {
    button.addEventListener('click', () =>
      busy(button, () =>
        guard(async () => {
          // runSync waits for the finished run, so the result is immediate
          // rather than a "queued" message that explains nothing.
          const result = await runSync();
          const summary = `${result.status} — ${result.punches_new} new punch(es), `
            + `${result.attendances_created} created, ${result.attendances_closed} closed`;
          await render(mount);
          return summary;
        }).then((summary) => summary && window.dispatchEvent(
          new CustomEvent('bb:toast', { detail: summary })
        ))
      )
    );
  }
}
