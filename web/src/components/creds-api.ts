// Разговор страницы с сервером про кредиты (#300): два маршрута, JSON на
// POST, ответ в ту же строку. Код уходит только в запрос, страница его не
// хранит. Секретов в ответах не бывает: сервер отдаёт только url, owner и
// текст отказа.
import type { Cred } from "../types";

export interface Reply { ok: boolean; url?: string; owner?: string; error?: string }

export async function post(url: string, body: Record<string, string>): Promise<Reply> {
  try {
    const r = await fetch(url, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    const j = (await r.json()) as Partial<Reply>;
    return { ...j, ok: r.ok && !j.error };
  } catch {
    return { ok: false, error: "сервер не ответил" };
  }
}

export const loginStart = (name: string) => post("/api/creds/login/start", { name });
export const loginCode = (name: string, code: string) => post("/api/creds/login/code", { name, code });

// Кнопка авторизации -- только у claude: у ключевых провайдеров входа нет,
// ключ сдаётся командой mop cred add.
export const canAuthorize = (c: Cred) => c.profile === "claude";

// Цвет Badge по слову статуса, которое сервер уже собрал (cred_status_word):
// страница не разбирает статус второй раз, только красит.
export function statusColor(status: string): string {
  if (status.startsWith("активен")) return "green";
  if (status.startsWith("ждёт квоты")) return "yellow";
  if (status.startsWith("ждёт ручной авторизации")) return "red";
  return "gray";
}

export const percentText = (p: number | null) => (p == null ? "-" : `${p}%`);
