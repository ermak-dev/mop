// Смена модели (#468 -> #520): каталог прокси -- единственный источник
// (useModels, GET /api/admin/models), визард собирает выбор на шаге
// модели и шлёт приказ {act: "model", params: {model}}. Смена обратима
// (#513): подтверждения нет, «выполнить» шлёт POST сразу. Вердикт --
// балун (#488, слово оператора): succeeded приезжает только после
// телеметрии сервера (та же сессия, свежая запись, та же модель) --
// «диалог закрылся» подтверждением не считается. Ход операции --
// общий useAdminOp; результат в памяти страницы.
import { useOperationNotice } from "../useOperationNotice";
import { useOrder, type Signal } from "./ControlWindow";
import { opWord } from "./RestartBox";
import type { Puppet } from "../types";
import type { Session } from "../useSession";
import { useAdminOp } from "../useAdminOp";

export function ModelBox({ session, row, order = null }: {
  session: Session;
  /** Строка ростера: цель смены модели. */
  row: Puppet;
  /** Приказ-токен визарда (#520): срабатывание по изменению. */
  order?: Signal;
}) {
  const { op, submit } = useAdminOp(session, session.role === "admin" ? row.name : null);
  useOperationNotice(op, {
    scope: `${session.login}\0${session.role}\0${row.name}`,
    title: `Смена модели: ${row.name}`,
    word: op.status === "succeeded" ? "модель изменена" : opWord(op.status),
    enabled: session.role === "admin",
    onSettled: () => { void fetch("/api/pool", { credentials: "same-origin" }).catch(() => {}); },
  });
  if (session.role !== "admin") return null;
  // Вход (#520): приказ визарда; замыкание свежего рендера держит сам
  // useOrder (latest-ref). Повтор при живом ходе -- движковый дедуп.
  useOrder(order, "model", (params) => {
    void submit("model", params);
  });
  return null;
}
