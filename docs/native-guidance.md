# Shared adaptable-development policy delivery

The standing policy is authored once in
`.claude/skills/adaptable-development/SKILL.md` (version `1.0.0`, 276 body words).
Detailed procedures use the existing skills system:
`design-feature-adoption` and `compatibility-improvement`. Root triggers select
them before implementation; unrelated trivial tasks remain proportionate.
Product-specific design/security/domain/release instructions retain their scope.

Source → `build-plugin.py` → `plugin/manolii-framework/skills/` and
`build-registry.py` → `registry/platform/framework/skills/` →
`ai-resolve.py --native-guidance` → committed consumer carrier, skills and lock.
Private registries retain their own universe content; generic policy stays in
platform scope. Do not add this policy to retired copy-sync rings.

## Native loaders

Official documentation checked 17 October 2026 (Australia/Sydney):

| Surface | Delivery and constraints | Official source |
| --- | --- | --- |
| Claude Code Web | Real `@.claude/skills/adaptable-development/SKILL.md` import alongside existing root `CLAUDE.md` (relative to a scoped importer or the explicit canonical skill alias when applicable). Imports resolve from the importing file. This loads only the standing policy; existing unowned live AGENTS imports are preserved. Current native AGENTS support depends on version/config and defaults to CLAUDE when mixed; the explicit import covers that case and older hosts. | [Memory](https://code.claude.com/docs/en/memory), [cloud environments](https://code.claude.com/docs/en/cloud-environments) |
| Codex Web | Discovered root AGENTS; a present root override receives the same managed policy because it replaces root AGENTS for Codex. Audit nested overrides/fallbacks and configured chain limit (default 32 KiB). Claude imports are not a Codex loader. | [AGENTS discovery](https://learn.chatgpt.com/docs/agent-configuration/agents-md) |
| Cursor Web / Cloud | Supported root/nested AGENTS discovery; `.cursor/rules/adaptable-development.mdc` has `alwaysApply: true` and only selects procedures. Read referenced files explicitly. | [Rules](https://cursor.com/docs/rules) |
| Devin | Essential policy prepended to AGENTS within its first 16,384 bytes; procedures remain repository-visible. Knowledge/personal memory and plugin declarations are not shared installation evidence. | [AGENTS](https://docs.devin.ai/onboard-devin/agents-md) |

The policy skill import avoids loading a curated AGENTS catalogue into Claude
when it already has separate operating instructions. Managed legacy AGENTS imports
are migrated through their recorded block hash; unowned imports stay unchanged.
The two detailed procedures remain on demand.

No account settings, hooks, permissions, model routes or installed plugins are
changed. CLI availability does not establish access to the corresponding cloud
product. Static checks cannot inspect host-injected instructions, invocation
overrides or semantic conflicts in local guidance; inspect them during adoption.
The checker reports the configured local Codex chain and fallback filenames,
including global-file size, without printing global instruction contents.
Do not raise limits to conceal oversized guidance. A long legacy AGENTS tail
may still be truncated by Devin even though the new essential policy is early.

## Adoption and verification

Read consumer/owning-master instructions, inspect open PR ownership and current
approved intent, classify the repository and preview both ordinary resolution
and native delivery. Keep existing dependencies and product overlays unless the
policy's chosen reviewed pin specifically requires a change. Reconcile each
conflict; do not delete curated files just to clear the resolver.

Run `--native-guidance` first (read-only), then `--apply`, then `--check`.
Repeat `--apply` and verify no diff. Inspect the complete diff for local guidance
preservation, public/private boundaries, actual imports and procedure paths.
Apply in an exclusive editing window for the affected files: pause external
editors, formatters and other generators until the update finishes. Prefer an
isolated task worktree. The existing repository mutex serializes participating
resolver/mirror writers; it cannot lock an unrelated editor. Destination snapshots
are revalidated after preparing each temporary file, immediately before its atomic
rename (including recovery). This detects edits during preparation but is not an
atomic content compare-and-swap against a writer that ignores the mutex. Each file
replacement is atomic; the batch is not one atomic filesystem transaction. A
recovery conflict retains the newer file and requires manual reconciliation;
do not blindly retry it. Keep destination directories stable during the update.
Existing registry lint, builder freshness and native-guidance tests verify
distribution separately from platform behaviour. Record version, source ref,
hashes and exact checks in the owning rollout record.

Use a small fresh-session probe on each available **target cloud surface**, in a
representative repository that already has local instructions:

1. Capture platform loading evidence where available. Claude import loading can
   use the existing `InstructionsLoaded` handler/verifier; native AGENTS-only
   loading does not emit that event on current Claude. Codex/Cursor/Devin must
   use their actual supported loading/task evidence, never fabricated events.
2. Supply a deterministic design reference and ask for a small feature. Observe
   a read of `design-feature-adoption`, mapping to real app dependencies, direct
   reference comparison and complete functional verification.
3. Ask for a behaviour-preserving shared-boundary refactor with a representative
   variation. Observe a read of `compatibility-improvement`, baseline/consumer
   mapping, preserved defaults/state/in-flight operations and relevant checks.
4. Ask for an unrelated typo correction. Confirm it stays proportionate and
   does not load both full procedures unnecessarily.

Retain tool/read/render/test evidence; self-reported compliance alone is not
proof. Report **generated**, **installed**, **loaded** and **behaviour verified**
separately. If a cloud host/session cannot be exercised, mark those columns
unverified and continue deterministic distribution work. A local CLI probe
qualifies only that CLI/runtime, not the Web product.

## Update and rollback

Update authored skills, advance the policy version for semantic changes, rebuild
both generated outputs and rerun freshness/lint/tests. Pin a reviewed revision
through the existing branch/PR/release path, preview native resolution, then
apply/check/repeat it in each eligible consumer. Preserve private overlays and
scope contracts. Never hand-edit generated plugin/registry skills.

For an established private mirror that must keep its other platform versions,
preview the bounded generation with `scripts/build-registry.py --policy-only
--registry-root <mirror>/registry --check`, then omit `--check` to regenerate only
the three public policy skill files. Inspect the diff and run the mirror's
existing lint/PR gates. This does not refresh unrelated components, private
plugins, workflow pins or model routing. Use the resolver from the reviewed
pack revision when the consumer's vendored resolver predates native delivery.

Rollback by reverting the consumer's owning policy-install/update commit via
its normal PR path, including the native lock and managed blocks/files. Keep
later unrelated edits: resolve revert conflicts deliberately. Do not use full
tree restores or `--prune` for native blocks. This changes instructions only;
application code, stored data and in-flight operations need no rollback here.
Platform/account caches require a fresh session to verify restored guidance.
