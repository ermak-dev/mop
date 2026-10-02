// Сессия дашборда (#416, контракт #415): кто смотрит страницу -- гость или
// оператор с ролью. Backend ещё нет, форма запросов закреплена здесь: все
// запросы same-origin (cookie сессии), POST -- JSON. Пароль дальше тела
// запроса не уходит и нигде не хранится.
export interface Session { login: string | null; role: "admin" | "user" | null }

export const GUEST: Session = { login: null, role: null };

/** Отказ входа: сервер назвал причину (401) или лимит (429). */
export class LoginRefused extends Error {
  constructor(public status: number, public reason: string) {
    super(reason);
  }
}

const post = (url: string, body: unknown): Promise<Response> =>
  fetch(url, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

/** Кто открыл страницу. 5xx -- ошибка панели, не «гость». */
export async function fetchSession(): Promise<Session> {
  const r = await fetch("/api/session", { credentials: "same-origin" });
  if (!r.ok) throw new Error(`session: HTTP ${r.status}`);
  return r.json();
}

/** Вход логином/паролем mop. -> Session; LoginRefused при 401/429. */
export async function postLogin(login: string, password: string): Promise<Session> {
  const r = await post("/api/login", { login, password });
  if (r.status === 401) throw new LoginRefused(401, (await r.json()).error);
  if (r.status === 429) throw new LoginRefused(429, "слишком много попыток — подождите");
  if (!r.ok) throw new LoginRefused(r.status, `HTTP ${r.status}`);
  return r.json();
}

/** Выход: сервер гасит cookie, ответ -- гость. */
export async function postLogout(): Promise<Session> {
  const r = await post("/api/logout", {});
  if (!r.ok) throw new Error(`logout: HTTP ${r.status}`);
  return r.json();
}
