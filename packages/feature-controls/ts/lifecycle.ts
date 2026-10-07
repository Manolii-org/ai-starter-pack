import { approvalMessage, payloadOf, sameScope, validate, validateCatalog } from "./validation.js";
import type { ApprovalVerifier, Catalog, ControlState, DurableControlStore, KillState, ReleaseBundle } from "./types.js";
export interface RuntimeOptions {
  controls?: DurableControlStore; verifier?: ApprovalVerifier; trust_policy?: "verified" | "local-test";
  local_disabled?: string[]; require_fresh_kills?: boolean;
}
export class Lifecycle {
  readonly catalog: Catalog;
  protected readonly options: RuntimeOptions;
  private local: ControlState = { bundle: null, kill: null };
  constructor(catalog: Catalog, options: RuntimeOptions = {}) {
    validateCatalog(catalog);
    this.catalog = structuredClone(catalog);
    this.options = { ...options, local_disabled: [...(options.local_disabled ?? [])] };
    if (options.trust_policy === "local-test" && options.controls && options.controls.test_only !== true) throw new Error("local trust requires a test-only store");
    for (const key of this.options.local_disabled!) if (!Object.hasOwn(catalog.features, key)) throw new Error("unknown local disable");
  }
  protected async state(): Promise<ControlState> {
    if (this.options.controls?.durable === true) return structuredClone(await this.options.controls.read(this.catalog.scope));
    if (this.options.trust_policy !== "local-test") throw new Error("durable control store required");
    return structuredClone(this.local);
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
    if (this.options.trust_policy === "local-test") return;
    if (!this.options.verifier || !await this.options.verifier.verify(approvalMessage(bundle), bundle.approval_ref, "release")) throw new Error("release approval required");
  }
  async activate(bundle: ReleaseBundle, expected_revision: number, now = Date.now()): Promise<void> {
    bundle = structuredClone(bundle);
    payloadOf(bundle, this.catalog);
    validateTime(now);
    if (bundle.created_at > now + 30000 || bundle.expires_at <= now || !Number.isSafeInteger(expected_revision)) throw new Error("stale bundle");
    await this.approved(bundle);
    const current = await this.state();
    const revision = current.bundle?.revision ?? 0;
    if (revision !== expected_revision || bundle.revision <= revision) throw new Error("revision conflict or replay");
    await this.swap(current, { bundle, kill: current.kill });
  }
  async updateKills(kill: KillState, approval_ref: string, now = Date.now()): Promise<void> {
    kill = structuredClone(kill);
    validate("kill", kill); validateTime(now);
    if (!sameScope(kill.scope, this.catalog.scope) || kill.expires_at <= now ||
        kill.disabled.some(k => !Object.hasOwn(this.catalog.features, k))) throw new Error("invalid kill binding");
    if (this.options.trust_policy !== "local-test" && (!this.options.verifier ||
        !await this.options.verifier.verify(killMessage(kill), approval_ref, "kill"))) throw new Error("kill approval required");
    const current = await this.state();
    if (kill.generation <= (current.kill?.generation ?? 0)) throw new Error("kill replay");
    await this.swap(current, { bundle: current.bundle, kill });
  }
}
export function validateTime(now: number): void {
  if (!Number.isSafeInteger(now) || now < 0) throw new Error("invalid clock");
}
export function killMessage(kill: KillState): Uint8Array {
  const s = kill.scope;
  return new TextEncoder().encode(JSON.stringify(["feature-controls/kill/v1", s.namespace, s.application,
    s.environment, kill.generation, kill.expires_at, kill.disabled]));
}
