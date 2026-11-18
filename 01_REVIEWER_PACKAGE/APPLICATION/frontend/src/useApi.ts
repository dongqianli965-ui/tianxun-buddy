/** 统一请求状态：每个页面一个实例，trace 共享，页头可见最近调用。 */
import { ref } from "vue";
import { ApiError } from "./api";

export interface TraceEntry {
  label: string;
  at: string;
  ms: number;
  ok: boolean;
  status: number;
  message: string;
}

export const traceLog = ref<TraceEntry[]>([]);

export function formatError(e: unknown): string {
  if (e instanceof ApiError) {
    const code = e.code ? `[${e.code}] ` : "";
    const status = e.status ? `HTTP ${e.status} · ` : "";
    return `${status}${code}${e.message}`;
  }
  if (e instanceof Error) return e.message;
  return String(e);
}

export function useApi() {
  const busy = ref(false);
  const error = ref("");
  const result = ref<unknown>(null);
  const lastLabel = ref("");

  async function run<T>(label: string, fn: () => Promise<T>): Promise<T | null> {
    busy.value = true;
    error.value = "";
    result.value = null;
    lastLabel.value = label;
    const started = performance.now();
    try {
      const out = await fn();
      result.value = out;
      traceLog.value = [{
        label, at: new Date().toISOString(), ms: Math.round(performance.now() - started),
        ok: true, status: 200, message: "",
      }, ...traceLog.value].slice(0, 30);
      return out;
    } catch (e) {
      error.value = formatError(e);
      const status = e instanceof ApiError ? e.status : 0;
      traceLog.value = [{
        label, at: new Date().toISOString(), ms: Math.round(performance.now() - started),
        ok: false, status, message: error.value,
      }, ...traceLog.value].slice(0, 30);
      return null;
    } finally {
      busy.value = false;
    }
  }

  function reset(): void {
    error.value = "";
    result.value = null;
    lastLabel.value = "";
  }

  return { busy, error, result, lastLabel, run, reset };
}
