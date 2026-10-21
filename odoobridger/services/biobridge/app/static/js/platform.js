/* OdooBridger platform mode.
 *
 * When this BioBridge is OdooBridger's biometric service, people never sign
 * in here with a password: the dashboard sends them to the OdooBridger portal,
 * which sends them back with a one-time code (#/sso?code=…), and the server
 * trades that code for their identity. Billing and the account itself are
 * managed in the portal too. In standalone mode none of this runs.
 */

import { auth, loadSession } from './api.js';

const STATE_KEY = 'bb.sso.state';

export const platform = {
  info: null,
  get on() {
    return Boolean(this.info?.platform_mode);
  },
  async load() {
    if (this.info) return this.info;
    try {
      const response = await fetch('/api/v1/auth/mode');
      this.info = response.ok ? await response.json() : { platform_mode: false };
    } catch {
      this.info = { platform_mode: false };
    }
    return this.info;
  },
};

function waiting(root, title, text, link) {
  root.classList.remove('hidden');
  root.innerHTML = `
    <div class="auth-wrap"><div class="auth-card">
      <h1>${title}</h1><p class="sub">${text}</p>
      ${link ? `<p class="auth-alt"><a href="${link.href}">${link.label}</a></p>` : ''}
    </div></div>`;
}

/** Off to the OdooBridger portal, which sends the person back signed in. */
export function startPlatformSignIn() {
  const state = Math.random().toString(36).slice(2) + Date.now().toString(36);
  try { sessionStorage.setItem(STATE_KEY, state); } catch { /* checked loosely below */ }
  const back = `${window.location.origin}${window.location.pathname}#/sso`;
  const url = `${platform.info.authorize_url}?redirect_uri=${encodeURIComponent(back)}&state=${encodeURIComponent(state)}`;
  waiting(document.getElementById('auth-root'), 'Signing You In',
    'Taking you to OdooBridger to sign in…', { href: url, label: 'Continue' });
  window.location.replace(url);
}

/** Back from the portal with ?code=…: open a BioBridge session. */
export async function finishPlatformSignIn(route) {
  const root = document.getElementById('auth-root');
  let expected = null;
  try { expected = sessionStorage.getItem(STATE_KEY); sessionStorage.removeItem(STATE_KEY); } catch { /* none */ }
  if (expected && route.query.state !== expected) {
    waiting(root, 'Sign-In Expired', 'That sign-in link was not started from this browser. Try again.',
      { href: platform.info.portal_url, label: 'Back to OdooBridger' });
    return false;
  }
  waiting(root, 'Signing You In', 'One moment…');
  const response = await fetch('/api/v1/auth/platform/sso', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ code: route.query.code || '' }),
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    waiting(root, 'Could Not Sign You In', payload.detail || 'Please try again from OdooBridger.',
      { href: platform.info.portal_url, label: 'Back to OdooBridger' });
    return false;
  }
  auth.persist(payload);
  await loadSession();
  root.classList.add('hidden');
  return true;
}
