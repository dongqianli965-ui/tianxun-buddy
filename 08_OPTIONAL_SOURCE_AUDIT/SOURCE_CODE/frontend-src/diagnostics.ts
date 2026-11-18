/** 本次页面会话的真实 HTTP 观察值。绝不记录口令、Token、请求体或原文。 */
import { ref } from "vue";
import type { EndpointMeta } from "./endpoints";

export interface HttpObservation {
  method: string;
  path: string;
  status: number;
  ok: boolean;
  at: string;
  ms: number;
}

export const httpObservations = ref<HttpObservation[]>([]);

export function observeHttp(method: string, path: string, status: number, started: number): void {
  httpObservations.value = [{
    method: method.toUpperCase(), path: path.split("?")[0], status,
    ok: status >= 200 && status < 300, at: new Date().toISOString(),
    ms: Math.round(performance.now() - started),
  }, ...httpObservations.value].slice(0, 500);
}

function matches(template: string, path: string): boolean {
  const expected = template.split("?")[0].split("/");
  const actual = path.split("/");
  return expected.length === actual.length && expected.every((part, index) =>
    /^\{[^}]+\}$/.test(part) ? Boolean(actual[index]) : part === actual[index]);
}

export function latestObservation(endpoint: EndpointMeta): HttpObservation | undefined {
  return httpObservations.value.find(item => item.method === endpoint.method && matches(endpoint.path, item.path));
}

export function observationLabel(endpoint: EndpointMeta): string {
  const item = latestObservation(endpoint);
  if (!item) return "未探测";
  if (!item.status) return "网络请求失败";
  return `HTTP ${item.status} · ${item.ok ? "已响应" : "已拒绝或失败"}`;
}
