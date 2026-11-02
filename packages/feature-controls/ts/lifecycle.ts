import { createHash } from "node:crypto";
import { approvalMessage, payloadOf, compatibleReleaseManifest, releaseCatalogFeatures, sameScope, validate, validateCatalog } from "./validation.js";
import type { ApprovalVerifier, Catalog, ControlState, DurableControlStore, KillState, ReleaseBundle, ReleaseManifest } from "./types.js";
export interface RuntimeOptions {
  controls?: DurableControlStore; verifier?: ApprovalVerifier; trust_policy?: "verified" | "local-test";
  local_disabled?: string[]; require_fresh_kills?: boolean;
  release_manifest?: ReleaseManifest; release_source_sha?: string;
}
export class Lifecycle {
  readonly #catalog: Catalog;
  get catalog(): Catalog { return this.#catalog; }
  protected readonly options: RuntimeOptions;
  private local: ControlState = { bundle: null, kill: null, time_highwater: 0 };
  constructor(catalog: Catalog, options: RuntimeOptions = {}) {
    catalog = structuredClone(catalog);
    validateCatalog(catalog);
    this.#catalog = freezeCatalog(catalog);
    this.options = { ...options, local_disabled: [...(options.local_disabled ?? [])] };
    if (options.release_manifest) {
      const manifest = structuredClone(options.release_manifest);
      const definitions = releaseCatalogFeatures(manifest);
      if (Object.keys(definitions).length && !/^[0-9a-f]{40}$/.test(options.release_source_sha ?? "")) throw new Error("release source binding required");
      for (const [key, expected] of Object.entries(definitions)) {
        if (JSON.stringify(catalog.features[key]) === undefined ||
            Object.entries(expected).some(([field, value]) => JSON.stringify((catalog.features[key] as unknown as Record<string, unknown>)[field]) !== JSON.stringify(value)) ||
            Object.keys(catalog.features[key]).length !== Object.keys(expected).length) throw new Error("release catalogue mismatch");
        if (manifest.features[key].state === "development") this.options.local_disabled!.push(key);
      }
      this.options.release_manifest = manifest;
    }
    if (options.trust_policy === "local-test" && options.controls && options.controls.test_only !== true) throw new Error("local trust requires a test-only store");
    for (const key of this.options.local_disabled!) if (!Object.hasOwn(catalog.features, key)) throw new Error("unknown local disable");
  }
  protected async state(): Promise<ControlState> {
    if (this.options.controls?.durable === true) {
      const state = structuredClone(await this.options.controls.read(this.catalog.scope));
      validate("control_state", state);
      if (state.bundle && !sameScope(state.bundle.scope, this.catalog.scope) || state.kill && !sameScope(state.kill.scope, this.catalog.scope)) throw new Error("control scope mismatch");
      return state;
    }
    if (this.options.trust_policy !== "local-test") throw new Error("durable control store required");
    return structuredClone(this.local);
  }
  protected async observedState(now: number, preview = false): Promise<ControlState> {
    for (let attempt = 0; attempt < 4; attempt++) {
      const state = await this.state();
      validateTime(state.time_highwater);
      if (now < state.time_highwater) throw new Error("clock rollback");
      if (preview || now === state.time_highwater) return state;
      const next = { ...state, time_highwater: now };
      if (this.options.controls?.durable === true) {
        if (!await this.options.controls.compareAndSwap(this.catalog.scope, state, next)) continue;
      } else await this.swap(state, next);
      return next;
    }
    throw new Error("clock fence conflict");
  }
  private async swap(current: ControlState, next: ControlState): Promise<void> {
    if (this.options.controls?.durable === true) {
      if (!await this.options.controls.compareAndSwap(this.catalog.scope, current, next)) throw new Error("activation conflict");
    } else {
      if (JSON.stringify(this.local) !== JSON.stringify(current)) throw new Error("activation conflict");
      this.local = structuredClone(next);
    }
  }
  protected async approved(bundle: ReleaseBundle): Promise<void> {
    if (this.options.release_manifest && Object.keys(this.options.release_manifest.features).length &&
        (!bundle.deployment || !compatibleReleaseManifest(this.options.release_manifest, bundle.deployment.manifest))) {
      throw new Error("release does not match deployed source policy");
    }
    if (this.options.trust_policy === "local-test") return;
    if (!this.options.verifier || !await this.options.verifier.verify(approvalMessage(bundle), bundle.approval_ref, "release")) throw new Error("release approval required");
  }
  private async storedKillApproved(kill: KillState): Promise<void> {
    if (this.options.trust_policy === "local-test") return;
    if (!this.options.verifier?.verifyKill || await this.options.verifier.verifyKill(
      structuredClone(kill), structuredClone(this.catalog.scope)) !== true) throw new Error("stored kill approval required");
  }
  /** Private owning-process evidence. Never return this state to browsers. */
  async inspectForRelease(now?: number): Promise<ControlState> {
    if (now !== undefined) validateTime(now);
    const state = await this.state();
    if (!state.bundle || !state.kill) throw new Error("release controls unavailable or stale");
    payloadOf(state.bundle, this.catalog); killMessage(state.kill);
    await this.approved(state.bundle);
    await this.storedKillApproved(state.kill);
    if (now === undefined) now = Date.now();
    validateTime(now);
    if (now < state.time_highwater || state.bundle.created_at > now ||
        state.bundle.expires_at <= now || state.kill.expires_at <= now) throw new Error("release controls unavailable or stale");
    return structuredClone(state);
  }
  /** Publisher-only prior approval check, before introducing a new catalogue. */
  async previousForDeployment(now?: number): Promise<ControlState> {
    if (now !== undefined) validateTime(now);
    const state = await this.state(), bundle = state.bundle;
    if (!bundle || !state.kill) throw new Error("previous controls unavailable");
    validate("bundle", bundle); killMessage(state.kill);
    if (Buffer.byteLength(bundle.payload_bytes, "utf8") > 1048576 ||
        createHash("sha256").update(bundle.payload_bytes, "utf8").digest("hex") !== bundle.payload_sha256) throw new Error("invalid previous release");
    if (this.options.trust_policy !== "local-test" && (!this.options.verifier ||
        !await this.options.verifier.verify(approvalMessage(bundle), bundle.approval_ref, "release"))) throw new Error("previous approval required");
    await this.storedKillApproved(state.kill);
    if (now === undefined) now = Date.now();
    validateTime(now);
    if (now < state.time_highwater || bundle.created_at > now || bundle.expires_at <= now || state.kill.expires_at <= now) throw new Error("previous controls stale");
    return structuredClone(state);
  }
  async activate(bundle: ReleaseBundle, expected_revision: number, now?: number): Promise<void> {
    bundle = structuredClone(bundle);
    payloadOf(bundle, this.catalog);
    if (now !== undefined) validateTime(now);
    if (!Number.isSafeInteger(expected_revision) || now !== undefined &&
        (bundle.created_at > now + 30000 || bundle.expires_at <= now)) throw new Error("stale bundle");
    await this.approved(bundle);
    const current = await this.state();
    // Approval and store I/O can overlap observations that advance the durable floor.
    if (now === undefined) now = Date.now();
    validateTime(now);
    if (bundle.created_at > now + 30000 || bundle.expires_at <= now) throw new Error("stale bundle");
    if (now < current.time_highwater) throw new Error("clock rollback");
    const revision = current.bundle?.revision ?? 0;
    if (revision !== expected_revision || bundle.revision <= revision) throw new Error("revision conflict or replay");
    await this.swap(current, { bundle, kill: current.kill, time_highwater: now });
  }
  async updateKills(kill: KillState, approval_ref: string, now?: number): Promise<void> {
    kill = structuredClone(kill);
    const message = killMessage(kill);
    if (now !== undefined) validateTime(now);
    if (!sameScope(kill.scope, this.catalog.scope) || now !== undefined && kill.expires_at <= now ||
        kill.disabled.some(k => !Object.hasOwn(this.catalog.features, k))) throw new Error("invalid kill binding");
    if (this.options.trust_policy !== "local-test" && (!this.options.verifier ||
        !await this.options.verifier.verify(message, approval_ref, "kill"))) throw new Error("kill approval required");
    const current = await this.state();
    if (now === undefined) now = Date.now();
    validateTime(now);
    if (kill.expires_at <= now) throw new Error("invalid kill binding");
    if (now < current.time_highwater) throw new Error("clock rollback");
    if (kill.generation <= (current.kill?.generation ?? 0)) throw new Error("kill replay");
    await this.swap(current, { bundle: current.bundle, kill, time_highwater: now });
  }
}
export function validateTime(now: number): void {
  if (!Number.isSafeInteger(now) || now < 0) throw new Error("invalid clock");
}
export function killMessage(kill: KillState): Uint8Array {
  validate("kill", kill);
  if (Object.keys(kill.exceptions ?? {}).some(key => !kill.disabled.includes(key))) throw new Error("exception without disable");
  const s = kill.scope;
  const message: unknown[] = [kill.exceptions === undefined ? "feature-controls/kill/v1" : "feature-controls/kill/v2", s.ecosystem_id, s.deployment_id,
    s.environment_id, s.feature_namespace, kill.generation, kill.expires_at, kill.disabled];
  if (kill.exceptions !== undefined) message.push(Object.keys(kill.exceptions).sort().map(key => {
    const exception = kill.exceptions![key];
    if (!exception.reason.trim()) throw new Error("empty disable reason");
    return [key, exception.owner, exception.reason, exception.review_at];
  }));
  return new TextEncoder().encode(JSON.stringify(message));
}

function freezeCatalog(catalog: Catalog): Catalog {
  const freeze = (value: unknown): void => {
    if (value && typeof value === "object") {
      for (const child of Object.values(value)) freeze(child);
      Object.freeze(value);
    }
  };
  freeze(catalog); return catalog;
}
