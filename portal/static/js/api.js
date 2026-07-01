/**
 * api.js — общие утилиты: fetch-обёртка, тосты, auth guard.
 */

// ── Toast ─────────────────────────────────────────────────────────────────────
const toastContainer = (() => {
  const el = document.createElement('div');
  el.id = 'toast-container';
  document.body.appendChild(el);
  return el;
})();

export function toast(msg, type = 'inf', duration = 3500) {
  const el = document.createElement('div');
  el.className = `toast toast-${type}`;
  el.textContent = msg;
  toastContainer.appendChild(el);
  setTimeout(() => el.remove(), duration);
}

// ── API fetch ─────────────────────────────────────────────────────────────────
export async function api(method, path, body = null) {
  const opts = {
    method,
    headers: { 'Content-Type': 'application/json' },
    credentials: 'same-origin',
  };
  if (body !== null) opts.body = JSON.stringify(body);

  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));

  if (res.status === 401) {
    // Сессия истекла — редирект на логин
    window.location.href = '/';
    throw new Error('Unauthorized');
  }

  if (!res.ok) {
    throw new Error(data.detail || `HTTP ${res.status}`);
  }

  return data;
}

// ── Auth guard (вызывать в начале защищённых страниц) ─────────────────────────
export async function guardAuth() {
  try {
    const data = await api('GET', '/api/me');
    return data;
  } catch {
    window.location.href = '/';
    throw new Error('redirect');
  }
}

// ── Logout ────────────────────────────────────────────────────────────────────
export async function logout() {
  await api('POST', '/api/logout').catch(() => {});
  window.location.href = '/';
}
