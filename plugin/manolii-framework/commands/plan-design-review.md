---
name: plan-design-review
version: 1.0.0
description: UI/UX review of a plan — haiku checklist; auto-skips on backend-only changes
type: command
requires_mcp: []
required_entities: []
safety_tier: green
tags: ['plan-review', 'design', 'ux']
blast_radius: low
---

# /plan-design-review — design / UX plan review

Based on proven patterns from the Manolii ecosystem.

Lightweight haiku-driven design checklist for a plan that touches UI. Skipped automatically when no UI surface is affected.

## Auto-skip detection

Before running, the command checks if the plan affects design surface:
- File extensions: `.tsx`, `.jsx`, `.html`, `.css`, `.scss`, `.svelte`, `.vue`
- Paths matching: `**/components/**`, `**/pages/**`, `**/app/**`, `**/views/**`, `**/templates/**`
- Design token or Tailwind config files

If none match, output: `No design surface affected — skipping.` and exit.

## Checklist (when triggered)

Single haiku call (~1.5k tokens). Reads the plan + any mockup/wireframe references and emits findings under:

- **Design system match:** does the change inherit from the project's design tokens (`.ai/design-systems/<product>.json`)? Are Tailwind classes consistent with existing patterns?
- **Accessibility hard constraints:** WCAG AA contrast on new colour combinations? Focus rings? Keyboard navigation? Reduced-motion respected?
- **Mobile-first:** layout proven at 320px? Touch-target minimum 44×44px?
- **Consistency:** matches existing component library patterns? Or is this a one-off that needs a library entry?
- **Copy:** plain language, correct voice register for the product?

Output: markdown findings to `.ai/sprints/<sprint-id>/plan-design-review.md` (or `.ai/reviews/design-<topic>-<date>.md` outside a sprint).

## Implementation notes for Claude

1. Detect product slug from plan paths or `--product` flag.
2. Load `.ai/design-systems/<slug>.json` if exists; pass tokens to the haiku prompt.
3. Dispatch generate(haiku) with: tokens + plan content.
4. Write findings file.

## See also
- `/design-shotgun` — generate mockup variants
- `/design-html` — convert chosen mockup to product components
- `/plan-eng-review` — engineering review
- `/plan-devex-review` — operator-experience checklist
