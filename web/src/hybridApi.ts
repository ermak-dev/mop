// Приватный prospective-контракт #600: cookie, JSON, никаких полей личности.
export class HybridHttpError extends Error {
  constructor(public status: number, message: string) { super(message); }
}
export async function hybridRequest<T>(url: string, body?: unknown, signal?: AbortSignal): Promise<T> {
  const response = await fetch(url, {
    credentials: 'same-origin', signal,
    ...(body === undefined ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }),
  });
  if (!response.ok) {
    let reason = `HTTP ${response.status}`;
    try { const value = await response.json(); if (typeof value.error === 'string') reason += `: ${value.error}`; } catch { /* Ответ без JSON: сохраняем HTTP-причину. */ }
    throw new HybridHttpError(response.status, reason);
  }
  return response.json();
}
export const terminalBase = (target: string) => `/api/puppet/${encodeURIComponent(target)}/term`;
export const reasonOf = (error: unknown) => error instanceof Error ? error.message : 'неизвестная ошибка';
