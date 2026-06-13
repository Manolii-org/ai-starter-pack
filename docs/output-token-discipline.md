# Output Token Discipline

These rules apply to the main thread and to every sub-agent. They are framed for "all upside, no quality cost" — they cut filler, narration, and re-pasted content, never reasoning.

Excludes by design: answer-first reasoning suppression, hard `max_tokens` truncation, and other tactics with genuine accuracy tradeoffs.

## Hard length caps

- **Inter-tool narration:** ≤25 words. One sentence per logical batch of tool calls, not per call. For 3 parallel reads, one sentence covers all three.
- **End-of-turn summary:** 1–2 sentences. State what changed and (if relevant) what's next. Nothing else.
- **Final response on simple tasks:** ≤100 words unless the task genuinely requires more (research synthesis, plans, structured outputs).
- **Sub-agent return strings:** ≤120 words by default for extraction/review/audit tasks; raise to ≤200 only when complexity requires it. Orchestrator must include the cap in the dispatch prompt; the sub-agent enforces it. For structured outputs (plans, reports, schemas), see medium/high complexity caps below.

## Banned filler phrases

Never start a response with: "Great question", "Sure!", "I'll help you", "Let me…", "I'll now…", "You're absolutely right", "Of course", "Certainly". Never end with: "Let me know if…", "Hope this helps", "Feel free to…", "I'm here if you need…". Begin with the answer or the action.

## End-of-turn antipattern

After a single-file edit do **not** produce a multi-bullet "Summary of changes" recap.

- Bad: a 5-bullet summary listing each line touched.
- Good: "Updated `lib/safety.ts:42` to handle null inputs. Tests pass."

## Diff-only output for code changes

After an `Edit` or `Write`, do **not** restate the change in prose. The diff is the artifact. State outcome only ("Updated X — Y now does Z"), not a re-narration of the patch.

## Reference, never re-paste

Cite `file_path:line_number` rather than pasting source code into a response. Never paste source that already exists in the workspace; never re-paste content the user already supplied.

## Cap intermediate narration

Default to **zero** narration between routine tool calls (Read, Grep, Bash, parallel batches). Speak only when: (a) you find something material, (b) you change direction, (c) you hit a blocker. Silent multi-step is fine when the next step is dictated by the previous result.

## Tabular / list outputs over prose

For audit, status, and review-style outputs, use markdown tables or bullets. A 4-column table is ~40% denser than the same information in prose with no quality loss.

## Sub-agent dispatch contract

Every `Agent()` prompt MUST include an explicit return-length cap. Pattern:

```
Report findings in under 120 words. No preamble. Bullets, not prose.
```

Adjust the cap (50 / 120 / 200) to the task. Without an explicit cap, sub-agents trend toward 500–2000-word recaps that double-charge through orchestrator context.

## Memory anchors before /smart-compact

Before invoking `/smart-compact` or `/compact`, list the 3–5 decisions, file paths, and unresolved blockers that MUST survive the summary. The smart-compact skill includes this step (`Step 0`); main thread should call it explicitly when triggering compact mid-session.

## Headless mode for scripted invocations

Hook scripts and CI helpers that invoke `claude` as a subprocess MUST use `claude --print` (headless mode). Already enforced in core automation; new helpers must follow the same pattern.

## Advisor response ceiling

- For standard escalation outcomes, keep advisor responses to **<=220 words** and structured as `{decision, confidence, alternatives_considered, implementation_guidance}`.
- Exceed 220 words only for security-boundary, schema/migration, or legal/compliance escalations where detail is safety-critical.

## Claude Code style + OSS routing defaults

- Keep **Default** Claude Code output style for day-to-day engineering. Explanatory/Learning styles are for training and usually increase output length.
- For non-sensitive summarize/classify/extract tasks, run an **OSS-first pass** (tier aliases like `tier-1-fast` / `haiku`) and escalate only when confidence <= 6 or an escalation trigger applies.
- Keep Anthropic-native high-capability models for restricted data, security boundaries, and final judgment passes.

## Excluded tactics (have quality tradeoffs)

The following appear in token-reduction guides but are excluded from this discipline because they degrade output quality:

- **Answer-first, reasoning-after** — autoregressive models lose accuracy when forced to commit before reasoning.
- **Hard `max_tokens` truncation** — truncates rather than condenses; produces broken outputs.
- **Suppressing all reasoning steps** — degrades multi-step accuracy. Filler ≠ reasoning.
- **Stripping comments/docstrings before file inclusion** — fragile; affects downstream understanding.

## Complexity-based cap auto-lift

Use `python3 scripts/subagent_output_cap.py --task "..." --file-count N --estimated-steps M --json` to choose a deterministic cap:

- Low complexity: **120** words
- Medium complexity: **250** words
- High complexity: **400** words

Dispatch suffix format remains unchanged; only `N` changes from the computed cap.

## Claude Code Web compatibility

These helpers are CLI-only and compatible with Claude Code Web Bash tool execution:

- `python3 scripts/subagent_output_cap.py ... --json` returns deterministic JSON for prompt suffix construction.
- `python3 scripts/advisor_preflight_check.py ...` returns process exit codes (`0` pass, `2` compact required, `3` snapshot missing/stale) suitable for fail-closed wrappers.

## Auto-Clarity Exception List

Word caps, format-locks, and terse-return contracts are **overridden** in the following contexts. Always emit full grammar with explicit conjunctions and reasoning:

- **Security warnings & CVE-class findings.** Dropped articles → ambiguity → wrong call. The `review` agent and any CVE-tagged finding inside `security-deep-dive`'s JSON `message`/`fix` fields keep prose grammar.
- **Irreversible / destructive operation confirmations.** `DROP`, `rm -rf`, `git push --force`, secret rotation, schema drops, mass deletes. State the operation, the blast radius, and the rollback path in full sentences before executing.
- **Multi-step sequences where step order is load-bearing.** Migrations, deploy chains, multi-repo coordinations. Use numbered steps with explicit conjunctions ("first", "then", "only after"). Fragments that omit ordering risk misexecution.
- **Safety-critical actions.** Merge contacts, bulk delete, send email — operations that need full grammar in approval prompts and audit-log entries for defensibility.
- **Restricted agent outputs.** `review`, `security-deep-dive` finding text, advisor escalations. Decision-grade outputs where article-dropping creates ambiguity.
- **Client-engagement deliverables.** Anything tagged `consulting` or destined for a client environment must read normal-prose. Framework patterns are applied internally with terse style; what the client sees is full grammar.
- **User asks to clarify or repeats the question.** Drop terse mode for the clarification, then resume.

When in doubt, expand. The cost of a confused operator is higher than the saved tokens.

## Sub-agent return-string contracts (format-lock pattern)

Word caps describe length; **format-locks describe shape**. Where a sub-agent's return is inherently a list of finding records or a structured ack, the agent file should declare an explicit line schema with verbatim examples. This eliminates prose preambles ("I reviewed the diff and found..."), severity legends, and per-finding restatement that word caps don't prevent.

Active format-lock contracts:

| Agent | Contract location | Shape |
|---|---|---|
| `diff-reflex` | `.claude/agents/diff-reflex.md` § Output Contract | `path:line: 🔴 CRITICAL: <problem>. <fix>.` per line |
| `review-internal` | `.claude/agents/review-internal.md` § Output Format | Issues table + Verdict line; no Summary/Positives |
| `pr-classifier` | `.claude/agents/pr-classifier.md` § Output Schema | JSON manifest only, no prose |

> **Note on broad PR-assessment agents** (`architecture-impact`, `security-deep-dive`): these intentionally return their full JSON findings object (per `scripts/assessment/broad.py`'s `parse_source_output()` contract), not a compressed ack. Do not add ack-only "Return to Caller" sections to these agents — the orchestrator parses their return string as JSON and would silently drop findings. The `.ai/candidates/<agent>.json` file is a duplicate write for downstream consumers, not a substitute for the JSON return.

### Caller-side contract for built-in `Explore`

`Explore` is a built-in Claude Code subagent type — its agent file cannot be edited. Callers must specify the return shape in the dispatch prompt itself. Standard suffix for locator-class Explore dispatches:

```
Return shape: one line per match, exactly `<path>:<line> — `<symbol>` — <≤6-word note>`.
Sort by path, then line. No preamble, no totals header. Single hit: just the line.
Zero hits: `No match.` Cap total response at 30 lines; if more, emit the top 30 plus
final line `… <N> more matches truncated.`
```

Use this suffix on Explore dispatches that are pure locators (find file, find symbol, find references). Do **not** use it on Explore dispatches that synthesize ("how does this subsystem work?") — those are research, not extraction.
