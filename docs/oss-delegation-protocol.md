# OSS Delegation Protocol

Post Sonnet-executor flip: the main thread (Sonnet) is cheap enough for judgment, orchestration, and moderate file work. OSS sub-agents remain valuable for thick parallel implementation and agentic loops. Three rules apply.

**Quality assurance:** For every rule below, the main thread retains the review step — sub-agents implement, main thread approves.

---

## Rule 1 — Thick sub-agent for parallel multi-file implementation

**Trigger:** Any task requiring 3+ file edits across 2+ independent scopes that can execute in parallel.

**Do NOT:** Call Edit/Write/Read in sequential main-thread turns across multiple independent scopes.

**Do:** Write a precise implementation brief, then dispatch:
```
Agent(subagent_type="generate", model="tier-0-oss-heavy",
      description="Implement: <specific change>",
      prompt="<exact files, functions, expected behaviour, constraints>\nReturn a summary of changes made.")
```
Use `tier-1-fast` for routine edits. Use `tier-0-oss-heavy` for complex multi-file patches, schema changes, or refactors. Main thread reviews the returned diff summary only.

**Data exception:** If any file contains `data_sensitivity: restricted`, use `model="sonnet"`.

## Rule 2 — OSS executor + review-internal gate for boundary tasks

**Trigger:** Any internal (non-client, non-restricted) task that matches a heavy-main escalation pattern but does NOT require Anthropic-only routing (no `data_sensitivity: restricted` or `anthropic_only`).

**Do NOT:** Skip the review gate — OSS models on boundary-crossing tasks have ~40% self-reported accuracy vs ~80-85% with a Sonnet review pass.

**Do:** Sequential two-step dispatch: OSS implements, `review-internal` validates.

```python
# Step 1: OSS implementation
result = Agent(subagent_type="generate", model="tier-0-oss-heavy",
               description="Implement: <specific change>",
               prompt="<exact files, functions, expected behaviour, constraints>\n"
                      "Return a diff summary of all changes made.")

# Step 2: Sonnet review gate
review = Agent(subagent_type="review-internal",
               description="Review OSS implementation",
               prompt=f"Review this implementation diff for correctness, security, and "
                      f"pattern adherence before it is applied:\n\n{result}\n\n"
                      f"Context: <original task spec>\n"
                      f"Respond with APPROVE or ESCALATE + specific concerns.")
```

**Resolution path on review failure:** Escalate directly to Sonnet main thread. Do NOT re-dispatch to OSS — one retry budget per task.

**Data exception:** If any file has `data_sensitivity: restricted` or `anthropic_only`, skip OSS entirely and handle on Sonnet main thread.

---

## Rule 3 — Agentic loops stay inside sub-agents (implement + test + fix cycles)

**Trigger:** Any task with an iteration loop — write code, run tests, fix failures, repeat.

**Do NOT:** Run the implement→test→fix cycle as sequential main-thread turns.

**Do:** Delegate the entire loop to a single sub-agent call:
```
Agent(subagent_type="generate", model="tier-2-agentic",
      description="Implement + test: <feature>",
      prompt="<spec>\n\nWorkflow:\n1. Implement the change\n2. Run tests: <test command>\n"
             "3. Fix any failures\n4. Repeat until all tests pass or 3 iterations exhausted\n"
             "Return: final diff summary + test output.")
```
The sub-agent handles the full loop (20+ internal tool calls) and the main thread sees one result.

## Rule 4 — Internal code-patch tasks use tier-0-oss-heavy, not sonnet

**When dispatching Agent() for internal (non-client, non-restricted) code-patch or refactor tasks:**
```
# Use this:
Agent(..., model="tier-0-oss-heavy")
# Not this:
Agent(..., model="sonnet")             # Reserve sonnet for orchestration or restricted data
```
Named agents already configured for this: `test-hardener`, `review-internal`. For ad-hoc Agent() calls on internal code work, explicitly pass `model="tier-0-oss-heavy"`.

---

## OSS routing summary

| Task type | Sub-agent | Model | Why |
|---|---|---|---|
| Parallel multi-file edit | generate | tier-0-oss-heavy | Better on code-patch benchmarks |
| Boundary task (auth/schema/security) — internal data | generate → review-internal | tier-0-oss-heavy → sonnet | Rule 2: OSS implements, Sonnet gates |
| Routine multi-file edit (<64K ctx) | generate | tier-1-fast | Cheap, good accuracy |
| Implement + test loop | generate | tier-2-agentic | Large context for long loops |
| Extraction / grep / search | generate | tier-4-extract | Fast, precise |
| PR test writing | test-hardener | tier-0-oss-heavy | Already configured |
| Client data / restricted | any | sonnet or higher | Anthropic-only routing |
