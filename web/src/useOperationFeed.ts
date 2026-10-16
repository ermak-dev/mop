// Только уведомления UI после применения подтверждённой Operation в store.
// Ни таймер не изобретает unknown, ни SSE не становится вторым владельцем статуса.
export interface OperationFrame { id: string; action: string; target: string | null; status: string; error?: string }
type FrameListener = (frame: OperationFrame) => void;
class OperationNotifications {
  private listeners = new Set<FrameListener>();
  private own = new Set<string>();
  private seen = new Set<string>();
  frame(frame: OperationFrame) {
    const key = JSON.stringify([frame.id, frame.status]);
    if (this.seen.has(key)) return;
    this.seen.add(key);
    if (this.seen.size > 512) this.seen.delete(this.seen.values().next().value!);
    for (const listener of this.listeners) listener(frame);
  }
  accepted(_target: string | null, id: string) { this.own.add(id); }
  owns(id: string) { return this.own.has(id); }
  subscribe(listener: FrameListener) { this.listeners.add(listener); return () => { this.listeners.delete(listener); }; }
  clear() { this.own.clear(); this.seen.clear(); }
  resetForTest() { this.clear(); this.listeners.clear(); }
}
export const operations = new OperationNotifications();
