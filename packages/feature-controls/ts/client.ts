export type Json = null | boolean | number | string | Json[] | { [key: string]: Json };
export interface SnapshotScope { application: string; environment: string; surface: string }
export interface ResolvedDecision {
  value: Json; reason: string; expires_at: number;
  configuration_revision: number | null; kill_generation: number;
}
export interface Snapshot extends SnapshotScope {
  schema_version: 1; generated_at: number; expires_at: number;
  decisions: Record<string, ResolvedDecision>;
}
const record = (v: unknown): v is Record<string, unknown> =>
  v !== null && typeof v === "object" && !Array.isArray(v);
const time = (v: unknown): v is number => Number.isSafeInteger(v) && (v as number) >= 0;
export function validateSnapshot(value: unknown, scope: SnapshotScope, now = Date.now()): value is Snapshot {
  const snapshotFields = ["schema_version", "application", "environment", "surface", "generated_at", "expires_at", "decisions"];
  const decisionFields = ["value", "reason", "expires_at", "configuration_revision", "kill_generation"];
  if (!record(value) || value.schema_version !== 1 || !time(now) ||
      Object.keys(value).length !== snapshotFields.length || Object.keys(value).some(k => !snapshotFields.includes(k)) ||
      value.application !== scope.application || value.environment !== scope.environment ||
      value.surface !== scope.surface || !time(value.generated_at) || value.generated_at > now ||
      !time(value.expires_at) || value.expires_at <= now || value.generated_at >= value.expires_at ||
      !record(value.decisions) || Object.keys(value.decisions).length > 1000) return false;
  return Object.entries(value.decisions).every(([key, d]) =>
    /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/.test(key) && record(d) &&
    Object.keys(d).length === decisionFields.length && Object.keys(d).every(k => decisionFields.includes(k)) &&
    typeof d.reason === "string" && d.reason.length <= 128 && time(d.expires_at) &&
    d.expires_at <= (value.expires_at as number) && time(d.kill_generation) &&
    (d.configuration_revision === null || time(d.configuration_revision)) &&
    Object.hasOwn(d, "value") && jsonValue(d.value));
}
function jsonValue(value: unknown, depth = 0): value is Json {
  if (depth > 20) return false;
  if (value === null || typeof value === "boolean" || typeof value === "string") return true;
  if (typeof value === "number") return Number.isFinite(value);
  if (Array.isArray(value)) return value.length <= 1000 && value.every(v => jsonValue(v, depth + 1));
  return record(value) && Object.keys(value).length <= 1000 && Object.values(value).every(v => jsonValue(v, depth + 1));
}
export function decisionValue<T extends Json>(snapshot: unknown, key: string, baseline: T,
  scope: SnapshotScope, now = Date.now(), accepts?: (value: Json) => value is T): T {
  if (!validateSnapshot(snapshot, scope, now) || !Object.hasOwn(snapshot.decisions, key)) return baseline;
  const decision = snapshot.decisions[key];
  if (decision.expires_at <= now) return baseline;
  const value = decision.value;
  const valid = accepts ? accepts(value) : value === null ? baseline === null :
    ["boolean", "number", "string"].includes(typeof value) && typeof value === typeof baseline;
  return valid ? value as T : baseline;
}
export class DecisionClient {
  private current: Snapshot | null = null;
  private session: string | null = null;
  constructor(readonly scope: SnapshotScope) {}
  bindSession(session: string | null): void {
    if (session !== this.session) this.clear();
    this.session = session;
  }
  setSnapshot(snapshot: unknown, session: string, now = Date.now()): boolean {
    if (session !== this.session || !validateSnapshot(snapshot, this.scope, now)) { this.clear(); return false; }
    this.current = JSON.parse(JSON.stringify(snapshot)) as Snapshot;
    return true;
  }
  get<T extends Json>(key: string, baseline: T, now = Date.now(), accepts?: (value: Json) => value is T): T {
    return decisionValue(this.current, key, baseline, this.scope, now, accepts);
  }
  clear(): void { this.current = null; }
}
