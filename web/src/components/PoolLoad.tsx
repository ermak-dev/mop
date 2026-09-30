// Загрузка пула (#378): строка бейджей в шапке и полоса аллокаций во всю
// ширину окна. Суммы и сегменты считает сервер (web.load), здесь только
// слова, цвета и ширина сегмента -- его доля во всех местах.
import { Badge, Group, Progress, Tooltip } from "@mantine/core";
import type { Counts, Load, LoadKind } from "../types";
import { KIND_COLOR, KIND_WORD, plural, presentKinds } from "./format";

// «Прочее выделенное» и свободные места -- нейтральные: это не корзины
// папетов, цвета корзин остаются за ними.
const SEGMENT_COLOR: Record<LoadKind, string> = {
  busy: KIND_COLOR.busy, free: KIND_COLOR.free, sick: KIND_COLOR.sick,
  silent: KIND_COLOR.silent, other: "gray.6", vacant: "gray.3",
};

const vacant = (n: number) => plural(n, "свободное место", "свободных места", "свободных мест");

/** Подсказка сегмента: число и слово. */
export function segmentLabel(kind: LoadKind, n: number): string {
  if (kind === "other") return `${n} — прочее выделенное`;
  if (kind === "vacant") return vacant(n);
  return `${n} ${KIND_WORD[kind].many}`;
}

/** Бейджи шапки: папеты и места пула, корзины -- только непустые. Снимок
 *  без load (сервер до #378) -- прежняя шапка: папеты и корзины. */
export function LoadBadges({ load, counts }: { load?: Load; counts: Counts }) {
  return (
    <Group gap="xs" data-testid="chips">
      <Badge size="lg" variant="light" color="gray">{plural(counts.puppets, "папет", "папета", "папетов")}</Badge>
      {load && <Badge size="lg" variant="light" color="gray">всего {plural(load.total, "место", "места", "мест")}</Badge>}
      {load && <Badge size="lg" variant="light" color="gray">выделено {load.allocated}</Badge>}
      {presentKinds(counts).map((k) => (
        <Badge key={k} size="lg" variant="light" color={KIND_COLOR[k]}>{counts[k]} {KIND_WORD[k].many}</Badge>
      ))}
      {load && <Badge size="lg" variant="light" color="gray">{vacant(load.free_slots)}</Badge>}
    </Group>
  );
}

/** Полоса аллокаций: сегменты в порядке сервера, пустые не рисуются. */
export function LoadBar({ load }: { load: Load }) {
  return (
    <Progress.Root size="xl" radius={0} data-testid="load-bar">
      {load.total > 0 && load.segments.filter((s) => s.slots > 0).map((s) => (
        <Tooltip key={s.kind} label={segmentLabel(s.kind, s.slots)} withArrow>
          <Progress.Section value={(s.slots / load.total) * 100} color={SEGMENT_COLOR[s.kind]}
                            data-testid={`load-${s.kind}`} />
        </Tooltip>
      ))}
    </Progress.Root>
  );
}
