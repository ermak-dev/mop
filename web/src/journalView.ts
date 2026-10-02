// Представление события журнала (#422): та же запись, что отдал сервер
// (journal_entry, #424 расширяет конверт), -- здесь только слова для
// человека. Хелперы не знают, откуда запись пришла: главная, страница
// журнала и диалог читают одни и те же строки.
import type { JournalEntry } from "./types";

/** Полная дата события: дд.мм.гггг чч:мм:сс + пояс смотрящего, «-» без
 *  метки. Главная пишет одни чч:мм:сс (format.hhmmss) -- журнал живёт
 *  дольше дня, и без даты утреннее событие не отличить от вчерашнего, а
 *  без пояса -- чей это день (#422, ревью 2: пояс обязателен в записи).
 *  timeZone -- для проверок, страница его не передаёт. */
export function fullAt(ts: number | null | undefined, timeZone?: string): string {
  if (!ts) return "-";
  const date = new Date(ts * 1000);
  // конечная, но вне диапазона Date метка (бэкенд допускает 1e20) --
  // прочерк, а не RangeError, роняющий всю страницу журнала (#422, ревью 3)
  if (isNaN(date.getTime())) return "-";
  const fmt = new Intl.DateTimeFormat("ru-RU", {
    timeZone, year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23",
    timeZoneName: "shortOffset",
  });
  const get = (t: string) => fmt.formatToParts(date).find((x) => x.type === t)?.value ?? "";
  // node печатает «GMT» вместо «UTC» -- это один и тот же нулевой пояс,
  // приводим к каноническому виду, чтобы запись не зависела от среды
  const zone = get("timeZoneName").replace(/^GMT$/, "UTC");
  return `${get("day")}.${get("month")}.${get("year")} ${get("hour")}:${get("minute")}:${get("second")} ${zone}`;
}

/** Заголовок карточки: «событие — папет»; без папета («up» пишет агент
 *  узла) -- одно событие. */
export function entryTitle(e: JournalEntry): string {
  return e.name && e.name !== "-" ? `${e.event} — ${e.name}` : e.event;
}

/** Осмысленное описание по виду события (#422): не сырой text, а фраза.
 *  Виды -- те, что публикует агент (_event): send, idle, wipe, type, up.
 *  Незнакомый вид не выдумывает своей фразы -- остаётся text как есть. */
export function journalText(e: JournalEntry): string {
  switch (e.event) {
    // агент пишет «from <имя>» -- фраза по-русски, имя остаётся как есть
    case "send": return `сообщение от ${e.text.replace(/^from\s+/, "")}`.trim();
    case "idle": return e.text ? `дождался простоя: ${e.text}` : "дождался простоя";
    case "wipe": return "клон снесён";
    case "type": return e.text ? `команда в клоне: ${e.text}` : "команда в клоне";
    // «up» агент пишет латиницей «agent on <узел>» -- переводим узел в фразу
    case "up": {
      const node = e.text.replace(/^agent on\s+/, "");
      return node && node !== e.text ? `агент на узле ${node} поднялся` : "агент поднялся";
    }
    default: return e.text;
  }
}
