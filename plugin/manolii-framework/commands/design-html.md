---
name: design-html
version: 1.0.0
description: Convert a chosen mockup variant to product-native components (Tailwind/Next/etc.)
type: command
requires_mcp: []
required_entities: []
safety_tier: amber
tags: ['design', 'ui', 'components']
blast_radius: medium
---

# /design-html — winner picker → product components

Based on proven patterns from the Manolii ecosystem.

Takes a chosen `/design-shotgun` variant + operator notes, converts it to the target product's component library using Sonnet.

## Usage

```
/design-html <shotgun-id>/<variant-id> [--into <output-path>] --notes "<picks/changes>"
```

Example:

```
/design-html shotgun-20260101-abc123/v2 --into src/components/Checkout.tsx --notes "Use #2 layout but the green from #4"
```

## Steps

1. Read the chosen variant HTML from `.ai/design-variants/<shotgun-id>/<variant-id>.html`
2. Load product design tokens from `.ai/design-systems/<product>.json` — derived from output path slug
3. Apply operator notes (compose, e.g. layout from #2 + colours from #4)
4. Dispatch via Sonnet to convert HTML → product-native component (Tailwind classes, framework idioms)
5. Write to the operator-chosen path
6. Optionally save taste signal: `/remember "Picked variant <id> for <scene>. Notes: <notes>" tag=design-taste`

## Token cost

- ~2k sonnet for the conversion (one component per call)

## See also

- `/design-shotgun` — generates the input variants
- `.ai/design-systems/<product>.json` — token files
