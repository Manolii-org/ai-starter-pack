---
name: compatibility-improvement
version: 1.0.1
scope: platform
author: AI Starter Pack contributors
license: Apache-2.0
description: Read before structural improvement or refactoring of an existing application; preserve consumers, defaults, stored state and in-flight activity.
type: skill
safety_tier: green
requires_mcp: []
required_entities: []
tags: [engineering, design, testing]
---

# Existing-application improvement

Use before existing-app refactoring or structural improvement. Keep trivial unrelated edits proportionate. Apply the design-feature-adoption procedure as well when approved visuals or intentional features change.

1. Establish the actual behaviour and architecture baseline: critical journeys, observable outputs, errors/fallbacks, performance where affected, test health, running versions and known failures. Record reproducible checks before editing. Inspect existing work/open PRs and ownership.
2. Map affected consumers and boundaries: imports/callers, APIs/events, stable names and identifiers, data contracts, defaults and configuration precedence, product/tenant/owner scopes, persisted and long-lived state, queues/jobs, sessions, integrations and in-flight operations. Find consumers outside the immediate repository where relevant.
3. Prioritise by concrete benefit, risk, reversibility and confidence. Prefer the smallest boundary that keeps a likely change local. Share only equivalent meaning/behaviour; preserve genuine domain differences. Justify packages or generic infrastructure with concrete reuse and lifecycle needs.
4. Make small, reversible, behaviour-preserving steps. Keep structural changes separate from intentional behaviour changes, including changes to default values, errors, fallbacks, provider choice and outputs. Preserve existing stable identifiers and contracts. Do not bundle dependency/model/permission upgrades without task scope.
5. Verify each affected contract against the baseline with targeted compatibility and functional checks, visual comparison where UI changes, and performance checks where relevant. Exercise representative downstream consumers and one meaningful configuration/theme/domain/provider variation for a material shared-boundary change. Use existing tools and record baseline failures separately from new regressions.
6. Plan safe release sequencing: compatibility between old/new code, readers/writers and workers; additive migrations where applicable; retries and idempotency; resumable work; feature/config defaults; monitoring and rollback criteria. Rollback must preserve data and activity created during the change. A destructive restore that discards new activity is not a rollback plan. Validate recovery with proportionate existing tooling.
7. Follow the repository's authorised integration/promotion rules. Hold only the affected boundary when a dependency or decision blocks it; complete independent work. Do not bypass review, guarded paths or deployment controls.

Leave one concise record in the established task/PR location: baseline and affected consumers; stable contracts; steps and customisation paths; compatibility/visual/functional/performance/variation evidence; baseline failures and unavailable checks; intentional changes approved separately; release sequence, rollback/update instructions, limitations and deferred work.
