export type RawRecord = Record<string, unknown>;
export interface HistoryPage { epoch: string; session_id: string; cursor: string; records: RawRecord[]; reset: boolean; has_more: boolean; partial: boolean }
export interface HistoryState { epoch: string | null; session_id: string | null; cursor: string | null; records: RawRecord[]; partial: boolean }
export const emptyHistory: HistoryState = { epoch: null, session_id: null, cursor: null, records: [], partial: false };

export function mergeHistory(state: HistoryState, page: HistoryPage): HistoryState {
  const replace = page.reset || state.epoch !== page.epoch || state.session_id !== page.session_id;
  if (!replace && state.cursor === page.cursor) return { ...state, partial: page.partial };
  const records = replace ? [] : [...state.records];
  const ids = new Map(records.map((r, i) => [typeof r.uuid === 'string' ? `${r.type}:${r.uuid}` : `offset:${i}`, i]));
  for (const record of page.records) {
    const id = typeof record.uuid === 'string' ? `${record.type}:${record.uuid}` : null;
    const index = id === null ? undefined : ids.get(id);
    if (index !== undefined) records[index] = record;
    else { if (id !== null) ids.set(id, records.length); records.push(record); }
  }
  return { epoch: page.epoch, session_id: page.session_id, cursor: page.cursor, records, partial: page.partial };
}

export interface ChatEntry { key: string; kind: string; origin: string; text?: string; id?: string; name?: string; input?: unknown; result?: unknown; error?: boolean; resultOrigin?: string; resultRaw?: RawRecord; raw: RawRecord }
const object = (value: unknown): RawRecord | null => value !== null && typeof value === 'object' && !Array.isArray(value) ? value as RawRecord : null;
export function chatEntries(records: RawRecord[]): ChatEntry[] {
  const entries: ChatEntry[] = [];
  const tools = new Map<string, ChatEntry>();
  records.forEach((raw, offset) => {
    const key = `${offset}:${raw.uuid ?? ''}`;
    const origin = raw.type === 'assistant' ? 'assistant' : typeof raw.origin === 'string' ? raw.origin : 'unknown';
    const base = { key, origin, raw };
    if (raw.type !== 'assistant' && raw.type !== 'user') {
      entries.push({ ...base, kind: raw.subtype === 'compact_boundary' ? 'compaction' : raw.type === 'system' || raw.type === 'summary' ? 'meta' : 'unknown' });
      return;
    }
    const message = object(raw.message);
    const content = typeof raw.message === 'string' ? raw.message : message?.content;
    if (typeof content === 'string') entries.push({ ...base, kind: 'text', text: content });
    else if (Array.isArray(content)) content.forEach((value, i) => {
      const block = object(value);
      const item = { ...base, key: `${key}:${i}` };
      if (block?.type === 'text' && typeof block.text === 'string') entries.push({ ...item, kind: 'text', text: block.text });
      else if (block?.type === 'tool_use' && typeof block.id === 'string') {
        const tool: ChatEntry = { ...item, kind: 'tool', id: block.id, name: typeof block.name === 'string' ? block.name : 'tool_use', input: block.input };
        tools.set(block.id, tool); entries.push(tool);
      } else if (block?.type === 'tool_result' && typeof block.tool_use_id === 'string') {
        const tool = tools.get(block.tool_use_id);
        if (tool && tool.result === undefined) { tool.result = block.content; tool.error = block.is_error === true; tool.resultOrigin = origin; tool.resultRaw = raw; }
        else entries.push({ ...item, kind: 'result', id: block.tool_use_id, result: block.content, error: block.is_error === true });
      } else entries.push({ ...item, kind: 'unknown', raw: { ...raw, block: value } });
    });
    else entries.push({ ...base, kind: 'unknown' });
    // Метаданные не превращаются в обещание завершённого хода.
    const metadata = Object.fromEntries(Object.entries(raw).filter(([k]) => !['uuid', 'type', 'message', 'origin', 'timestamp', 'parentUuid'].includes(k)));
    const messageMeta = message ? Object.fromEntries(Object.entries(message).filter(([k]) => k !== 'content' && k !== 'role')) : {};
    if (Object.keys(metadata).length || Object.keys(messageMeta).length) entries.push({ ...base, key: `${key}:meta`, kind: 'meta', raw: { ...metadata, message: messageMeta } });
  });
  return entries;
}

export function encodeInput(text: string): string[] {
  const bytes = new TextEncoder().encode(text);
  const parts: string[] = [];
  for (let start = 0; start < bytes.length; start += 4096) parts.push(btoa(String.fromCharCode(...bytes.subarray(start, start + 4096))));
  return parts;
}
export const STOP = '\u001b';
export function humanInput(text: string, interrupt = false): string[] {
  // Вставка добавляется к текущей строке CLI, не стирает чужой черновик.
  for (const char of text) {
    const code = char.charCodeAt(0);
    if ((code < 32 && ![9, 10, 13].includes(code)) || code === 127) throw new Error('terminal control characters are not allowed in chat text');
  }
  return encodeInput(`${interrupt ? STOP : ''}\u001b[200~${text}\u001b[201~\r`);
}
export function decodeOutput(data: string): Uint8Array {
  return Uint8Array.from(atob(data), c => c.charCodeAt(0));
}
