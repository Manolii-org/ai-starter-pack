# LiteLLM Proxy — Deploy Pattern (No Custom Image)

This directory deploys the multi-model routing proxy to Fly.io using the
**pinned public upstream image** plus **deploy-time file injection** — there is
no Dockerfile and no per-instance image build (ADR-0023 Decision 3).

```text
ghcr.io/berriai/litellm-non_root:<pinned>   ← public image, used as-is
  + [[files]] config.yaml                   ← injected at deploy (instance policy)
  + [[files]] sonnet_advisor_guardrail.py   ← injected at deploy
  + [env] PYTHONPATH=/tmp                   ← lets LiteLLM import the guardrail
```

**Why no baked config:** `config.yaml` is instance *policy*, not just secrets —
instances legitimately diverge (e.g. an instance with no Anthropic contract
strips the entire Anthropic tier). A shared image with baked config would
silently impose one instance's policy on another. Each instance ships its own
`config.yaml` next to `fly.toml`; the image stays generic.

**Deploy:** `scripts/setup-litellm.sh` (interactive), or manually:

```bash
cd deploy/litellm-proxy && flyctl deploy --app <your-app>
```

Relative `[[files]] local_path` values resolve against the directory
containing `fly.toml` (per Fly docs); deploying from this directory also lets
`flyctl` auto-discover the config file.

## Injection variants (validated 2026-06-12 on a live Fly app)

Both variants were deployed to a throwaway Fly app and verified
(`/health/liveliness` 200; guardrail file written + chowned by the init layer;
callback import confirmed at startup — a missing/unimportable callback aborts
LiteLLM startup, so a healthy proxy with this config cannot have skipped it):

| Variant | Verdict | Notes |
|---|---|---|
| `local_path` (pack default) | pass | No base64 step, no secret management; file content is embedded in the machine config and visible via the Machines API to anyone with app access — acceptable for policy files (API keys stay in Fly secrets). Relative paths resolve against the fly.toml directory. |
| `secret_name` (base64) | pass | Production-parity (the upstream maintainer's production proxy runs this form with 13 injected modules). Requires base64-encoding each file into a Fly secret; total file secrets must stay under Fly's 64 KiB ceiling (this pack's two files ≈ 50 KB encoded). |

**Fallback** if `[[files]]` injection is unavailable in your environment: deploy
the public GHCR image *without* config and mount/manage config out-of-band —
never bake `config.yaml` into a shared image.

## Migrating an existing instance off a custom image

For instances that adopted a `[build] dockerfile` proxy (a deployment that
bakes config + guardrail into a custom image), the fly.toml diff is:

```diff
 [build]
-  dockerfile = "Dockerfile"
+  image = "ghcr.io/berriai/litellm-non_root:v1.82.3-stable.patch.4"

+[[files]]
+  guest_path = "/tmp/config.yaml"
+  local_path = "config.yaml"
+
+[[files]]
+  guest_path = "/tmp/sonnet_advisor_guardrail.py"
+  local_path = "sonnet_advisor_guardrail.py"
+
+[env]
+  PYTHONPATH = "/tmp"
+
 [processes]
-  app = "--config /app/config.yaml --port 4000 --num_workers 2"
+  app = "--config /tmp/config.yaml --port 4000 --num_workers 1"
```

Then delete the instance's Dockerfile and redeploy from this directory. Keep
`--num_workers 1`: multi-worker spawn mode can deadlock at startup when custom
Python callbacks import `litellm` at module level. Instances adopt this via
copier update / their operator — applying it to any specific downstream
instance is out of scope for the pack itself.
