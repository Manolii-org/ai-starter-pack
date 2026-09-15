"""Prevent distributed LiteLLM routes from permitting OpenRouter retention."""

from pathlib import Path
from urllib.parse import urlsplit

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]


def _reaches_openrouter(params: dict) -> bool:
    model = str(params.get("model", ""))
    api_base = str(params.get("api_base", ""))
    if model.startswith("openrouter/"):
        return True
    candidate = api_base if "//" in api_base else f"//{api_base}"
    try:
        host = (urlsplit(candidate).hostname or "").lower()
    except ValueError:
        return False
    return host == "openrouter.ai" or host.endswith(".openrouter.ai")


def test_every_openrouter_hop_enforces_zero_retention():
    config = yaml.safe_load(
        (REPO_ROOT / "deploy" / "litellm-proxy" / "config.yaml").read_text(
            encoding="utf-8"
        )
    )
    hops = []
    violations = []
    for deployment in config["model_list"]:
        params = deployment.get("litellm_params") or {}
        if not _reaches_openrouter(params):
            continue
        alias = deployment.get("model_name", "<unnamed>")
        hops.append(alias)
        provider = (params.get("extra_body") or {}).get("provider") or {}
        if provider.get("data_collection") != "deny":
            violations.append(f"{alias}: data_collection must be deny")
        if provider.get("zdr") is not True:
            violations.append(f"{alias}: zdr must be true")

    assert hops, "expected at least one OpenRouter deployment"
    assert not violations, "\n".join(violations)
