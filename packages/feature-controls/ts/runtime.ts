import { randomUUID } from "node:crypto";
import { GrowthBook, StickyBucketServiceSync } from "@growthbook/growthbook";
import type { FeatureApiResponse, StickyAssignmentsDocument } from "@growthbook/growthbook";
import { Lifecycle, validateTime } from "./lifecycle.js";
import type { RuntimeOptions } from "./lifecycle.js";
import { allocationId, ascii, attributesOf, boundaryOf, matches, ownJson, payloadOf, sameScope, validate } from "./validation.js";
import type { Assignment, AssignmentKey, AssignmentStore, Catalog, Context, ControlState, Decision, Event, EventSink, Feature, KillState, Snapshot } from "./types.js";
class ReadOnlyStickyFixture extends StickyBucketServiceSync {
  constructor(private readonly doc: StickyAssignmentsDocument | null) { super(); }
  getAssignmentsSync(): StickyAssignmentsDocument | null { return this.doc; }
  saveAssignmentsSync(): void {}
}
export interface EvaluationOptions { preview?: boolean; now?: number }
interface Capture { state: ControlState; payload?: Record<string, unknown>; invalid_bundle?: boolean; unavailable?: boolean }
export class FeatureRuntime extends Lifecycle {
  constructor(catalog: Catalog, options: RuntimeOptions & { assignments?: AssignmentStore; events?: EventSink } = {}) {
    super(catalog, options);
    if (options.events && (options.trust_policy !== "local-test" || options.events.test_only !== true)) throw new Error("measurement is local-test only");
    this.assignments = options.assignments; this.events = options.events;
  }
  private readonly assignments?: AssignmentStore;
  private readonly events?: EventSink;
  private readonly issued = new Map<string, Decision>();
  private readonly exposed = new Set<string>();
  async evaluate(key: string, surface_id: string, context: Context, options: EvaluationOptions = {}): Promise<Decision> {
    return this.evaluateCaptured(key, surface_id, structuredClone(context), { ...options });
  }
  private async evaluateCaptured(key: string, surface_id: string, context: Context, options: EvaluationOptions, capture?: Capture): Promise<Decision> {
    const now = options.now ?? Date.now(); validateTime(now); ascii(key); ascii(surface_id);
    validate("context", context);
    const attributes = attributesOf(context);
    const feature = Object.hasOwn(this.catalog.features, key) ? this.catalog.features[key] : undefined;
    const base: Decision = { decision_id: randomUUID(), feature_key: key, scope: structuredClone(this.catalog.scope), application_id: context.application_id, context_scope: context.context_scope, surface_id,
      value: structuredClone(feature?.disabled_value ?? null), reason: "unknown", status: "denied", preview: !!options.preview,
      expires_at: Math.min(context.expires_at, now + 60000), configuration_revision: null, kill_generation: 0 };
    if (!feature) return base;
    const fail = (reason: string): Decision => ({ ...base, reason,
      value: structuredClone(feature.failure === "baseline" ? feature.baseline : feature.disabled_value),
      status: feature.failure === "baseline" ? "baseline" : "denied" });
    if (!sameScope(context.scope, this.catalog.scope) || context.surface_id !== surface_id) return { ...base, reason: "context_binding_mismatch" };
    if (!context.authorized) return { ...base, reason: "unauthorized" };
    if (!(feature.applications ?? this.catalog.applications).includes(context.application_id)) return { ...base, reason: "application_ineligible" };
    if (context.excluded_groups.some(k => attributes.groups.includes(k))) return { ...base, reason: "group_excluded" };
    if (context.expires_at <= now) return { ...base, reason: "context_expired" };
    if (!feature.surfaces.includes(surface_id)) return { ...base, reason: "surface_ineligible" };
    if (this.excluded(key, new Set([...(this.options.local_disabled ?? []), ...context.excluded]))) return { ...base, reason: "disabled_or_excluded" };
    let state;
    if (capture?.unavailable) return { ...base, reason: "controls_unavailable" };
    try { state = capture?.state ?? await this.observedState(now, !!options.preview); } catch { return { ...base, reason: "controls_unavailable" }; }
    if (state.kill) {
      try { this.checkKill(state.kill); } catch { return { ...base, reason: "invalid_kills" }; }
      base.kill_generation = state.kill.generation;
    }
    const disabled = new Set([...(this.options.local_disabled ?? []), ...(state.kill?.disabled ?? []), ...context.excluded]);
    if (this.excluded(key, disabled)) return { ...base, reason: "disabled_or_excluded" };
    if (this.options.require_fresh_kills !== false && (!state.kill || state.kill.expires_at <= now)) return { ...base, reason: "kills_expired" };
    if (!context.eligible) return { ...base, reason: "ineligible" };
    if (!state.bundle) return fail("bundle_unavailable");
    const bundle = state.bundle;
    base.configuration_revision = bundle.revision;
    base.expires_at = Math.min(base.expires_at, bundle.expires_at, state.kill?.expires_at ?? base.expires_at);
    if (bundle.expires_at <= now || bundle.created_at > now) return fail("bundle_expired");
    let payload: Record<string, unknown>;
    if (capture?.invalid_bundle) return fail("invalid_bundle");
    try {
      if (capture?.payload) payload = capture.payload;
      else { payload = payloadOf(bundle, this.catalog); await this.approved(bundle); }
    } catch { return fail("invalid_bundle"); }
    if (feature.experiment && this.assignments?.durable !== true && !options.preview) return fail("assignment_store_required");
    try {
      const assignment_boundary = boundaryOf(feature, context);
      attributes.id = allocationId(context, assignment_boundary);
      const assignmentKey: AssignmentKey | undefined = feature.experiment ? { scope: this.catalog.scope, assignment_boundary,
        experiment_key: feature.experiment.key, allocation_epoch: feature.experiment.epoch, unit_key: context.assignment_key } : undefined;
      let assignment = assignmentKey && this.assignments?.durable ? await this.assignments.read(structuredClone(assignmentKey)) : null;
      let result = await this.provider(payload, key, attributes, feature, assignment);
      if (result.experimentResult?.inExperiment && !options.preview && assignmentKey && this.assignments) {
        assignment = await this.assignments.createIfAbsent(structuredClone(assignmentKey), result.experimentResult.key);
        ascii(assignment.assignment_id); ascii(assignment.variant);
        result = await this.provider(payload, key, attributes, feature, assignment);
        if (!result.experimentResult?.stickyBucketUsed || result.experimentResult.key !== assignment.variant) return fail("assignment_conflict");
      }
      const value: unknown = result.value;
      if (!matches(value, feature)) return fail("invalid_value");
      const decision: Decision = { ...base, value, status: "resolved",
        reason: options.preview ? "preview" : result.experimentResult?.inExperiment ? "experiment" : "released" };
      if (assignment && result.experimentResult?.inExperiment && !options.preview) {
        decision.assignment = assignment; decision.allocation_epoch = feature.experiment!.epoch;
        decision.unit_key = context.assignment_key;
      }
      if (decision.assignment && this.options.trust_policy === "local-test") {
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
  private async provider(payload: Record<string, unknown>, key: string, attributes: ReturnType<typeof attributesOf>, feature: Feature, assignment: Assignment | null) {
    const doc: StickyAssignmentsDocument | null = assignment && feature.experiment ? {
      attributeName: "id", attributeValue: attributes.id,
      assignments: { [`${feature.experiment.key}__${feature.experiment.epoch}`]: assignment.variant }
    } : null;
    const gb = new GrowthBook({ attributes: structuredClone(attributes), stickyBucketService: feature.experiment ? new ReadOnlyStickyFixture(doc) : undefined });
    try { await gb.setPayload(ownJson(JSON.stringify(payload)) as FeatureApiResponse); return gb.evalFeature(key); }
    finally { gb.destroy(); }
  }
  async snapshot(keys: string[], surface_id: string, context: Context, options: EvaluationOptions = {}): Promise<Snapshot> {
    keys = [...keys]; context = structuredClone(context); options = { ...options };
    if (keys.length > 64) throw new Error("snapshot too large");
    validate("context", context); ascii(surface_id);
    const now = options.now ?? Date.now();
    const capture: Capture = { state: { bundle: null, kill: null, time_highwater: 0 } };
    validateTime(now);
    try { capture.state = await this.observedState(now, !!options.preview); } catch { capture.unavailable = true; }
    if (capture.state.bundle && !capture.unavailable) {
      try { capture.payload = payloadOf(capture.state.bundle, this.catalog); await this.approved(capture.state.bundle); }
      catch { capture.invalid_bundle = true; }
    }
    const decisions = Object.create(null) as Snapshot["decisions"];
    let expires = Math.min(context.expires_at, now + 60000);
    for (const key of keys) {
      const d = await this.evaluateCaptured(key, surface_id, context, { ...options, now }, capture);
      expires = Math.min(expires, d.expires_at);
      decisions[key] = { value: d.value, reason: d.reason, status: d.status, expires_at: d.expires_at,
        configuration_revision: capture.state.bundle?.revision ?? null, kill_generation: capture.state.kill?.generation ?? 0 };
    }
    for (const d of Object.values(decisions)) d.expires_at = Math.min(d.expires_at, expires);
    return { schema_version: 1, ...this.catalog.scope, application_id: context.application_id, context_scope: context.context_scope,
      configuration_revision: capture.state.bundle?.revision ?? 0, kill_generation: capture.state.kill?.generation ?? 0,
      time_highwater: capture.state.time_highwater,
      surface_id, generated_at: now, expires_at: expires, decisions };
  }
  async recordEvent(event: Event, now = Date.now()): Promise<boolean> {
    event = structuredClone(event);
    if (this.options.trust_policy !== "local-test" || this.events?.test_only !== true) throw new Error("measurement is local-test only");
    validate("event", event); validateTime(now);
    if (this.events?.durable !== true) throw new Error("durable event sink required");
    const d = this.issued.get(event.decision_id);
    if (!d || d.preview || d.reason !== "experiment" || !d.assignment || d.expires_at <= now ||
        !sameScope(event.scope, d.scope) || event.timestamp > now || event.timestamp < now - 60000 ||
        event.application_id !== d.application_id || event.surface_id !== d.surface_id || event.context_scope !== d.context_scope ||
        event.feature_key !== d.feature_key || event.assignment_id !== d.assignment.assignment_id ||
        event.variant !== d.assignment.variant || event.configuration_revision !== d.configuration_revision ||
        event.evidence.unit_key !== d.unit_key ||
        event.allocation_epoch !== d.allocation_epoch ||
        (event.kind === "outcome") !== (event.evidence.source === "business-transition") ||
        (event.kind === "outcome" && (!this.exposed.has(event.decision_id) || event.event_id !== event.evidence.transition_key))) throw new Error("event attribution mismatch");
    const added = await this.events.appendIfAbsent(structuredClone(event));
    if (event.kind === "exposure") this.exposed.add(event.decision_id);
    return added;
  }
}
