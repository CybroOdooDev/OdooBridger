/* The core's HTTP client and session, for one door at a time.
 *
 * The portal and the staff console are separate pages with separate sessions
 * ("customer" and "staff" scope). Each page calls makeClient(scope) once. The
 * access token lives in memory; the refresh token in sessionStorage so a
 * reload survives without a long-lived credential in localStorage. */

const BASE = '/api/v1';

export class ApiError extends Error {
  constructor(message, status, payload) {
    super(message);
    this.status = status;
    this.payload = payload;
  }
}

function message(payload, fallback) {
  const detail = payload && payload.detail;
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) {
    return detail.map((e) => `${(e.loc || []).slice(1).join('.') || 'field'}: ${e.msg}`).join('; ');
  }
  return fallback;
}

export function makeClient(scope) {
  const key = `ob.refresh.${scope}`;
  const store = (v) => { try { if (v) sessionStorage.setItem(key, v); else sessionStorage.removeItem(key); } catch { /* private mode */ } };
  const load = () => { try { return sessionStorage.getItem(key); } catch { return null; } };

  const session = {
    accessToken: null,
    refreshToken: load(),
    get signedIn() { return Boolean(this.accessToken); },
    persist(tokens) {
      this.accessToken = tokens.access_token;
      if (tokens.refresh_token) this.refreshToken = tokens.refresh_token;
      store(this.refreshToken);
    },
    clear() {
      this.accessToken = null;
      this.refreshToken = null;
      store(null);
    },
  };

  let refreshing = null;
  async function refresh() {
    if (!session.refreshToken) return false;
    const response = await fetch(`${BASE}/auth/refresh`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ refresh_token: session.refreshToken }),
    });
    if (!response.ok) return false;
    const tokens = await response.json();
    if (tokens.scope !== scope) return false;
    session.persist(tokens);
    return true;
  }

  async function request(method, path, body, retry = true) {
    const headers = {};
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    if (session.accessToken) headers.Authorization = `Bearer ${session.accessToken}`;
    const response = await fetch(BASE + path, {
      method, headers, body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (response.status === 401 && session.accessToken) {
      if (retry) {
        refreshing = refreshing || refresh();
        const ok = await refreshing;
        refreshing = null;
        if (ok) return request(method, path, body, false);
      }
      session.clear();
      window.dispatchEvent(new CustomEvent('ob:signed-out'));
      throw new ApiError('Your session has ended. Please sign in again.', 401);
    }
    const text = await response.text();
    let payload = null;
    if (text) { try { payload = JSON.parse(text); } catch { payload = text; } }
    if (!response.ok) throw new ApiError(message(payload, `Request failed (HTTP ${response.status})`), response.status, payload);
    return payload;
  }

  const api = {
    get: (p) => request('GET', p),
    post: (p, b = {}) => request('POST', p, b),
    patch: (p, b) => request('PATCH', p, b),
    del: (p) => request('DELETE', p),
  };

  /** Rehydrate after a reload: true when a session was restored. */
  async function restore() {
    if (session.accessToken) return true;
    if (!session.refreshToken) return false;
    try {
      return await refresh();
    } catch {
      return false;
    }
  }

  async function signOut() {
    if (session.refreshToken) {
      try {
        await fetch(`${BASE}/auth/logout`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ refresh_token: session.refreshToken }),
        });
      } catch { /* signing out locally is what matters */ }
    }
    session.clear();
  }

  return { api, session, restore, signOut };
}

/** Money: cents -> "$29" or "$29.50". */
export function money(cents, currency = 'usd') {
  if (cents == null) return '—';
  const value = cents / 100;
  const symbol = { usd: '$', eur: '€', gbp: '£', inr: '₹', aed: 'AED ' }[String(currency).toLowerCase()] ?? '';
  return `${symbol}${Number.isInteger(value) ? value : value.toFixed(2)}`;
}

export function fmtDate(iso) {
  if (!iso) return '—';
  return new Date(iso).toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' });
}
