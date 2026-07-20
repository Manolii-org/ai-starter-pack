# Kernel file provenance

Source of truth: `manolii-org/master` @ `db2f0cf69405a1448715c238d0265a294169e8b8`
(main, 2026-07-20). Files are verbatim copies; refresh this table in the same PR
whenever they are re-synced. Verify with `sha256sum -c` against the values below
(paths relative to `kernel/backup/`; the scripts/ sub-layout mirrors master so relative lib sourcing works verbatim).

| Kernel path | Master path | SHA-256 |
|---|---|---|
| scripts/lib/backup-db-lib.sh | scripts/lib/backup-db-lib.sh | e2e3122fab0a8d7c67406cb63051d2a08738ab45d4bd88f4dd27c848ece537ed |
| scripts/backup-pg-dump.sh | scripts/backup-pg-dump.sh | f5b89698acb6e77ac10ac28723dd8c7ca52bb28148f1b3a4900f7097508de4ab |
| scripts/restore-drill.sh | scripts/restore-drill.sh | 20d06d4871a43e6015b0197dbd3d0650eaf49bd5566c6d5c7779b5875157e826 |
| scripts/restore-drill-neon-app.sh | scripts/restore-drill-neon-app.sh | 295c0cf9ae6b477f0a442b878a1e12c1fbe5aac8c03e27cdf09db248216f2cf0 |
| scripts/backup-resolve-db-url.py | scripts/backup-resolve-db-url.py | 9e8e2125c546b8d836328f70d82c1192f614cbe251b7cb3cf5d976d36d8244d7 |
