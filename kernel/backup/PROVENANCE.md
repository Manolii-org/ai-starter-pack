# Kernel file provenance

Source of truth: `manolii-org/master` @ `e05fb1e6a711`
(main, 2026-07-20; includes PR #2910 restore-drill.sh fixes: reachable aws-ls
error handler + early Neon-branch cleanup trap). Files are verbatim copies; refresh this table in the same PR
whenever they are re-synced. Verify with `sha256sum -c` against the values below
(paths relative to `kernel/backup/`; the scripts/ sub-layout mirrors master so relative lib sourcing works verbatim).

| Kernel path | Master path | SHA-256 |
|---|---|---|
| scripts/lib/backup-db-lib.sh | scripts/lib/backup-db-lib.sh | e2e3122fab0a8d7c67406cb63051d2a08738ab45d4bd88f4dd27c848ece537ed |
| scripts/backup-pg-dump.sh | scripts/backup-pg-dump.sh | f5b89698acb6e77ac10ac28723dd8c7ca52bb28148f1b3a4900f7097508de4ab |
| scripts/restore-drill.sh | scripts/restore-drill.sh | 38890c4e6991f837ef95d21da1faf96d27cc5615ee6b2de79e0fbe774aab76b7 |
| scripts/restore-drill-neon-app.sh | scripts/restore-drill-neon-app.sh | 295c0cf9ae6b477f0a442b878a1e12c1fbe5aac8c03e27cdf09db248216f2cf0 |
| scripts/backup-resolve-db-url.py | scripts/backup-resolve-db-url.py | 9e8e2125c546b8d836328f70d82c1192f614cbe251b7cb3cf5d976d36d8244d7 |
