import type { Json, ResolvedDecision } from "./client.js";
export type { Json, Snapshot, SnapshotScope, SnapshotWatermark, ResolvedDecision } from "./client.js";
export const PROVIDER_SEMANTICS = "growthbook:js-1.8.0:py-3.2.0:hash-2";
export interface Scope { ecosystem_id: string; deployment_id: string; feature_namespace: string; environment_id: string }
export interface AssignmentBoundary { mode: "application" | "shared"; key: string }
export interface Feature {
  value_type: "boolean" | "number" | "string" | "json"; baseline: Json; disabled_value: Json;
  allowed_values?: Json[]; value_schema?: Record<string, unknown>;
  failure: "baseline" | "deny"; surfaces: string[]; ancestors: string[];
  applications?: string[];
  experiment?: { key: string; epoch: number; assignment_boundary?: { key: string; applications: string[] } };
}
export interface Catalog { schema_version: 1; scope: Scope; applications: string[]; revision: number; features: Record<string, Feature> }
export interface ReleaseBundle {
  schema_version: 1; scope: Scope; revision: number; catalog_revision: number;
  created_at: number; expires_at: number; provider_semantics: typeof PROVIDER_SEMANTICS;
  payload_sha256: string; payload_bytes: string; approval_ref: string;
}
export interface KillState { schema_version: 1; scope: Scope; generation: number; expires_at: number; disabled: string[] }
export interface Context {
  schema_version: 1; scope: Scope; application_id: string; surface_id: string; projection_source: "trusted-server"; context_scope: string;
  assignment_key: string; authorized: boolean; eligible: boolean; expires_at: number; excluded: string[];
  groups: string[]; group_ancestors: Record<string, string[]>; excluded_groups: string[];
  roles: string[]; tenant_key: string;
}
export interface ControlState { time_highwater: number; bundle: ReleaseBundle | null; kill: KillState | null }
export interface DurableControlStore {
  durable: true; test_only?: boolean;
  read(scope: Scope): Promise<ControlState>;
  compareAndSwap(scope: Scope, expected: ControlState, next: ControlState): Promise<boolean>;
}
export interface ApprovalVerifier {
  verify(message: Uint8Array, approvalRef: string, purpose: "release" | "kill"): Promise<boolean>;
}
export interface AssignmentKey { scope: Scope; assignment_boundary: AssignmentBoundary; experiment_key: string; allocation_epoch: number; unit_key: string }
export interface Assignment { assignment_id: string; variant: string }
export interface AssignmentStore {
  durable: true;
  read(key: AssignmentKey): Promise<Assignment | null>;
  createIfAbsent(key: AssignmentKey, variant: string): Promise<Assignment>;
}
export interface Decision extends ResolvedDecision {
  decision_id: string; feature_key: string; scope: Scope; application_id: string; surface_id: string; context_scope: string;
  assignment?: Assignment; allocation_epoch?: number; preview: boolean; unit_key?: string;
}
export interface Event {
  schema_version: 1; scope: Scope; application_id: string; surface_id: string; context_scope: string; event_id: string; kind: "exposure" | "outcome";
  decision_id: string; feature_key: string; assignment_id: string; configuration_revision: number;
  allocation_epoch: number; variant: string; timestamp: number;
  evidence: { source: "render" | "behavior" | "business-transition"; unit_key: string; transition_key?: string };
}
export interface EventSink {
  durable: true; test_only: true;
  appendIfAbsent(event: Event): Promise<boolean>;
}
