import { randomUUID } from "node:crypto";
import { GrowthBook, StickyBucketServiceSync } from "@growthbook/growthbook";
import type { FeatureApiResponse, StickyAssignmentsDocument } from "@growthbook/growthbook";
import { Lifecycle, validateTime } from "./lifecycle.js";
import type { RuntimeOptions } from "./lifecycle.js";
import { ascii, matches, payloadOf, sameScope, validate } from "./validation.js";
import type { Assignment, AssignmentKey, AssignmentStore, Catalog, Context, Decision, Event, EventSink, Feature, KillState, Snapshot } from "./types.js";
class ReadOnlyStickyFixture extends StickyBucketServiceSync {
  constructor(private readonly doc: StickyAssignmentsDocument | null) { super(); }
  getAssignmentsSync(): StickyAssignmentsDocument | null { return this.doc; }
  saveAssignmentsSync(): void {}
}
export interface EvaluationOptions { preview?: boolean; now?: number }
export class FeatureRuntime extends Lifecycle {
  constructor(catalog: Catalog, options: RuntimeOptions & { assignments?: AssignmentStore; events?: EventSink } = {}) {
    super(catalog, options);
    this.assignments = options.assignments; this.events = options.events;
  }
  private readonly assignments?: AssignmentStore;
  private readonly events?: EventSink;
  private readonly issued = new Map<string, Decision>();
  private readonly exposed = new Set<string>();
  async evaluate(key: string, surface: string, context: Context, options: EvaluationOptions = {}): Promise<Decision> {
    const now = options.now ?? Date.now(); validateTime(now); ascii(key); ascii(surface);
    validate("context", context);
    const feature = Object.hasOwn(this.catalog.features, key) ? this.catalog.features[key] : undefined;
    const base: Decision = { decision_id: randomUUID(), feature_key: key, scope: structuredClone(this.catalog.scope), surface,
      value: structuredClone(feature?.baseline ?? null), reason: "unknown", status: "denied", preview: !!options.preview,
      expires_at: Math.min(context.expires_at, now + 60000), configuration_revision: null, kill_generation: 0 };
    if (!feature) return base;
    const fail = (reason: string): Decision => ({ ...base, reason, status: feature.failure === "baseline" ? "baseline" : "denied" });
    if (!context.authorized) return { ...base, reason: "unauthorized" };
    if (context.expires_at <= now) return fail("context_expired");
    if (!feature.surfaces.includes(surface)) return fail("surface_ineligible");
    let state;
    try { state = await this.state(); } catch { return fail("controls_unavailable"); }
    if (state.kill) {
      try { this.checkKill(state.kill); } catch { return fail("invalid_kills"); }
      base.kill_generation = state.kill.generation;
    }
    const disabled = new Set([...(this.options.local_disabled ?? []), ...(state.kill?.disabled ?? []), ...context.excluded]);
    if (this.excluded(key, disabled)) return fail("disabled_or_excluded");
    if (this.options.require_fresh_kills !== false && (!state.kill || state.kill.expires_at <= now)) return fail("kills_expired");
    if (!context.eligible) return fail("ineligible");
    if (!state.bundle) return fail("bundle_unavailable");
    const bundle = state.bundle;
    base.configuration_revision = bundle.revision;
    base.expires_at = Math.min(base.expires_at, bundle.expires_at, state.kill?.expires_at ?? base.expires_at);
    if (bundle.expires_at <= now || bundle.created_at > now) return fail("bundle_expired");
    let payload: Record<string, unknown>;
    try { payload = payloadOf(bundle, this.catalog); await this.approved(bundle); } catch { return fail("invalid_bundle"); }
    if (feature.experiment && this.assignments?.durable !== true && !options.preview) return fail("assignment_store_required");
    try {
      const assignmentKey: AssignmentKey | undefined = feature.experiment ? { scope: this.catalog.scope,
        experiment_key: feature.experiment.key, allocation_epoch: feature.experiment.epoch, unit_key: context.assignment_key } : undefined;
      let assignment = assignmentKey && this.assignments?.durable ? await this.assignments.read(assignmentKey) : null;
      let result = await this.provider(payload, key, context.assignment_key, feature, assignment);
      if (result.experimentResult?.inExperiment && !options.preview && assignmentKey && this.assignments) {
        assignment = await this.assignments.createIfAbsent(assignmentKey, result.experimentResult.key);
        ascii(assignment.assignment_id); ascii(assignment.variant);
        result = await this.provider(payload, key, context.assignment_key, feature, assignment);
        if (!result.experimentResult?.stickyBucketUsed || result.experimentResult.key !== assignment.variant) return fail("assignment_conflict");
      }
      const value: unknown = result.value;
      if (!matches(value, feature)) return fail("invalid_value");
      const decision: Decision = { ...base, value, status: "resolved",
        reason: options.preview ? "preview" : result.experimentResult?.inExperiment ? "experiment" : "released" };
      if (assignment && result.experimentResult?.inExperiment && !options.preview) {
        decision.assignment = assignment; decision.allocation_epoch = feature.experiment!.epoch;
        if (this.issued.size >= 1000) for (const [id, d] of this.issued) if (d.expires_at <= now) { this.issued.delete(id); this.exposed.delete(id); }
        if (this.issued.size >= 1000) return fail("decision_capacity");
        this.issued.set(decision.decision_id, structuredClone(decision));
      }
      return decision;
    } catch { return fail("provider_or_assignment_unavailable"); }
  }
  private checkKill(kill: KillState): void {
    validate("kill", kill);
    if (!sameScope(kill.scope, this.catalog.scope)) throw new Error("kill scope mismatch");
  }
  private excluded(key: string, excluded: Set<string>): boolean {
    return excluded.has(key) || this.catalog.features[key].ancestors.some(a => this.excluded(a, excluded));
  }
  private async provider(payload: Record<string, unknown>, key: string, unit: string, feature: Feature, assignment: Assignment | null) {
    const doc: StickyAssignmentsDocument | null = assignment && feature.experiment ? {
      attributeName: "id", attributeValue: unit,
      assignments: { [`${feature.experiment.key}__${feature.experiment.epoch}`]: assignment.variant }
    } : null;
    const gb = new GrowthBook({ attributes: { id: unit }, stickyBucketService: feature.experiment ? new ReadOnlyStickyFixture(doc) : undefined });
    try { await gb.setPayload(structuredClone(payload) as FeatureApiResponse); return gb.evalFeature(key); }
    finally { gb.destroy(); }
  }
  async snapshot(keys: string[], surface: string, context: Context, options: EvaluationOptions = {}): Promise<Snapshot> {
    if (keys.length > 1000) throw new Error("snapshot too large");
    const now = options.now ?? Date.now();
    const decisions = Object.create(null) as Snapshot["decisions"];
    let expires = Math.min(context.expires_at, now + 60000);
    for (const key of keys) {
      const d = await this.evaluate(key, surface, context, { ...options, now });
      expires = Math.min(expires, d.expires_at);
      decisions[key] = { value: d.value, reason: d.reason, expires_at: d.expires_at,
        configuration_revision: d.configuration_revision, kill_generation: d.kill_generation };
    }
    for (const d of Object.values(decisions)) d.expires_at = Math.min(d.expires_at, expires);
    return { schema_version: 1, application: this.catalog.scope.application, environment: this.catalog.scope.environment,
      surface, generated_at: now, expires_at: expires, decisions };
  }
  async recordEvent(event: Event, now = Date.now()): Promise<boolean> {
    validate("event", event); validateTime(now);
    if (this.events?.durable !== true) throw new Error("durable event sink required");
    const d = this.issued.get(event.decision_id);
    if (!d || d.preview || d.reason !== "experiment" || !d.assignment || d.expires_at <= now ||
        !sameScope(event.scope, d.scope) || event.timestamp > now || event.timestamp < now - 60000 ||
        event.feature_key !== d.feature_key || event.assignment_id !== d.assignment.assignment_id ||
        event.variant !== d.assignment.variant || event.configuration_revision !== d.configuration_revision ||
        event.allocation_epoch !== d.allocation_epoch ||
        (event.kind === "outcome") !== (event.evidence.source === "business-transition") ||
        (event.kind === "outcome" && !this.exposed.has(event.decision_id))) throw new Error("event attribution mismatch");
    const added = await this.events.appendIfAbsent(structuredClone(event));
    if (event.kind === "exposure") this.exposed.add(event.decision_id);
    return added;
  }
}
