// Общее для компонентов (#298): та же запись чисел и корзин, что была на
// старой странице, чтобы перенос не менял вид. Корзину (kind) считает
// сервер (mop/server/web.py), здесь только её имя и цвет для человека.
//
// Одно место на запись (#323): склонение, короткие числа, слова корзин и
// цвет статуса кредита жили копиями в App, usage-format и creds-api.
import { KINDS, type Counts, type Kind } from "../types";

// Слова корзин: one -- у одного папета (подпись проекта), many -- у
// счётчика в шапке. Слова прежние, как были в двух таблицах.
export const KIND_WORD: Record<Kind, { one: string; many: string }> = {
  free: { one: "свободен", many: "свободны" },
  busy: { one: "занят", many: "заняты" },
  sick: { one: "болен", many: "больны" },
  silent: { one: "агент молчит", many: "агент молчит" },
  down: { one: "не работает", many: "не подняты" },
};
// Цвета старой страницы: зелёный, янтарный, красный, фиолетовый, серый.
export const KIND_COLOR: Record<Kind, string> = {
  free: "green", busy: "yellow", sick: "red", silent: "violet", down: "gray",
};

/** Корзины, в которых кто-то есть, в порядке KINDS. */
export function presentKinds(counts: Counts): Kind[] {
  return KINDS.filter((k) => counts[k]);
}

/** Склонение по числу: 1 папет, 3 папета, 11 папетов. */
export function plural(n: number, one: string, few: string, many: string): string {
  const m10 = n % 10, m100 = n % 100;
  if (m10 === 1 && m100 !== 11) return `${n} ${one}`;
  if (m10 >= 2 && m10 <= 4 && (m100 < 10 || m100 >= 20)) return `${n} ${few}`;
  return `${n} ${many}`;
}

/** Список через запятую, "-" для пустого. */
export function listOrDash(items: string[]): string {
  return items.length ? items.join(", ") : "-";
}

/** Место клона: КБ -> "N GB" | "N MB", "-" без данных. */
export function gb(kb: number | null | undefined): string {
  if (kb == null) return "-";
  return kb >= 1048576 ? `${(kb / 1048576).toFixed(0)} GB` : `${(kb / 1024).toFixed(0)} MB`;
}

/** МБ -> "N GB" вниз, "-" без данных. Вниз, как `mop node` и строка пула
 *  (#329): свободного не бывает больше, чем есть -- 1536 МБ это «1 GB». */
export function gbOfMb(mb: number | null | undefined): string {
  return mb == null ? "-" : `${Math.floor(mb / 1024)} GB`;
}

/** Токены коротко: 1.2k, 15M, 2.3G -- меньше десяти единиц с одним знаком. */
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
export function hhmmss(ts: number | null | undefined): string {
  return ts ? new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "-";
}

/** Корзина узла по строке состояния, как на старой странице. */
export function nodeKind(state: string): Kind {
  if (/ready$/.test(state)) return "free";
  if (/draining|closed/.test(state)) return "busy";
  return "down";
}

// Цвет Badge по слову статуса, которое сервер уже собрал (cred_status_word):
// страница не разбирает статус второй раз, только красит.
export function statusColor(status: string): string {
  if (status.startsWith("активен")) return "green";
  if (status.startsWith("ждёт квоты")) return "yellow";
  if (status.startsWith("ждёт ручной авторизации")) return "red";
  return "gray";
}

/** Процент худшего окна кредита, "-" без данных. */
export const percentText = (p: number | null) => (p == null ? "-" : `${p}%`);

// Время сброса квоты (#331) -- в поясе смотрящего. Сервер прежде вписывал
// его в слово статуса сам: «%H:%M:%S» в поясе сервера и без даты, и недельное
// окно через три дня читалось как «сегодня в пять утра». Теперь слово голое,
// а resets_at (эпоха, секунды) пишется здесь: «до чч:мм» сегодня и
// «до дд.мм чч:мм» в другой день; секунды не нужны. timeZone -- для проверок,
// страница его не передаёт, и пояс -- браузера.
function wallClock(ts: number, timeZone?: string): Record<string, string> {
  const fmt = new Intl.DateTimeFormat("ru-RU", {
    timeZone, year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hourCycle: "h23",
  });
  return Object.fromEntries(fmt.formatToParts(new Date(ts * 1000)).map((p) => [p.type, p.value]));
}

/** «до чч:мм» или «до дд.мм чч:мм»; "" без времени сброса. */
export function untilText(resetsAt: number | null, now: number = Date.now() / 1000, timeZone?: string): string {
  if (resetsAt == null) return "";
  const r = wallClock(resetsAt, timeZone), n = wallClock(now, timeZone);
  const today = r.year === n.year && r.month === n.month && r.day === n.day;
  return today ? `до ${r.hour}:${r.minute}` : `до ${r.day}.${r.month} ${r.hour}:${r.minute}`;
}

/** Статус кредита для ячейки: к голому «ждёт квоты» -- время сброса.
 *  Только к голому: слово сервера до #331 уже несёт своё «до …». */
export function credStatusText(status: string, resetsAt: number | null,
                               now: number = Date.now() / 1000, timeZone?: string): string {
  const until = status === "ждёт квоты" ? untilText(resetsAt, now, timeZone) : "";
  return until ? `${status} ${until}` : status;
}
