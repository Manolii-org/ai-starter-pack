# Model Plane Boundary — where the Claude subscription ends and the OSS proxy begins

> **Authoritative for:** which model serves which plane, and how each plane is wired.
> **Layer:** L3. **Last verified:** 2026-08-07 against the live router (`/model/info`).
>
> Read this before asserting any model ID is "Anthropic-direct", before changing
> `ANTHROPIC_BASE_URL`, and before pinning a model in `.claude/settings.json`.

> **Updated 2026-08-09:** sonnet-group primary remapped to DeepSeek V4 Flash 0731 (AA Max II 52). Fail-closed Llama 3.3 70B advisor unchanged. See `.ai/assessments/sonnet-flash-0731-2026-08-09.md`.

## Intent

Two planes, and they are **not** the same thing:

| Plane | What runs there | Which model | Why |
|---|---|---|---|
| **Main thread** | The Claude Code session the operator talks to | **The Claude subscription model** — Opus or Sonnet, whichever the client has selected | Judgement, architecture, verification. This is what the subscription is for. |
| **Delegation** | Bulk, mechanical, parallel, fan-out work dispatched *away* from the main thread | **OSS via the LiteLLM proxy** | Cost. Delegation is supplementary — it serves the main thread, it does not replace it. |

The OSS proxy exists to make delegation cheap. **It is not meant to serve the main thread.**

## Reality check: that split has never run in Claude Code (added 2026-08-10)

The Intent table above describes a target state, **not the behaviour of any Claude Code session to date**. Three independent lines of evidence:

1. **Claude Code has no per-sub-agent transport.** `ANTHROPIC_BASE_URL` is process-wide, and none of the eight `CLAUDE_CODE_*SUBAGENT*` env vars in the 2.1.226 binary is a base-URL or provider override. Per-agent provider routing is an **open feature request** — [anthropics/claude-code#38698](https://github.com/anthropics/claude-code/issues/38698), filed 2026-03-25, still open — whose only listed workaround is separate sessions, "which loses agent orchestration capabilities."

2. **Only two states are reachable**, and neither is the Intent table:

   | `CLAUDE_CLIENT_USE_PROXY` | Main thread | Agent-tool sub-agents |
   |---|---|---|
   | `0` (default) | Anthropic ✅ | **Anthropic** ❌ (direct, not OSS) |
   | `1` | **OSS** ❌ (not Claude) | OSS ✅ |

3. **Earlier versions exported unconditionally.** `scripts/claude-session-start.sh` set `ANTHROPIC_BASE_URL` whenever `LITELLM_PROXY_ACTIVE=1`, with no plane gate — the entire client (main thread included) ran on OSS whenever the proxy was up.

It looked correct because `MAIN_THREAD_EXECUTOR_MODEL=claude-sonnet-4-6` reads as an Anthropic ID while the proxy remaps it. That is precisely the mistake the next section exists to prevent — it was live in this repo for four months.

### How to actually get the split

Delegation must call the proxy **as a tool**, not via `Agent()`: a script or MCP server that posts to the proxy from a **subprocess** with its own `ANTHROPIC_BASE_URL`. `ANTHROPIC_BASE_URL` is per-process, so a child can be proxied while the session is not — `scripts/bootstrap_env.py::_set_synthesis_env()` already does exactly this for eval/synthesis callers.

Trade-off: you give up in-session `Agent()` orchestration (context passing, integrated returns, the transcript) for shell-out delegation you collect yourself. Decide it deliberately.

**Independent of transport:** restoring OSS delegation does **not** fix delegation *quality*. A 120-word sub-agent return starves the main thread whether Haiku 4.5 or DeepSeek V4 Flash wrote it. See `docs/output-token-discipline.md` § Sub-agent return strings.

## The mistake this document exists to prevent

`claude-sonnet-4-6` looks like an Anthropic model ID. The proxy does not treat it as one.

Verified via `GET /model/info` on a deployed LiteLLM router — the router
reporting its own backends:

```
claude-sonnet-4-6         ->  fireworks_ai/accounts/fireworks/models/deepseek-v4-flash-0731
claude-sonnet-4-6         ->  together_ai/deepseek-ai/DeepSeek-V4-Pro
claude-haiku-4-5-20251001 ->  fireworks_ai/accounts/fireworks/models/deepseek-v4-flash-0731
haiku                     ->  fireworks_ai/accounts/fireworks/models/deepseek-v4-flash-0731
sonnet                    ->  fireworks_ai/accounts/fireworks/models/deepseek-v4-flash-0731
```

**When the proxy is in the request path, `claude-sonnet-4-6` is DeepSeek V4 Flash 0731** (advisor-guarded; V4 Pro in ordered fallbacks, not LB twin). Any rule,
comment or hook calling it "Anthropic-direct" is wrong and must be corrected, not worked around.

### Re-verify it — do not trust this table's date

The block above is a point-in-time observation. A proxy route-table change would silently
invalidate it, and the fail-closed reasoning built on top (RULE8 preferring `opus` *because* the
proxy cannot serve it) would quietly stop holding. Asserted-once evidence is not a control, so
the claims are re-runnable:

```bash
python3 scripts/verify-model-plane.py          # 0 = claims hold, 1 = DRIFTED, 2 = unverified
```

Exit 2 (no endpoint, no key, proxy unreachable) is deliberately not 0 — unverified is not the same
as verified. On drift it names which claim moved, so this document and the RULE8 guidance in
`scripts/lint-agent-routing.py` get re-checked together. The most dangerous drift it catches is an
Anthropic passthrough appearing for `opus`, which would end its fail-closed property.

### You cannot detect this by asking the model

| Requested | Self-report (probed 2026-08-07) |
|---|---|
| `claude-sonnet-4-6` | *"I'm Claude, developed by Anthropic, cutoff April 2024"* |
| `haiku` | *"DeepSeek-V3, developed by DeepSeek"* |

DeepSeek V4 Pro claims to be Claude, and the `haiku` route named the wrong DeepSeek version.
**Self-identification is not evidence.** Use `/model/info`. Nothing else settles it.

## Why the main thread drifted onto the OSS plane

`ANTHROPIC_BASE_URL` is **all-or-nothing for the Claude Code client**. Setting it redirects the
entire client — main thread *and* every `Agent`-tool sub-agent. There is no per-agent base URL, so
this mechanism cannot express "subscription main thread, OSS sub-agents".

The proxy serves no Opus route: `opus`, `claude-opus-5` and `claude-opus-4-7` all return **400
Invalid model name**. So once `ANTHROPIC_BASE_URL` is set on the client, an Opus session cannot
function, and the main thread had to be pinned to an ID the proxy does serve — which is exactly what
`.claude/settings.json` `"model": "claude-sonnet-4-6"` *used to* do (pin removed 2026-08-08 once the
client defaulted off the proxy).

**That pin is not an independent mistake. It is the forced consequence of routing the client through
the proxy** — the tell that the delegation mechanism is applied at the wrong layer.

## The two mechanisms — use the right one

**Mechanism A — programmatic (correct for delegation).** `scripts/assessment/sdk_runner.py` calls
the proxy over plain HTTP using `LITELLM_PROXY_URL` + `LLM_API_KEY` / `LITELLM_MASTER_KEY`. This is
process-local: it never touches the Claude Code client, so the main thread stays on the subscription
model. `sdk_runner.py`'s own header says it exists to avoid "`ANTHROPIC_BASE_URL` env-var leakage
that would silently route calls". PR assessment, evals and batch runners use it.

**Mechanism B — `ANTHROPIC_BASE_URL` on the client (wrong layer for delegation).** Redirects
everything, forces the main-thread pin, makes Opus impossible. Use only when you intend the *whole*
session — judgement included — to run on OSS.

## Wiring invariant (regression-prone — check this first)

Until 2026-08-07 `scripts/claude-session-start.sh` fetched `LITELLM_PROXY_URL` from the secrets
manager but **never exported it**, so `ANTHROPIC_BASE_URL` was the only thing wiring Mechanism A.

Effect: the planes were silently coupled. Clearing `ANTHROPIC_BASE_URL` to put the main thread back
on the subscription model would also have broken every proxy-delegated call, with no error
explaining why. `claude-session-start.sh` now exports `LITELLM_PROXY_URL` under its own name and
lists it in the env-file de-dup filter so it cannot accumulate stale values.

**Precedence (unified 2026-08-08):** `_resolve_route` in `sdk_runner.py` is the single
resolver for `invoke_agent`, `invoke_agent_metered`, ReAct, and `invoke_agent_with_model`
(proxy path). The named `LITELLM_PROXY_URL` **wins** when set; `ANTHROPIC_BASE_URL` is only
a fallback when the named endpoint is absent (legacy / `CLAUDE_CLIENT_USE_PROXY=1` shells).

**Invariant:** `LITELLM_PROXY_URL` (and `LITELLM_MASTER_KEY` / `LLM_API_KEY`) must be set
independently of `ANTHROPIC_BASE_URL` so clearing the client redirect cannot break delegation.


### The switch that makes the intent reachable

`USE_LITELLM_PROXY=0` looks like the way to keep the main thread on the subscription model. It is
not: it skips the credential fetch entirely, so `LITELLM_PROXY_URL` and `LLM_API_KEY` are never
exported and **delegation goes down with the client redirect**. That flag conflates two decisions —
*fetch proxy credentials* and *point the Claude Code client at the proxy*.

`CLAUDE_CLIENT_USE_PROXY` (added 2026-08-07, `scripts/claude-session-start.sh`) gates **only** the
client redirect:

| Setting | Main thread | Agent-tool sub-agents | Programmatic delegation |
|---|---|---|---|
| **default (`0`)** | **Claude subscription model** | Anthropic | **OSS — still wired** |
| `CLAUDE_CLIENT_USE_PROXY=1` | proxy → OSS, needs the `settings.json` pin | OSS | OSS |
| `USE_LITELLM_PROXY=0` | Claude subscription model | Anthropic | **broken — no endpoint** |

Default is `0` as of 2026-08-08 — the Intent row above. Opt in to `CLAUDE_CLIENT_USE_PROXY=1`
only when you deliberately want the whole Claude Code client on OSS. Cost of the default: with the
client un-proxied, Agent-tool sub-agents run on Anthropic rather than OSS, because
`ANTHROPIC_BASE_URL` is all-or-nothing and there is no per-agent override. Only the programmatic
path (`sdk_runner`, evals, batch runners) keeps OSS routing.

With default `0`, the `.claude/settings.json` model pin is no longer forced by the proxy: the
client-selected subscription model (including Opus) works. Keep the pin only if you want a
fixed Sonnet default on Anthropic; removing it while `CLAUDE_CLIENT_USE_PROXY=1` still 400s
because the proxy has no Opus route — those two must still move together when opting into proxy
mode.

**The flag must be honoured in BOTH bootstraps.** `scripts/load-ecosystem.sh` is the one that
exports the client vars into the shell a client is launched from, so it is authoritative: a
SessionStart hook cannot retract what a parent shell already exported. Both files now gate the
client redirect on `CLAUDE_CLIENT_USE_PROXY` and export `LITELLM_PROXY_URL` outside that gate.
`load-ecosystem.sh` had the identical original defect — it fetched `LITELLM_PROXY_URL` into a local
and never exported it.

Note for anyone tempted to fix this by writing `unset` into `CLAUDE_ENV_FILE`: that file is
**parsed** for `export KEY=VALUE` lines (`scripts/pre-tool-use.py:263-269`), not sourced, so an
`unset` there is not a reliable retraction. It is written as belt-and-braces only.

`scripts/tests/test_session_start_plane_separation.py` pins the structural property across both
files — the `LITELLM_PROXY_URL` export must stay **outside** the `CLAUDE_CLIENT_USE_PROXY`
conditional, and both must spell the flag the same way. Verified by mutation: moving the export
inside fails the guard.

```bash
printenv LITELLM_PROXY_URL   # must be set on its own
printenv ANTHROPIC_BASE_URL  # may be unset without breaking delegation
```

## Consequences for the security rules

Two agents are pinned to `claude-sonnet-4-6` in `.claude/model-routing.json` **because it was
believed Anthropic-direct**: `agent_routing.infra` (processes secret values)
and `agent_routing.ecosystem-auditor` (reads secret names across all projects). Their notes say the
pin stops those values reaching "a third-party OSS provider (DeepSeek)".

Given `/model/info`, **the pin does not achieve that whenever the proxy is in the request path** —
it routes to DeepSeek V4 Pro, the outcome the note says it prevents. The same false premise sits in
OSS Delegation Protocol **Rule 0**, which sends secret-bearing work to `claude-sonnet-4-6`.

Correct rule: **secret-bearing work must run where the proxy is not in the path.** With the main
thread on the subscription model, that means the main thread — not a sub-agent, and not any full
`claude-*` ID, because none of them is a reliable proxy-bypass.

`.claude/model-routing.json` is guarded (`.ai/guards.json` → `model-routing`); correcting those
notes and `tier_aliases.$comment` needs `/unfreeze` plus operator sign-off.

## Surface inventory — everywhere the false premise still lives

**The hand-maintained count was wrong, and the method that produced it was the
problem.** Earlier drafts claimed nine surfaces, then ten. `scripts/check-model-plane-premise.py`
(added 2026-08-08) greps every tracked file — run it; do not cite a remembered count.
Live operator surfaces (`CLAUDE.md`, `docs/oss-delegation-protocol.md`, `.claude/model-routing.md`,
hooks, RULE8) were corrected through 2026-08-08 alongside `CLAUDE_CLIENT_USE_PROXY` default `0`.
Residual: comments behind the `zero-prompt-hook` guard and stale wording inside the guarded
`.claude/model-routing.json` (needs operator `/unfreeze`).

Three sweeps were declared complete on this PR and each was falsified by an adversarial
reviewer -- `.claude/model-routing.md`, then `docs/oss-delegation-protocol.md` and
`scripts/model-usage-report.py`. Each time the response was to fix the named surface and
re-assert completeness. That was the error: a hand-maintained inventory is a snapshot of one
person's grep, so it cannot see what that grep missed, and nothing stops the sentence being
written again tomorrow. **Do not re-assert a count here. Run the checker.**

```bash
python3 scripts/check-model-plane-premise.py                # operative assertions
python3 scripts/check-model-plane-premise.py --list-exempt   # historical records + reasons
```

The bulk of the 87 are `prompts/` epic headers carrying one boilerplate routing table, plus
`templates/ai-starter-pack/` (which syncs to consumer repos) and generated configs
(`config/model-routing.source.yaml` → `.claude/model-routing.json`, guarded). Clearing them is a
coordinated change across guarded paths and a synced template, and is **not** attempted here.
The checker is advisory until that lands; flipping it to blocking is a one-line CI change.

The table below is retained as the record of what THIS PR corrected -- ten operative
surfaces, the highest-risk ones -- not as a claim about the repository.

**RULE8's two accepted values are not equivalent, and that is the substantive finding.** The rule
accepts `opus` and full `claude-*` IDs as interchangeable. Under the proxy they behave in opposite
ways: `opus` returns 400 (request dies, nothing leaks) while `claude-sonnet-4-6` returns 200 from
`deepseek-v4-pro` (secrets leave silently). One is fail-closed, the other fail-open. The linter now
leads with `opus` for that reason. It still *accepts* `claude-sonnet-4-6` rather than flagging it,
because `agent_routing.infra` and `agent_routing.ecosystem-auditor` are pinned to that ID in the
guarded `.claude/model-routing.json` — flagging it would fail CI with no in-repo remedy until that
file is unfrozen. Moving those two pins to `opus` is the real fix and needs `/unfreeze`.

| # | Surface | Status | Note |
|---|---|---|---|
| 1 | `scripts/post-tool-use.py` (secret-bearing advisory) | **fixed** 2026-08-07/08 | nudges opus-pinned `infra` (OMIT `model=`); main-thread/broker fallback |
| 2 | `scripts/post-merge-retro.py:18,60` | **fixed** 2026-08-07 | comments made conditional; runtime logic untouched |
| 3 | `deploy/openhands/openhands-system-prompt.md:65` | already correct | *"Proxy-remapped to OSS `sonnet` (2026-08-06) — NOT Anthropic-direct"* |
| 4 | `scripts/lint-agent-routing.py` RULE8 | **fixed** 2026-08-07 | FIX text now leads with `opus`; states plainly that `claude-sonnet-4-6` is not a bypass |
| 5 | `scripts/lint-agent-routing.py` | **fixed** 2026-08-07 | ported verbatim from #4; Rule 8 regions verified byte-identical |
| 6 | `config/claude-md-content-parity.json:18` | **fixed** 2026-08-07 | the `restricted-us-oss-sonnet` and `oss-rule-0` rule descriptions carried the old premise; both now state that `claude-sonnet-4-6` is not a bypass. Because the parity gate gives CLAUDE.md and this file a shared failure mode, correcting the rule text is what makes CI enforce the corrected premise rather than the old one. |
| 7 | `scripts/pre-tool-use.py:1999` | **OPEN — guarded** | `ANTHROPIC_DIRECT_MODELS` set name overstates the guarantee. Comment-only fix, blocked by the `zero-prompt-hook` guard; needs `/unfreeze` |
| 8 | `scripts/post-tool-use.py:2911` | **fixed** 2026-08-07 | comment now states the pin does not deliver the isolation it was chosen for |
| 9 | `scripts/test-proxy-routing-report.sh`; `scripts/tests/test_lint_agent_routing.py` | **fixed** 2026-08-07 | Tier C relabelled "via proxy → deepseek-v4-pro" (that script probes the proxy, so Tier C exercises the OSS backend); the test docstring now says it asserts *current rule behaviour, not that the routing is safe*, and names the condition under which it should be inverted. Assertion unchanged. |
| 10 | `.claude/model-routing.md:242-250` | **fixed** 2026-08-08 | The operator-facing guide told four agents (`infra`, `ecosystem-auditor`, `review`, `orchestrator`) that `claude-sonnet-4-6` is "Anthropic Sonnet (direct)", and stated outright that those calls "pass through the proxy unchanged to Anthropic". Backend column now reads "⚠️ DeepSeek V4 Pro *while the proxy is active*"; the "How routing works" note states that no full `claude-*` ID is a proxy-bypass and points Rule 0 work at the main thread or the broker. Not guarded — `.ai/guards.json` covers `model-routing.json` only. |

Plus the guarded `.claude/model-routing.json` (`agent_routing.infra`, `agent_routing.ecosystem-auditor`
notes and `tier_aliases.$comment`), which needs `/unfreeze`.

**Ordering constraint:** #4/#5 and #9's test must land together — fixing the linter without the test
fails CI, and fixing the test without the linter leaves the prescription in place. #6 must land with
the CLAUDE.md edit. Do not fix these piecemeal.

## Self-detection

`scripts/check-session-integrity.py` now carries `check_model_plane`, so this stops being something
an audit has to find. It **FAILs** when a `.claude/settings.json` model pin is combined with an
active proxy — the exact pairing that moves the main thread onto OSS — and **WARNs** when the two
planes are coupled through `ANTHROPIC_BASE_URL` with `LITELLM_PROXY_URL` unset. The check runs at
SessionStart and once per session on `UserPromptSubmit`, so a degraded session announces itself.

Seven tests in `scripts/tests/test_session_integrity.py` pin it, including that
`api.anthropic.com` must not read as proxied, that a malformed `settings.json` cannot take the
diagnostic down, and that the check is registered in `CHECKS` — an unregistered check never runs,
which is how this class of defect hides in the first place.

## Quick reference

| Question | Answer |
|---|---|
| What serves the main thread? | The Claude subscription model (Opus/Sonnet, client-selected) |
| What serves delegation? | OSS via LiteLLM, through Mechanism A |
| Is `claude-sonnet-4-6` Anthropic-direct? | **No**, whenever the proxy is in the path |
| Is any `claude-*` ID a reliable proxy-bypass? | No. Absence of the proxy is the only bypass |
| Can Opus run through the proxy? | No — 400 Invalid model name |
| How do I check what an ID really resolves to? | `GET /model/info`. Never ask the model |
| Where does delegation get its endpoint? | `LITELLM_PROXY_URL` — never `ANTHROPIC_BASE_URL` |


## Residual `.claude/settings.json` `env` keys (post-pin removal)

| Key | Current | Effect with `CLAUDE_CLIENT_USE_PROXY=0` |
|---|---|---|
| `CLAUDE_CODE_SUBAGENT_MODEL` | **unset** (removed 2026-08-13) | Must stay unset — env overrides `model=` and frontmatter (Rule 0 risk). Use explicit `model="haiku"` on Explore dispatches; programmatic `sdk_runner` paths stay on OSS |
| `MAIN_THREAD_EXECUTOR_MODEL=claude-sonnet-4-6` | still set | Classifier / executor helpers that read this env still name Sonnet; with the client un-proxied that ID is real Anthropic Sonnet, not DeepSeek |

Do **not** re-add a top-level `"model"` pin while the client is un-proxied — that would freeze the session off Opus even though the proxy no longer forces it.
