# Feature trains (shippable increments)

Pack consumers copy this language; the SSOT for Manolii policy remains
`Manolii-org/master` `docs/feature-train.md` (G-P4 = yes, 2026-09-14).

`main` is the integration branch **and** staging. Incomplete **slices** must
not land on `main`. That is unchanged.

## Isolated-staging-held carve-out (G-P4)

Recoverable staging **UX** on held-prod / `isolated-staging-held` apps is not a
slice. Those apps may merge to `main` when preview/staging can be temporarily
worse until the next **release candidate** (`workflow_dispatch` / explicit
promote) while the **prod alias stays held**.

| Still forbidden on `main` | Allowed on held-prod `main` |
|---|---|
| Incomplete slices (`Shippable: no`, stacked trunk `main`, “migration now / app later”) | Staging UX recoverable on the next RC |
| Any merge that needs a sibling PR or a revert to become promotable | Expand-only / default-off / unused API |

**In scope (example profile):** product apps whose production promote is held
(BCP, Impaktful). **Out of scope:** control-plane, libraries, auto-promote
apps, and this pack itself. Do not copy dirty-staging into Knowledge Layer,
Lead-Converter, scrape, cryptotrading, or master.

Do **not** bump consumer `@v*` pins for this language (G-P0). Do **not**
activate Integration Admission on every PR as a Wave H default
(`docs/contracts/integration-admission.md`).
