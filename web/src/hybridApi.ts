// Общая приватная HTTP-граница: Django cookie/CSRF, конечное ожидание, без replay.
export class HybridHttpError extends Error {
  constructor(public status: number, message: string, public code?: string,
    public epoch?: string, public retryable?: boolean) { super(message); }
}
export const PRIVATE_REQUEST_TIMEOUT_MS = 30_000;

export async function hybridRequest<T>(url: string, body?: unknown, signal?: AbortSignal): Promise<T> {
  if (!url.startsWith('/') || url.startsWith('//') || /[\\\s\u0000-\u001f\u007f]/.test(url)) {
    throw new HybridHttpError(0, 'Private API requires a same-origin path');
  }
  signal?.throwIfAborted();
  let mutation: RequestInit = {};
  if (body !== undefined) {
    const tokens = document.cookie.split(';').map(item => item.trim()).filter(item => item.startsWith('csrftoken='));
    const token = tokens[0]?.slice('csrftoken='.length);
    if (tokens.length !== 1 || !token || !/^[A-Za-z0-9_-]{1,128}$/.test(token)) {
      throw new HybridHttpError(403, 'CSRF token unavailable; refresh the authenticated session', 'csrf_failed');
    }
    mutation = { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRFToken': token }, body: JSON.stringify(body) };
  }
  const controller = new AbortController();
  let rejectPending: (reason: unknown) => void = () => {};
  const cancelled = new Promise<never>((_, reject) => { rejectPending = reject; });
  const abort = () => {
    rejectPending(signal?.reason ?? new DOMException('Request aborted', 'AbortError'));
    controller.abort(signal?.reason);
  };
  signal?.addEventListener('abort', abort, { once: true });
  const timer = setTimeout(() => {
    // Сначала фиксируем timeout: fetch может немедленно ответить AbortError.
    rejectPending(new HybridHttpError(0, 'Private request timed out; outcome may be unknown', 'request_timeout'));
    controller.abort();
  }, PRIVATE_REQUEST_TIMEOUT_MS);
  try {
    // Один deadline покрывает получение headers и чтение тела, включая transport,
    // который не соблюдает AbortSignal. У позднего результата больше нет consumer.
    return await Promise.race([cancelled, (async () => {
      const response = await fetch(url, {
        credentials: 'same-origin', cache: 'no-store', redirect: 'error', signal: controller.signal, ...mutation,
      });
      if (!response.ok) {
        let reason = `HTTP ${response.status}`;
        let code: string | undefined, epoch: string | undefined, retryable: boolean | undefined;
        try {
          const value = await response.json();
          const message = typeof value?.error === 'string' ? value.error : value?.error?.message;
          if (typeof message === 'string' && message.length <= 4096) reason += `: ${message}`;
          if (typeof value?.error?.code === 'string' && /^[a-z][a-z0-9_]{0,127}$/.test(value.error.code)) code = value.error.code;
          if (typeof value?.epoch === 'string' && value.epoch.length <= 512) epoch = value.epoch;
          if (typeof value?.error?.retryable === 'boolean') retryable = value.error.retryable;
        } catch { /* Ответ без JSON: сохраняем HTTP-причину. */ }
        throw new HybridHttpError(response.status, reason, code, epoch, retryable);
      }
      return response.json() as Promise<T>;
    })()]);
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener('abort', abort);
  }
}
export const terminalBase = (target: string) => `/api/puppet/${encodeURIComponent(target)}/term`;
export const reasonOf = (error: unknown) => error instanceof Error ? error.message : 'неизвестная ошибка';
