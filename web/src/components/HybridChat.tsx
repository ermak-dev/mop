import { useLayoutEffect, useMemo, useRef } from 'react';
import { Text } from '@mantine/core';
import { chatEntries } from '../hybridProtocol';
import type { ChatEntry, RawRecord } from '../hybridProtocol';
const json = (value: unknown) => typeof value === 'string' ? value : JSON.stringify(value, null, 2);
const originLabel = (origin: string) => origin === 'assistant' ? 'Ассистент' : origin === 'human' ? 'Человек' : origin === 'peer' ? 'Папет / мастер' : 'Сообщение';
function Entry({ entry, showMetadata }: { entry: ChatEntry; showMetadata: boolean }) {
  if (entry.kind === 'text') return <article className="hybrid-message">
    {showMetadata && <Text size="xs" c="dimmed" mb={4}>{originLabel(entry.origin)}</Text>}
    <div className="hybrid-chat-text">{entry.text}</div>
  </article>;
  const tool = entry.kind === 'tool' || entry.kind === 'result';
  const title = tool ? `${entry.name ?? 'Результат инструмента'} (${entry.id})`
    : entry.kind === 'compaction' ? 'Сжатие контекста' : entry.kind === 'meta' ? 'Метаданные / частичная запись' : 'Неизвестная запись: показаны исходные данные';
  return <details className="hybrid-detail">
    <summary>{title}{entry.error ? ' — ошибка инструмента' : ''}</summary>
    <Text size="xs" c="dimmed" my={4}>{originLabel(entry.origin)}</Text>
    {tool ? <>
      {entry.input !== undefined && <pre aria-label="аргументы инструмента">{json(entry.input)}</pre>}
      {entry.result !== undefined ? <pre aria-label="результат инструмента">{json(entry.result)}</pre> : <Text size="sm" c="dimmed">Результат ещё не получен в истории</Text>}
      {entry.resultOrigin && <Text size="xs" c="dimmed">Источник результата: {originLabel(entry.resultOrigin)}</Text>}
      <details><summary>Исходная запись</summary><pre>{json(entry.raw)}</pre></details>
      {entry.resultRaw && <details><summary>Исходная запись результата</summary><pre>{json(entry.resultRaw)}</pre></details>}
    </> : <pre>{json(entry.raw)}</pre>}
  </details>;
}
export function HybridChat({ records, partial, loading, error, showMetadata }: {
  records: RawRecord[]; partial: boolean; loading: boolean; error: string | null; showMetadata: boolean;
}) {
  const entries = useMemo(() => chatEntries(records).filter(entry => showMetadata || entry.kind === 'text'), [records, showMetadata]);
  const viewport = useRef<HTMLDivElement>(null);
  const follow = useRef(true);
  useLayoutEffect(() => {
    if (follow.current && viewport.current) viewport.current.scrollTop = viewport.current.scrollHeight;
  }, [entries]);
  return <>
    {error && <Text role="alert" c="red" size="sm" px="md">История недоступна: {error}. Последние строки доступны во вкладке «Терминал».</Text>}
    {showMetadata && partial && <Text size="xs" c="dimmed" px="md">Неполная запись: продолжение появится после обновления</Text>}
    <div ref={viewport} className="hybrid-history" role="region" aria-label="История чата" tabIndex={0}
      onScroll={() => { const el = viewport.current!; follow.current = el.scrollHeight - el.clientHeight - el.scrollTop < 16; }}>
      <div className="hybrid-transcript">
        {entries.map(entry => <Entry key={entry.key} entry={entry} showMetadata={showMetadata} />)}
        {!entries.length && !error && <Text size="sm" c="dimmed" role={loading ? 'status' : undefined}>{loading ? 'Загрузка истории' : 'В истории пока нет записей'}</Text>}
      </div>
    </div>
  </>;
}
