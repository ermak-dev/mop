// Общее для компонентов (#298): та же запись чисел и корзин, что была на
// старой странице, чтобы перенос не менял вид. Корзину (kind) считает
// сервер (mop/server/web.py), здесь только её имя и цвет для человека.
import type { Kind } from "../types";

export const KIND_ONE: Record<Kind, string> = {
  free: "свободен", busy: "занят", sick: "болен", silent: "агент молчит", down: "не работает",
};
// Цвета старой страницы: зелёный, янтарный, красный, фиолетовый, серый.
export const KIND_COLOR: Record<Kind, string> = {
  free: "green", busy: "yellow", sick: "red", silent: "violet", down: "gray",
};

/** Место клона: КБ -> "N GB" | "N MB", "-" без данных. */
export function gb(kb: number | null | undefined): string {
  if (kb == null) return "-";
  return kb >= 1048576 ? `${(kb / 1048576).toFixed(0)} GB` : `${(kb / 1024).toFixed(0)} MB`;
}

/** МБ -> "N GB", "-" без данных. */
export function gbOfMb(mb: number | null | undefined): string {
  return mb == null ? "-" : `${(mb / 1024).toFixed(0)} GB`;
}

/** Токены коротко: 1.2k, 15M, 2.3G. */
export function human(n: number): string {
  for (const [u, d] of [["G", 1e9], ["M", 1e6], ["k", 1e3]] as const) {
    if (n >= d) { const v = n / d; return (v < 10 ? v.toFixed(1) : v.toFixed(0)) + u; }
  }
  return String(n);
}

/** Та же запись, что render.ratio в питоне (#243): "свободно/всего". */
export function ratio(free: number | null | undefined, total: number | null | undefined): string {
  if (free == null && total == null) return "-";
  return `${free == null ? "-" : free}/${total == null ? "-" : total}`;
}

/** Время события журнала: чч:мм:сс по часам браузера, "-" без метки. */
export function hhmm(ts: number | null | undefined): string {
  return ts ? new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "-";
}

/** Корзина узла по строке состояния, как на старой странице. */
export function nodeKind(state: string): Kind {
  if (/ready$/.test(state)) return "free";
  if (/draining|closed/.test(state)) return "busy";
  return "down";
}
