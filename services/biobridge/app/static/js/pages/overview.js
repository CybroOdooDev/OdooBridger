/* Overview: is it working, what needs attention, and the Sync now button. */

import { api, auth } from '../api.js';
import { platform } from '../platform.js';
import { pairingReviewed } from '../setup-state.js';
import {
  banner, empty, esc, fmtAgo, fmtIn, loading, pill, stat, fmtUtc } from '../ui.js';

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

/* The one word for all biometric connections: the worst state among them. */
function worstOf(sources, fallback) {
  for (const st of ['failed', 'degraded', 'unverified', 'connected']) {
    if ((sources || []).some((x) => x.status === st)) return st;
  }
  return fallback;
}

/* ---- Sync & connections ------------------------------------------------------ */
const tonePill = (tone, label) => `<span class="pill ${tone}">${esc(label)}</span>`;
const names = (list) => list.map((x) => esc(x.name || x.provider)).join(', ');

/* The line under the title is the overall verdict, so no separate status banner is needed:
 * one coloured dot and a few words ("Everything is working" / what is wrong), then when the
 * next sync is. It replaces the old "Next sync in…" line, so it costs no vertical space. */
function overallStatus(data, conns, sources) {
  const issues = [];                       // [tone, text], worst first once sorted
  const odoo = conns.odoo;
  if (!odoo) issues.push(['warn', 'Odoo is not connected']);
  else if (odoo.status === 'failed') issues.push(['bad', 'Odoo is disconnected']);
  else if (odoo.status === 'degraded') issues.push(['warn', 'Odoo is unstable']);
  const live = (sources || []).filter((x) => x.is_active !== false);
  if (!live.length) issues.push(['warn', 'No biometric connection']);
  else if (worstOf(live, 'connected') === 'failed') issues.push(['bad', 'A biometric connection is down']);
  const c = conns.terminal_counts || {};
  const many = (n, one, more) => `${n} ${n === 1 ? one : more}`;
  if (c.offline) issues.push(['warn', many(c.offline, 'terminal is offline', 'terminals are offline')]);
  if (c.missing) issues.push(['warn', many(c.missing, 'terminal is missing', 'terminals are missing')]);
  if (c.waiting) issues.push(['warn', many(c.waiting, 'terminal has not called in', 'terminals have not called in')]);
  if (data.last_run?.status === 'failed' && !issues.some(([t]) => t === 'bad')) issues.push(['bad', 'The last sync failed']);
  if (data.punches_error > 0) issues.push(['bad', many(data.punches_error, 'punch has an error', 'punches have errors')]);
  const schedule = data.schedule;
  if (schedule && !schedule.running) issues.push(['bad', 'Automatic sync is paused']);
  issues.sort((a, b) => (a[0] === 'bad' ? 0 : 1) - (b[0] === 'bad' ? 0 : 1));
  if (!issues.length) return { tone: 'ok', text: 'Everything is working' };
  const shown = issues.slice(0, 2).map(([, t]) => t).join(' · ');
  return { tone: issues[0][0], text: issues.length > 2 ? `${shown} · +${issues.length - 2} more` : shown };
}

function syncSubtitle(data, conns, sources) {
  const verdict = overallStatus(data, conns, sources);
  const schedule = data.schedule;
  let next = '';
  if (schedule?.running && schedule.next_run_at) {
    const every = schedule.effective_interval_minutes;
    next = `Next sync ${fmtIn(schedule.next_run_at)} · every ${every} min${schedule.interval_widened ? ' (slowed to fit your plan)' : ''}`;
  } else if (schedule?.running) next = 'Automatic sync is off';
  return { tone: verdict.tone, verdict: verdict.text, next };
}

/* A message from Odoo may end without a stop ("403 Forbidden"); give it one before more text follows. */
const sentence = (s) => (/[.!?]$/.test(String(s).trim()) ? String(s).trim() : `${String(s).trim()}.`);

/* Odoo: where attendance goes. A problem says why, and the one place that fixes it. */
function odooRow(odoo, pending, portalOdoo) {
  const head = (pillHtml) => `<div class="sc-top"><a class="sc-name" href="#/settings/odoo">Odoo</a>${pillHtml}</div>`;
  if (!odoo) {
    return `<div class="sc-row">${head(tonePill('mute', 'Not connected'))}
      <div class="sc-meta">Attendance has nowhere to go yet.</div>
      <div class="sc-note warn"><span>Connect your Odoo to start writing attendance.</span>
        <a class="btn sm warn-link" href="#/settings/odoo">Connect Odoo</a></div></div>`;
  }
  const checked = odoo.last_checked_at ? ` · checked ${esc(fmtAgo(odoo.last_checked_at))}` : '';
  const meta = `<div class="sc-meta"><span class="mono">${esc(odoo.host)}</span>${checked}</div>`;
  const fix = odoo.managed
    ? { href: portalOdoo, label: 'Open OdooBridger' }
    : { href: '#/settings/odoo', label: odoo.status === 'failed' ? 'Fix Connection' : 'Odoo Connection' };
  const safe = pending > 0 ? ` ${pending} punch${pending === 1 ? ' is' : 'es are'} waiting and will be sent once it works.` : ' Punches are kept and sent once it works.';
  if (odoo.status === 'connected') return `<div class="sc-row">${head(tonePill('ok', 'Connected'))}${meta}</div>`;
  if (odoo.status === 'failed' || odoo.status === 'degraded') {
    const failed = odoo.status === 'failed';
    return `<div class="sc-row">${head(tonePill(failed ? 'bad' : 'warn', failed ? 'Disconnected' : 'Unstable'))}${meta}
      <div class="sc-note ${failed ? 'bad' : 'warn'}"><span>${esc(sentence(odoo.message || (failed ? 'Odoo did not accept the last request.' : 'Odoo is answering, but not everything works.')))}${safe}</span>
        <a class="btn sm ${failed ? 'bad-link' : 'warn-link'}" href="${esc(fix.href)}">${esc(fix.label)}</a></div></div>`;
  }
  return `<div class="sc-row">${head(tonePill('warn', 'Not checked yet'))}${meta}</div>`;
}

/* Connectivity and activity are different things: a terminal is judged only by a heartbeat
 * (online / offline), and "last punch" is shown beside it as activity, so a quiet door at night is
 * not read as a dead one, and a dead one is not hidden by an old punch. A terminal whose
 * platform reports no heartbeat gets a grey dot: it is listed, not known to be up. */
const NO_HEARTBEAT = 'This platform does not say whether the terminal is online, so only its last punch is shown.';
const punchText = (t) => (t.last_punch_at ? `last punch ${fmtAgo(t.last_punch_at)}` : 'no punches yet');
const TERMINAL_TEXT = {
  online: (t) => ['ok', `Online · ${punchText(t)}`, ''],
  offline: (t) => ['bad', `Offline · last contact ${fmtAgo(t.last_contact_at)}`, ''],
  waiting: () => ['warn', 'Has not called in yet', ''],
  missing: () => ['warn', 'No longer listed by its platform', ''],
  reporting: (t) => ['', punchText(t), NO_HEARTBEAT],
  unreachable: () => ['bad', 'Not answering', ''],
  unchecked: () => ['warn', 'Not checked yet', ''],
};

/* Terminals: are the devices alive, which is what customers care about most.
 * A push terminal's heartbeat is real proof, so it is "online"/"offline". One read
 * through a platform is only known to be listed or not, so it is "reporting", and a
 * quiet night is never called a fault. */
function terminalsRow(conns, sources) {
  const head = (pillHtml) => `<div class="sc-top"><a class="sc-name" href="#/settings/biometric">Terminals</a>${pillHtml}</div>`;
  const live = (sources || []).filter((x) => x.is_active !== false);
  if (!live.length) {
    return `<div class="sc-row">${head(tonePill('mute', 'Not connected'))}
      <div class="sc-meta">No biometric connection yet.</div>
      <div class="sc-note warn"><span>Add a BioTime server or a device to start collecting punches.</span>
        <a class="btn sm warn-link" href="#/settings/biometric">Add a connection</a></div></div>`;
  }
  const c = conns.terminal_counts || {};
  const total = c.total || 0;
  // "online" only where a heartbeat says so; terminals that are merely listed are counted as listed.
  const judged = (c.online || 0) + (c.offline || 0);
  const noHeartbeat = (c.reporting || 0) > 0 && judged === 0;
  const up = noHeartbeat ? c.reporting : c.online || 0;
  const word = noHeartbeat ? 'listed' : 'online';
  const worst = worstOf(live, 'connected');
  const brokenSources = live.filter((x) => x.status === 'failed' || x.status === 'degraded' || x.status === 'unverified');
  const down = (c.offline || 0) + (c.missing || 0) + (c.waiting || 0) + (c.unreachable || 0) + (c.unchecked || 0);

  let pillHtml;
  if (worst === 'failed') pillHtml = tonePill('bad', 'Not answering');
  else if (total === 0) pillHtml = tonePill(worst === 'connected' ? 'ok' : 'warn', worst === 'connected' ? 'Connected' : 'Check connection');
  else {
    // Mixed: some terminals prove they are up, others are only listed by a platform.
    const mixed = !noHeartbeat && (c.reporting || 0) > 0;
    const label = mixed ? `${c.online || 0} online · ${c.reporting} listed` : `${up} of ${total} ${word}`;
    pillHtml = tonePill(down || worst !== 'connected' ? 'warn' : noHeartbeat ? 'mute' : 'ok', label);
  }

  const last = conns.last_punch_at ? `Last punch ${esc(fmtAgo(conns.last_punch_at))}` : 'No punches yet';
  const meta = `<div class="sc-meta">${total === 0 && worst === 'connected' ? 'No terminals imported yet' : last}</div>`;

  const rows = (conns.terminals || []).map((t) => {
    const [tone, text, tip] = (TERMINAL_TEXT[t.state] || TERMINAL_TEXT.reporting)(t);
    const flag = (t.state === 'offline' || t.state === 'unreachable') ? 'bad' : ['waiting', 'missing', 'unchecked'].includes(t.state) ? 'warn' : '';
    const target = t.registered === false ? '#/settings/biometric' : `#/settings/biometric?device=${encodeURIComponent(t.id)}`;
    return `<a class="sc-term ${flag}" href="${target}"${tip ? ` data-tip="${esc(tip)}"` : ''}><span class="n"><i class="sc-dot ${tone}"></i>${esc(t.name)}</span><span class="t">${esc(text)}</span></a>`;
  }).join('');
  const hidden = total - (conns.terminals || []).length;
  const more = hidden > 0 ? `<div class="sc-more"><a href="#/settings/biometric">${hidden} more terminal${hidden === 1 ? '' : 's'} · View all</a></div>` : '';

  const notes = [];
  if (brokenSources.length) {
    const failed = worst === 'failed';
    const why = brokenSources.find((x) => x.status_message)?.status_message;
    notes.push(`<div class="sc-note ${failed ? 'bad' : 'warn'}"><span>${esc(brokenSources.length > 1 ? `${names(brokenSources)}: ` : '')}${esc(why || 'The last attempt to read punches did not work.')} New punches from it are not arriving.</span>
      <a class="btn sm ${failed ? 'bad-link' : 'warn-link'}" href="#/settings/biometric">Check connection</a></div>`);
  }
  if (c.offline) {
    notes.push(`<div class="sc-note warn"><span>${c.offline === 1 ? 'A terminal is' : `${c.offline} terminals are`} offline. Punches made meanwhile stay on the terminal and arrive when it reconnects.</span>
      <a class="btn sm warn-link" href="#/settings/biometric">Manage Terminals</a></div>`);
  }
  if (c.missing) {
    notes.push(`<div class="sc-note warn"><span>${c.missing === 1 ? 'A terminal is' : `${c.missing} terminals are`} no longer listed by the platform. It may have been removed or renamed there.</span>
      <a class="btn sm warn-link" href="#/settings/biometric">Manage Terminals</a></div>`);
  }
  if (c.waiting) {
    notes.push(`<div class="sc-note warn"><span>${c.waiting === 1 ? 'A terminal has' : `${c.waiting} terminals have`} not contacted BioBridge yet. Check the server address set on the terminal.</span>
      <a class="btn sm warn-link" href="#/settings/biometric">Manage Terminals</a></div>`);
  }
  return `<div class="sc-row">${head(pillHtml)}${meta}${rows ? `<div class="sc-terms">${rows}</div>` : ''}${more}${notes.join('')}</div>`;
}

/* The last run, in the right-hand column, and a dot for each of the last few runs (oldest first),
 * so a streak of failures shows without a table. */
function lastSyncPanel(run, runs) {
  if (!run) return `<h3>Last sync</h3>${empty('No Sync Has Run Yet', 'Connect both sides, then use Sync Now at the top.')}`;
  const dots = [...(runs || [])].reverse().map((r) => {
    const tone = r.status === 'success' ? 'ok' : r.status === 'failed' ? 'bad' : 'warn';
    return `<i class="sc-dot ${tone}" data-tip="${esc(`${fmtAgo(r.started_at)}: ${r.status}`)}"></i>`;
  }).join('');
  return `
    <h3>Last sync · <span data-tip="${esc(fmtUtc(run.started_at))} UTC">${esc(fmtAgo(run.started_at))}</span> ${pill(run.status)}</h3>
    <div class="sc-kv"><span>New punches</span><span>${esc(run.punches_new)}</span></div>
    <div class="sc-kv"><span>Attendance created</span><span>${esc(run.attendances_created)}</span></div>
    <div class="sc-kv"><span>Attendance closed</span><span>${esc(run.attendances_closed)}</span></div>
    ${dots ? `<div class="sc-kv"><span>Recent runs</span><span class="sc-runs">${dots}</span></div>` : ''}
    ${run.error_message ? banner('Error', run.error_message, 'bad') : ''}`;
}

/* One card for "is data flowing?": the Odoo in use, each terminal, and the last sync. */
function syncConnectionsCard({ data, run, runs, sources, portalOdoo, needsSetup }) {
  const conns = data.connections || {};
  const sub = syncSubtitle(data, conns, sources);
  return `
    <div class="card" id="syncCard">
      <div class="sc-head">
        <div>
          <h2 class="h2-split">Sync &amp; Connections ${scheduleIcon(data.schedule)}</h2>
          <div class="sc-sub"><i class="sc-dot ${sub.tone}"></i><strong class="${sub.tone}">${esc(sub.verdict)}</strong>${sub.next ? `<span class="sc-next">${esc(sub.next)}</span>` : ''}</div>
        </div>
        <div class="sc-actions">
          <a class="btn sm" href="#/settings/biometric">Connection Settings</a>
          ${auth.canWrite ? `<button class="primary sm" data-sync-now ${needsSetup ? 'disabled title="Connect Odoo and a biometric source first"' : ''}>
            <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M20 12a8 8 0 1 1-2.3-5.6M20 4v5h-5"/></svg>
            Sync Now</button>` : ''}
        </div>
      </div>
      <div class="sc-body">
        <div>
          ${odooRow(conns.odoo, data.punches_pending, portalOdoo)}
          ${terminalsRow(conns, sources)}
        </div>
        <div class="sc-side">
          ${lastSyncPanel(run, runs)}
          <div class="sc-links"><a href="#/activity">View history</a></div>
        </div>
      </div>
    </div>`;
}

/* ---- Latest punches and punches this week ------------------------------------------ */
const plural = (n, one, more) => `${n} ${n === 1 ? one : more}`;

/* Where one punch stands, in the customer's words: dot colour, pill tone, pill text. */
const PUNCH_STATE = {
  synced: ['ok', 'ok', 'In Odoo'],
  pending: ['', 'info', 'Waiting for next sync'],
  unmapped: ['warn', 'warn', 'Badge not linked'],
  error: ['bad', 'bad', 'Failed'],
  held: ['warn', 'warn', 'On hold'],
  skipped: ['', 'mute', 'Ignored'],
};

/* The site's own clock ("9:41 am"), read from the naive local time so no browser timezone shifts it;
 * an earlier day gets its date as well. */
function clockText(local, todayIso) {
  const m = /^(\d{4}-\d{2}-\d{2})[T ](\d{2}):(\d{2})/.exec(String(local || ''));
  if (!m) return '';
  const h = Number(m[2]);
  const time = `${h % 12 || 12}:${m[3]} ${h < 12 ? 'am' : 'pm'}`;
  if (m[1] === todayIso) return time;
  const day = new Date(`${m[1]}T12:00:00`).toLocaleDateString(undefined, { day: 'numeric', month: 'short' });
  return `${day}, ${time}`;
}

function latestPunchesCard(data) {
  const latest = data.latest_punches || [];
  const today = (data.punches_week || []).at(-1)?.date;
  const rows = latest.map((p) => {
    const [dot, tone, label] = PUNCH_STATE[p.state] || ['', 'mute', p.state];
    const where = [p.terminal, clockText(p.time_local || p.time, today)].filter(Boolean).join(' · ');
    const name = /^Badge \d/.test(p.name) ? p.name.replace('Badge ', 'Badge #') : p.name;
    return `
      <li class="lp-row">
        <i class="sc-dot ${dot}"></i>
        <div class="lp-main"><strong>${esc(name)}</strong><span>${esc(where)}</span></div>
        <span class="pill ${tone}">${esc(label)}</span>
      </li>`;
  }).join('');
  return `
    <div class="card" id="latestCard">
      <div class="sc-head"><h2>Latest Punches</h2><span class="hint">Updates after each sync</span></div>
      ${rows ? `<ul class="lp-list">${rows}</ul>` : empty('No Punches Yet', 'They appear here once a terminal records one.')}
    </div>`;
}

function punchesWeekCard(data) {
  const days = data.punches_week || [];
  const max = Math.max(1, ...days.map((d) => d.count));
  const weekday = (iso) => new Date(`${iso}T12:00:00`).toLocaleDateString(undefined, { weekday: 'short' });
  const bars = days.map((d, i) => `
    <div class="pw-col${i === days.length - 1 ? ' today' : ''}" data-tip="${esc(`${d.date}: ${plural(d.count, 'punch', 'punches')}`)}">
      <span class="pw-n">${esc(d.count)}</span>
      <i style="height:${Math.max(3, Math.round((d.count / max) * 130))}px"></i>
      <span class="pw-d">${esc(i === days.length - 1 ? 'Today' : weekday(d.date))}</span>
    </div>`).join('');
  return `
    <div class="card" id="weekCard">
      <div class="sc-head"><h2>Punches This Week</h2><span class="hint">Last 7 days</span></div>
      <div class="pw-bars" role="img" aria-label="Punches per day for the last seven days">${bars}</div>
    </div>`;
}

/* ---- Plan ---------------------------------------------------------------------------- */
/* Which plan the account is on, what state it is in, and how much of it is used. Works the same in
 * platform mode (the package and entitlements the core sent) and standalone (the tenant's own plan). */
function planTile(tenant, data, deviceCount) {
  if (!tenant) return '';
  const st = tenant.platform_state || {};
  const ent = st.entitlements || {};
  const name = st.package?.name || tenant.plan_name || '';
  const status = tenant.status;
  if (!name && status !== 'trialing') return '';
  const pick = (a, b) => (a !== undefined && a !== null ? a : b ?? null);
  const maxDevices = pick(ent.max_devices, tenant.plan_max_devices);
  const maxEmployees = pick(ent.max_employees, tenant.plan_max_employees);
  const floor = pick(ent.min_sync_interval_minutes, tenant.plan_min_sync_interval_minutes);

  const dateOf = (iso) => (iso ? new Date(iso).toLocaleDateString(undefined, { day: 'numeric', month: 'short' }) : '');
  const endsAt = st.trial_ends_at || (status === 'trialing' ? tenant.subscription_renews_at : null);
  const renewsAt = st.renews_at || tenant.subscription_renews_at;
  const daysLeft = endsAt ? Math.max(0, Math.ceil((new Date(endsAt) - Date.now()) / 86400000)) : null;

  let title = name || 'Free';
  let when = '';
  let tone = '';
  if (status === 'trialing') {
    title = `${name || 'Free'} trial`;
    when = daysLeft === null ? 'Free trial' : daysLeft === 0 ? 'Trial ends today' : `${plural(daysLeft, 'day', 'days')} left`;
    tone = daysLeft !== null && daysLeft <= 3 ? 'warn' : '';
  } else if (status === 'active') when = renewsAt ? `Renews ${dateOf(renewsAt)}` : 'Active';
  else if (status === 'past_due') { when = 'Payment needed: syncing is paused until it is paid'; tone = 'bad'; }
  else if (status === 'suspended') { when = 'Suspended: contact support'; tone = 'bad'; }
  else if (status === 'cancelled') { when = 'Cancelled'; tone = 'bad'; }
  else when = status;

  const every = data.schedule?.effective_interval_minutes || tenant.sync_interval_minutes;
  const speed = every
    ? `syncs every ${every} min${floor && every > floor ? ` (${name || 'your plan'} allows every ${floor})` : ''}`
    : '';
  const sub = [when, status === 'past_due' || status === 'suspended' || status === 'cancelled' ? '' : speed].filter(Boolean).join(' · ');

  const meter = (label, used, cap) => {
    if (cap === null || cap === undefined || cap === 0) {
      return `<div class="pl-meter"><span class="pl-k">${esc(label)}</span><span class="pl-v">${esc(used)} · no limit</span></div>`;
    }
    const pct = Math.min(100, Math.round((used / cap) * 100));
    const level = used >= cap ? 'bad' : pct >= 80 ? 'warn' : '';
    return `<div class="pl-meter"><span class="pl-k">${esc(label)}</span>
      <div class="pl-track" role="progressbar" aria-label="${esc(label)}" aria-valuemin="0" aria-valuemax="${esc(cap)}" aria-valuenow="${esc(used)}"><i class="${level}" style="width:${pct}%"></i></div>
      <span class="pl-v ${level}">${esc(used)} of ${esc(cap)}</span></div>`;
  };

  const changeHref = platform.on && platform.info?.account_url ? platform.info.account_url : '#/settings/billing/choose';
  return `
    <div class="card plan-tile" id="planTile">
      <div class="pl-head">
        <strong>${esc(title)}</strong>
        ${sub ? `<span class="pl-sub ${tone}">${esc(sub)}</span>` : ''}
      </div>
      <div class="pl-meters">
        ${meter('Devices', deviceCount, maxDevices)}
        ${meter('Employees', data.people_total || 0, maxEmployees)}
      </div>
      ${auth.canWrite ? `<a class="btn" href="${esc(changeHref)}">Change Plan</a>` : ''}
    </div>`;
}

/* ---- Failed-punch banner and the "needs attention" list ------------------------------- */
/* Punches that failed to reach Odoo are the one thing worth a banner of their own. */
function errorBanner(data) {
  const n = data.punches_error || 0;
  if (!n) return '';
  return `<div style="margin-bottom:14px">${banner(
    `${plural(n, 'punch', 'punches')} failed to reach Odoo`,
    'BioBridge retries on its own; any that stop retrying need a manual Retry. ',
    'bad', { href: '#/activity?state=error', label: 'Review failed punches' })}</div>`;
}

/* The same list as the bell, each matter with the page that fixes it. The unmatched-badge matter names
 * the badges, so it is a to-do and not just a count. */
function attentionCard(alerts, unmatchedRows) {
  if (!alerts.length) {
    return `<div class="card" id="attentionCard"><div class="sc-head"><h2>Needs Attention</h2></div>
      <div class="na-clear"><span class="na-mark" aria-hidden="true">✓</span>Nothing needs fixing right now.</div></div>`;
  }
  const rows = alerts.map((a) => {
    const badges = a.key === 'unmapped' && unmatchedRows.length
      ? `<span class="na-badges">${unmatchedRows.map((m) => esc(`#${m.emp_code}${m.source_name ? ` · ${m.source_name}` : ''}`)).join(' · ')}</span>`
      : '';
    return `
      <li class="na-item ${esc(a.severity)}">
        <div class="na-text"><strong>${esc(a.title)}</strong><span>${esc(a.detail)}</span>${badges}</div>
        ${a.href ? `<a class="btn sm ${a.severity === 'bad' ? 'bad-link' : 'warn-link'}" href="${esc(a.href)}">${esc(a.action || 'Open')}</a>` : ''}
      </li>`;
  }).join('');
  return `<div class="card" id="attentionCard"><div class="sc-head"><h2>Needs Attention</h2>
    <span class="pill ${alerts.some((a) => a.severity === 'bad') ? 'bad' : 'warn'}">${alerts.length}</span></div>
    <ul class="na-list">${rows}</ul></div>`;
}

/* When the next automatic sync is, or why none is coming. */
function nextSyncText(schedule) {
  if (!schedule) return '';
  if (!schedule.running) return 'Automatic sync is paused';
  if (!schedule.next_run_at) return 'Automatic sync is off';
  return `Next sync ${fmtIn(schedule.next_run_at)}`;
}

/* A connection that is down is re-checked by the server right after sign-in (and
 * every few minutes), so the page quietly re-reads itself a few times while any
 * connection is in trouble: the status turns Connected when it recovers, with no
 * button and no toast. Stops when everything is fine, the page is left, or the
 * tab is hidden. */
const SETTLE_AFTER = [7000, 20000, 60000];
const UNHEALTHY = ['failed', 'degraded', 'unverified'];
let settleTimer = null;

function settle(mount, route, attempt, unhealthy) {
  clearTimeout(settleTimer);
  if (!unhealthy || attempt >= SETTLE_AFTER.length) return;
  settleTimer = setTimeout(async () => {
    // #content is reused by every page, so only repaint while this one is showing.
    if (!mount.isConnected || document.hidden || !['', '#', '#/'].includes(window.location.hash)) return;
    try { await render(mount, route, { silent: true, attempt: attempt + 1 }); } catch { /* the next visit retries */ }
  }, SETTLE_AFTER[attempt] - (attempt ? SETTLE_AFTER[attempt - 1] : 0));
}

export async function render(mount, route, opts = {}) {
  if (!opts.silent) mount.innerHTML = loading();
  const [data, runs, devices, sources, tenant, alertsOut] = await Promise.all([
    api.get('/dashboard'),
    api.get('/sync/runs?limit=6').catch(() => []),
    api.get('/devices').catch(() => []),
    api.get('/sources').catch(() => []),
    api.get('/tenant').catch(() => null),
    api.get('/alerts').catch(() => ({ alerts: [] })),
  ]);
  const alerts = alertsOut.alerts || [];
  // Name the unmatched badges in the list rather than only counting them (best effort).
  const unmatchedRows = alerts.some((a) => a.key === 'unmapped')
    ? await api.get('/mappings?status=unmapped&limit=3').catch(() => []) : [];

  await platform.load();
  const portalOdoo = `${platform.info?.portal_url || '/'}#/odoo?from=biobridge-open`;
  const health = data.connection_health || {};
  const run = data.last_run;
  const needsSetup = health.odoo === 'missing' || health.source === 'missing';
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

    ${errorBanner(data)}
    <div class="grid cols-3" style="margin-bottom:14px">
      ${stat({
        label: 'Punches today', value: data.punches_today,
        note: [nextSyncText(data.schedule), data.punches_pending > 0 ? `${plural(data.punches_pending, 'punch', 'punches')} waiting for Odoo` : ''].filter(Boolean),
        href: `#/activity?date_from=${today}`,
      })}
      ${stat({
        label: 'Employee Attendance', value: data.people_today,
        note: data.people_total ? `of ${plural(data.people_total, 'employee', 'employees')} punched today` : 'No employees matched yet',
        href: '#/employees',
      })}
      ${stat({
        id: 'kpiAttention', label: 'Needs Attention', value: alerts.length,
        tone: alerts.some((a) => a.severity === 'bad') ? 'bad' : alerts.length ? 'warn' : '',
        note: alerts.length ? `${plural(alerts.length, 'matter', 'matters')} to look at` : 'Nothing needs fixing',
        href: '#/',
        tip: 'The same list as the bell. Opens the list below.',
      })}
    </div>

    ${syncConnectionsCard({ data, run, runs, sources, portalOdoo, needsSetup })}

    <div class="lp-pair">
      ${latestPunchesCard(data)}
      ${punchesWeekCard(data)}
    </div>

    ${attentionCard(alerts, unmatchedRows)}

    ${planTile(tenant, data, devices.filter((d) => d.is_enabled !== false).length)}
    </div>
    </div>
  `;

  // The Needs Attention tile scrolls to its list instead of leaving the page.
  mount.querySelector('#kpiAttention')?.addEventListener('click', (event) => {
    event.preventDefault();
    document.getElementById('attentionCard')?.scrollIntoView({ behavior: 'smooth', block: 'center' });
  });

  settle(mount, route, opts.attempt || 0, [
    health.odoo, worstOf(sources, health.source),
  ].some((state) => UNHEALTHY.includes(state)));
}
