---
name: design-feature-adoption
version: 1.0.0
scope: platform
author: AI Starter Pack contributors
license: Apache-2.0
description: Read before implementing a design handoff, new feature or substantial redesign; preserve the approved visual contract and verify complete functionality.
type: skill
safety_tier: green
---

# Design and feature adoption

Use this procedure for supplied designs, new features and substantial redesigns. Scale the assessment to impact; a trivial unrelated edit does not need it. Work within existing product instructions, security boundaries and release rules.

1. Reconcile the current approved source, version and scope with the request and repository. Check newer decisions, existing work, open PRs and ownership. Resolve conflicting approved intent before dependent implementation. Record missing inputs as limitations; do independent authorised work.
2. Inventory supplied screens, exact assets and fonts, content, reference viewports, responsive relationships and interaction states (loading, empty, error, validation, success, permissions, keyboard/focus and populated data). Mark inferred states explicitly.
3. Inspect actual application entry points, components, tokens, routes, contracts and consumers. Make a compact map: `requirement/reference → implementation → shared/variable boundary → acceptance check`. Include permissions, business rules, persistence, integrations and final outputs. Identify the real end-to-end journey behind each visible control.
4. Reuse components only where meaning, behaviour and appearance fit. Implement focused variants where required to match approved typography, geometry, spacing, colours, assets and content. Put presentation in scoped themes/tokens, wording/assets/prompts in content/templates, supported choices in validated configuration, business differences in domain modules and providers behind adapters. Preserve default/precedence and isolation contracts. Avoid speculative engines or packages.
5. Implement complete functionality: real input validation, authorization, business rules, durable writes, failure/retry behaviour and outputs. A visually complete shell or disconnected handler does not satisfy the feature. Preserve existing fallback/error behaviour unless an intentional change is approved separately.
6. Compare deterministically **directly against supplied references** before accepting implementation-generated regression baselines. Use existing screenshot/diff tools; fix viewport, device scale, browser/font versions, test data, clocks and animation state. Capture each supplied state, inspect overlays/diffs and explain material differences. Check responsive transitions as well as reference sizes. Do not silently substitute assets, restyle, inflate thresholds or update snapshots to conceal differences. If reference assets or reliable rendering are unavailable, mark visual acceptance unverified.
7. Check functional journeys and accessibility alongside visuals: validation, permissions, navigation, keyboard/focus, semantic names, errors, persistence/reload and outputs. Test affected shared consumers and one relevant variation when shared boundaries materially change. Use proportionate existing tooling; do not create an expensive always-on suite for this procedure.
8. Handle compelling exceptions narrowly: identify the exact reference/state, evidence of the constraint (for example an accessibility or platform requirement), alternatives and smallest departure. Resolve the conflict explicitly with the approved experience before accepting that departure; do not silently replace approved intent.

Leave a concise completion record in the task's established record or PR: approved source/version; requirement map; customisation paths; tested states/viewports/journeys/variation; evidence locations and differences; resolved exceptions; limitations and deferred work; permitted release stage. Distinguish generated/installed guidance, observed loading and verified behaviour.
