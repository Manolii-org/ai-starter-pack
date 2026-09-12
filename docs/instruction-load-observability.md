# Instruction-load observability

Claude Code may emit an asynchronous `InstructionsLoaded` event. This pack
records a content-free receipt (sanitised path + `observed_sha256`) for
correlation. It is observability, not a blocking safety control.

**Codex and Cursor do not emit this event.** Do not fabricate a Claude-style
host receipt on those surfaces. Keep untrusted Cursor Cloud checkouts free of
repository-controlled lifecycle hooks.

## Consumer configuration

Edit `config/instruction-load-canary.json` so `--require` matches files this
repository actually loads. Do not assume
`.claude/persistent-instructions.md` exists.

```bash
python3 scripts/verify-instruction-load-audit.py --session-id "$CLAUDE_CODE_SESSION_ID"
```

Receipts stay local and gitignored: `.ai/memory/instruction-loads.jsonl` and
`.ai/memory/*.lock`. Rotation uses `scripts/rotate-jsonl-receipt.py` (sidecar
lock, unique same-second archives). Observations are not failure telemetry.

Disable the Claude hook at render time with `--data claude_hooks=false`.
The handler scripts may still ship; settings and the plugin event must not
claim a host lifecycle that the profile does not have.

POSIX `fcntl` locking is required. Rollback is hook removal; leftover receipts
remain ignored.
