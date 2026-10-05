// Балун терминального кадра (#509): терминал операции, чей id эта страница
// не запускала (другая вкладка, другой оператор), показывается балуном --
// слово и цвет по правилам useOperationNotice, имя цели в заголовке. Свой
// ход (id из 202 этой страницы) балуном не дублируется: вердикт своего
// действия покажет его бокс (useAdminOp + useOperationNotice), один клик
// -- один балун. Слово -- словарь карточки #504: кадр несёт любое
// действие (restart, update, model, add...), слово «перезапущен» лгало бы
// про чужую правку. unknown -- жёлтый со ссылкой в журнал: правда об
// эффекте там, не в памяти страницы. Кадр, пропущенный при свёрнутой
// странице, не догоняется -- известный остаток #509.
import { useEffect } from "react";
import { Text } from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { operations } from "./useOperationFeed";

// Слово терминала (#509): тот же словарь, что был у карточки строки #504,
// -- действие кадра странице заранее не известно.
const WORDS: Record<string, string> = {
  succeeded: "выполнено",
  refused: "отказано",
  failed: "не удался",
  partial: "частично",
  unknown: "исход неизвестен",
};

/** Балун одного терминального кадра. */
function balloon(frame: { id: string; action: string; target: string | null;
                          status: string; error?: string }) {
  const word = WORDS[frame.status];
  if (!word) return;
  const title = frame.target ?? "операция";
  const message = `${word}${frame.error ? `: ${frame.error}` : ""}`;
  notifications.show({
    title,
    color: frame.status === "succeeded" ? "teal"
      : frame.status === "unknown" || frame.status === "partial" ? "yellow" : "red",
    message: frame.status === "unknown"
      // ссылка в журнал внутри балуна: правда об эффекте там
      ? <>{message} (<Text component="a" href="#journal" size="sm" c="dimmed" span>журнал</Text>)</>
      : message,
    autoClose: 8000,
  });
}

/** Подписка страницы на терминальные кадры (#509): один балун на чужой
 * терминал, дедуп -- свои id (202 этой страницы) и повторные кадры. */
export function useOperationBalloons() {
  useEffect(() => {
    const seen = new Set<string>();
    return operations.subscribe((frame) => {
      // свой id -- вердикт покажет бокс операции, не балун
      if (operations.owns(frame.id)) return;
      // красная доставка того же терминала не рождает второй балун
      if (seen.has(frame.id)) return;
      seen.add(frame.id);
      balloon(frame);
    });
  }, []);
}