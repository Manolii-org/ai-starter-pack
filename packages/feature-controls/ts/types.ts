import type { Json, ResolvedDecision } from "./client.js";
export type { Json, Snapshot, SnapshotScope, ResolvedDecision } from "./client.js";
export const PROVIDER_SEMANTICS = "growthbook:js-1.8.0:py-3.2.0:hash-2";
export interface Scope { namespace: string; application: string; environment: string }
export interface Feature {
  value_type: "boolean" | "number" | "string" | "json"; baseline: Json;
  failure: "baseline" | "deny"; surfaces: string[]; ancestors: string[];
  experiment?: { key: string; epoch: number };
}
export interface Catalog { schema_version: 1; scope: Scope; revision: number; features: Record<string, Feature> }
export interface ReleaseBundle {
  schema_version: 1; scope: Scope; revision: number; catalog_revision: number;
  created_at: number; expires_at: number; provider_semantics: typeof PROVIDER_SEMANTICS;
  payload_sha256: string; payload_bytes: string; approval_ref: string;
}
export interface KillState { schema_version: 1; scope: Scope; generation: number; expires_at: number; disabled: string[] }
export interface Context { assignment_key: string; authorized: boolean; eligible: boolean; expires_at: number; excluded: string[] }
export interface ControlState { bundle: ReleaseBundle | null; kill: KillState | null }
export interface DurableControlStore {
  durable: true; test_only?: boolean;
  read(scope: Scope): Promise<ControlState>;
  compareAndSwap(scope: Scope, expected: ControlState, next: ControlState): Promise<boolean>;
}
export interface ApprovalVerifier {
  verify(message: Uint8Array, approvalRef: string, purpose: "release" | "kill"): Promise<boolean>;
}
export interface AssignmentKey { scope: Scope; experiment_key: string; allocation_epoch: number; unit_key: string }
export interface Assignment { assignment_id: string; variant: string }
export interface AssignmentStore {
  durable: true;
  read(key: AssignmentKey): Promise<Assignment | null>;
  createIfAbsent(key: AssignmentKey, variant: string): Promise<Assignment>;
}
export interface Decision extends ResolvedDecision {
  decision_id: string; feature_key: string; scope: Scope; surface: string;
  status: "resolved" | "baseline" | "denied"; assignment?: Assignment; allocation_epoch?: number; preview: boolean;
}
export interface Event {
  schema_version: 1; scope: Scope; event_id: string; kind: "exposure" | "outcome";
  decision_id: string; feature_key: string; assignment_id: string; configuration_revision: number;
  allocation_epoch: number; variant: string; timestamp: number;
  evidence: { source: "render" | "behavior" | "business-transition"; unit_key: string };
}
export interface EventSink {
  durable: true;
  appendIfAbsent(event: Event): Promise<boolean>;
}
