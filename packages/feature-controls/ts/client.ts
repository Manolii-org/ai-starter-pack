export type Json = null | boolean | number | string | Json[] | { [key: string]: Json };
export interface SnapshotScope {
  ecosystem_id: string; deployment_id: string; feature_namespace: string;
  application_id: string; environment_id: string; surface_id: string; context_scope: string;
}
export interface SnapshotWatermark {
  configuration_revision: number; kill_generation: number; time_highwater: number;
}
export interface ResolvedDecision {
  status: "resolved" | "baseline" | "denied";
  value: Json; reason: string; expires_at: number;
  configuration_revision: number | null; kill_generation: number;
}
export interface Snapshot extends SnapshotScope, SnapshotWatermark {
  schema_version: 1; generated_at: number; expires_at: number;
  decisions: Record<string, ResolvedDecision>;
}
const record = (v: unknown): v is Record<string, unknown> =>
  v !== null && typeof v === "object" && !Array.isArray(v) &&
  (Object.getPrototypeOf(v) === Object.prototype || Object.getPrototypeOf(v) === null);
const time = (v: unknown): v is number => Number.isSafeInteger(v) && (v as number) >= 0;
const identifier = (v: unknown): v is string => typeof v === "string" && /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/.test(v);
const watermark = (v: unknown): v is SnapshotWatermark => record(v) &&
  time(v.configuration_revision) && time(v.kill_generation) && time(v.time_highwater);
export function validateSnapshot(value: unknown, scope: SnapshotScope, now: number, minimum: SnapshotWatermark): value is Snapshot {
  const scopeFields = ["ecosystem_id", "deployment_id", "environment_id", "feature_namespace", "application_id", "surface_id", "context_scope"] as const;
  const snapshotFields = ["schema_version", ...scopeFields, "generated_at", "expires_at", "decisions",
    "configuration_revision", "kill_generation", "time_highwater"];
  const decisionFields = ["value", "reason", "status", "expires_at", "configuration_revision", "kill_generation"];
  if (!record(value) || !record(scope) || value.schema_version !== 1 || !time(now) || !watermark(minimum) ||
      now < minimum.time_highwater || !watermark(value) || !time(value.generated_at) || value.time_highwater > value.generated_at ||
      value.configuration_revision < minimum.configuration_revision || value.kill_generation < minimum.kill_generation ||
      Object.keys(value).length !== snapshotFields.length || Object.keys(value).some(k => !snapshotFields.includes(k)) ||
      scopeFields.some(k => !identifier(scope[k]) || value[k] !== scope[k]) ||
      !time(value.generated_at) || value.generated_at > now ||
      !time(value.expires_at) || value.expires_at <= now || value.generated_at >= value.expires_at ||
      !record(value.decisions) || Object.keys(value.decisions).length > 64) return false;
  return Object.entries(value.decisions).every(([key, d]) =>
    identifier(key) && record(d) &&
    Object.keys(d).length === decisionFields.length && Object.keys(d).every(k => decisionFields.includes(k)) &&
    typeof d.reason === "string" && d.reason.length <= 128 && time(d.expires_at) &&
    d.expires_at <= (value.expires_at as number) && time(d.kill_generation) && d.kill_generation === value.kill_generation &&
    (d.configuration_revision === null && value.configuration_revision === 0 || time(d.configuration_revision) && d.configuration_revision === value.configuration_revision) &&
    ["resolved", "baseline", "denied"].includes(d.status as string) &&
    (d.status !== "resolved" || d.kill_generation === value.kill_generation && d.configuration_revision === value.configuration_revision) &&
    !(d.status === "denied" && d.value === true) && Object.hasOwn(d, "value") && jsonValue(d.value));
}
function jsonValue(value: unknown, depth = 0): value is Json {
  if (depth > 20) return false;
  if (value === null || typeof value === "boolean" || typeof value === "string") return true;
  if (typeof value === "number") return Number.isFinite(value) && (!Number.isInteger(value) || Number.isSafeInteger(value));
  if (Array.isArray(value)) return value.length <= 1000 && value.every(v => jsonValue(v, depth + 1));
  return record(value) && Object.keys(value).length <= 1000 && Object.values(value).every(v => jsonValue(v, depth + 1));
}
export function decisionValue<T extends Json>(snapshot: unknown, key: string, baseline: T,
  scope: SnapshotScope, now: number, minimum: SnapshotWatermark, accepts?: (value: Json) => value is T): T {
  if (!validateSnapshot(snapshot, scope, now, minimum) || !Object.hasOwn(snapshot.decisions, key)) return baseline;
  const decision = snapshot.decisions[key];
  if (decision.expires_at <= now && decision.status !== "denied") return baseline;
  return projectedValue(decision, baseline, accepts);
}
function projectedValue<T extends Json>(decision: ResolvedDecision, baseline: T, accepts?: (value: Json) => value is T): T {
  const value = copyJson(decision.value) as Json;
  if (decision.status === "denied" && value === false && typeof baseline === "boolean") return false as T;
  const valid = accepts ? accepts(copyJson(value) as Json) : value === null ? baseline === null :
    ["boolean", "number", "string"].includes(typeof value) && typeof value === typeof baseline;
  if (!valid && decision.status === "denied") throw new Error("denied value does not satisfy accessor type");
  return valid ? value as T : baseline;
}
export class DecisionClient {
  private current: Snapshot | null = null;
  private retained = new Map<string, ResolvedDecision>();
  private session: string | null = null;
  private readonly expected: SnapshotScope;
  private observed: SnapshotWatermark;
  constructor(scope: SnapshotScope, minimum: SnapshotWatermark) {
    if (!watermark(minimum)) throw new Error("invalid watermark");
    this.expected = { ...scope }; this.observed = { ...minimum };
  }
  get scope(): SnapshotScope { return { ...this.expected }; }
  get watermark(): SnapshotWatermark { return { ...this.observed }; }
  bindSession(session: string | null, context_scope: string | null): void {
    if (session !== null && !identifier(context_scope)) throw new Error("context scope required");
    if (session !== this.session || context_scope !== this.expected.context_scope) this.clear();
    this.session = session;
    if (context_scope !== null) this.expected.context_scope = context_scope;
  }
  setSnapshot(snapshot: unknown, session: string, now = Date.now()): boolean {
    if (session !== this.session) return false;
    if (!validateSnapshot(snapshot, this.expected, now, this.observed)) return false;
    const candidate = copyJson(snapshot) as Snapshot;
    if (!validateSnapshot(candidate, this.expected, now, this.observed)) return false;
    if (!this.supersedes(candidate)) return false;
    const retained = new Map<string, ResolvedDecision>();
    for (const [key, d] of [...this.retained, ...Object.entries(this.current?.decisions ?? {})])
      if (d.status === "denied" && !Object.hasOwn(candidate.decisions, key)) retained.set(key, d);
    if (retained.size + Object.keys(candidate.decisions).length > 64) return false;
    this.current = candidate;
    this.retained = retained;
    this.observed = { configuration_revision: candidate.configuration_revision,
      kill_generation: candidate.kill_generation, time_highwater: now };
    return true;
  }
  private supersedes(candidate: Snapshot): boolean {
    const prior = this.current;
    if (!prior || candidate.configuration_revision !== prior.configuration_revision ||
        candidate.kill_generation !== prior.kill_generation) return true;
    return candidate.generated_at > prior.generated_at || sameJson(candidate, prior);
  }
  get<T extends Json>(key: string, baseline: T, now = Date.now(), accepts?: (value: Json) => value is T): T {
    const previous = this.current && Object.hasOwn(this.current.decisions, key) ? this.current.decisions[key] : this.retained.get(key);
    if (previous?.status === "denied") {
      const value = projectedValue(previous, baseline, accepts);
      if (time(now) && now >= this.observed.time_highwater) this.observed.time_highwater = now;
      return value;
    }
    if (!time(now) || now < this.observed.time_highwater) return baseline;
    const value = decisionValue(this.current, key, baseline, this.expected, now, this.observed, accepts);
    this.observed.time_highwater = now;
    return value;
  }
  clear(): void { this.current = null; this.retained = new Map(); }
}
function sameJson(a: unknown, b: unknown): boolean {
  if (Array.isArray(a) || Array.isArray(b))
    return Array.isArray(a) && Array.isArray(b) && a.length === b.length && a.every((v, i) => sameJson(v, b[i]));
  if (record(a) || record(b)) return record(a) && record(b) && Object.keys(a).length === Object.keys(b).length &&
    Object.keys(a).every(k => Object.hasOwn(b, k) && sameJson(a[k], b[k]));
  return a === b;
}
function copyJson(value: unknown): unknown {
  if (Array.isArray(value)) return value.map((v: unknown) => copyJson(v));
  if (record(value)) return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, copyJson(v)]));
  return value;
}
