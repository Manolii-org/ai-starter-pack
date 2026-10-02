#!/usr/bin/env python3
"""Run `npx e2e <args>` with telemetry off and LiteLLM creds mapped in-process (never printed)."""
import os
import subprocess
import sys
from pathlib import Path

env = dict(os.environ)
env.setdefault("BB_LITELLM_URL", env.get("LITELLM_PROXY_URL", ""))
env.setdefault("BB_LITELLM_KEY", env.get("LLM_API_KEY", ""))
missing = [k for k in ("BB_LITELLM_URL", "BB_LITELLM_KEY") if not env.get(k)]
if missing:
    sys.exit(f"missing env: {', '.join(missing)}")
env["E2E_TELEMETRY_DISABLED"] = "1"
env.pop("CI", None)
sys.exit(subprocess.call(["npx", "e2e", *sys.argv[1:]], env=env, cwd=Path(__file__).parent))
