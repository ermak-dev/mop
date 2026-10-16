// Приватный prospective-контракт #600: cookie, JSON, никаких полей личности.
export class HybridHttpError extends Error {
  constructor(public status: number, message: string) { super(message); }
}
export async function hybridRequest<T>(url: string, body?: unknown, signal?: AbortSignal): Promise<T> {
  if (!url.startsWith('/') || url.startsWith('//') || /[\\\s\u0000-\u001f\u007f]/.test(url)) {
    throw new HybridHttpError(0, 'Private API requires a same-origin path');
  }
  let mutation: RequestInit = {};
  if (body !== undefined) {
    const tokens = document.cookie.split(';').map(item => item.trim()).filter(item => item.startsWith('csrftoken='));
    const token = tokens[0]?.slice('csrftoken='.length);
    if (tokens.length !== 1 || !token || !/^[A-Za-z0-9_-]{1,128}$/.test(token)) {
      throw new HybridHttpError(403, 'CSRF token unavailable; refresh the authenticated session');
    }
    mutation = { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRFToken': token }, body: JSON.stringify(body) };
  }
  const response = await fetch(url, {
    credentials: 'same-origin', cache: 'no-store', redirect: 'error', signal, ...mutation,
  });
  if (!response.ok) {
    let reason = `HTTP ${response.status}`;
    try {
      const value = await response.json();
      const message = typeof value?.error === 'string' ? value.error : value?.error?.message;
      if (typeof message === 'string' && message.length <= 4096) reason += `: ${message}`;
    } catch { /* Ответ без JSON: сохраняем HTTP-причину. */ }
    throw new HybridHttpError(response.status, reason);
  }
  return response.json();
}
export const terminalBase = (target: string) => `/api/puppet/${encodeURIComponent(target)}/term`;
export const reasonOf = (error: unknown) => error instanceof Error ? error.message : 'неизвестная ошибка';
