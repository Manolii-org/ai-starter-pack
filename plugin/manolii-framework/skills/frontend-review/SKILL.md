---
name: frontend-review
version: 1.0.0
description: "Audit React/Next.js/Tailwind UI code against accessibility (WCAG), performance, and composition rules (Vercel-style) — for Next.js/Tailwind front-ends."
type: skill
when_to_use: "Use when reviewing or writing React/Next.js/Tailwind UI — component design, accessibility, render performance, or when the user asks for a frontend/a11y/UX review of a diff."
paths: ["**/*.tsx", "**/*.jsx"]
requires_mcp: []
required_entities: []
safety_tier: green
allowed-tools:
  - Read
  - Grep
  - Glob
tags:
  - frontend
  - review
  - accessibility
eval_cases: null
supersedes: []
deprecation: null
---

# frontend-review

Green-tier read-only review skill for React/Next.js/Tailwind UI code. Scans diffs and changed files against accessibility (WCAG 2.1 AA), performance (render efficiency, bundle), and composition patterns (compound components, state colocations).

## Rule categories

**Accessibility** — WCAG 2.1 AA compliance, semantic HTML, keyboard navigation, focus management, ARIA sparingly. Rules: semantic landmarks (nav, main, aside), form labels (linked via htmlFor), img alt text (decorative: empty string), button/link keyboard access, color contrast ≥4.5:1, focus outline never hidden, ARIA only when semantic HTML cannot express structure.

**React/Next performance** — render efficiency, bundle size, server vs client boundaries. Rules: add "use client" only for interactivity (state, effects, handlers, refs, browser APIs) — not for size; memoize only after measurement, stable keys in lists, avoid prop drilling across 3+ levels, dynamic import for 50KB+ libraries, Next/Image for all product images, no inline function definitions in event handlers.

**Composition** — component design patterns, state management, reusability. Rules: compound components over boolean-prop explosion, prefer composition to configuration objects, colocate state with its consumers, uncontrolled components by default (controlled only with clear rationale), extract slot-based layout patterns to primitive layout components.

## Workflow

1. Scan changed .tsx/.jsx files via Glob/Grep
2. Apply rules against component structure, attributes, imports, event handlers
3. Report findings as a table: `file:line | category | rule | fix`
4. Link to reference.md for detailed rule rationale

For exploratory reviews, read the component and cross-check against the Accessibility, React/Next, and Composition sections of reference.md.

## Detailed rule list

See **reference.md** for the full numbered rule set with code examples, rationales, and before/after patterns. Use this SKILL.md as a quick dispatcher; reference.md is your detailed audit rubric.
