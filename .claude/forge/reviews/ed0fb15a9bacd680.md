commit: fix(ci): alnum-normalised credential chunk-match across transform variants
files: .github/workflows/codex-pr-review.yml
verdict: PASS — fifth dogfood round: separator-fragmentation and rot13 reconstructions bypassed literal replace; now normalise output to alnum-only and chunk-match every variant (exact/b64/b64url/hex/reversed/rot13); locally proven: fragmented withheld, rot13 withheld, clean text passes.
