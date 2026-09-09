"""Attach a safe LiteLLM product identity to request metadata and startup logs."""
from __future__ import annotations

import json
import hashlib
import logging
import os
from pathlib import Path

import yaml

from litellm.integrations.custom_logger import CustomLogger
from litellm.proxy._types import UserAPIKeyAuth

logger = logging.getLogger(__name__)
_DEFAULT_PATH = Path(__file__).resolve().with_name("litellm-product-attestation.json")
_REQUIRED_FIELDS = {
    "schema_version",
    "product_version",
    "profile",
    "source_revision",
    "config_sha256",
    "bundle_sha256",
}


def load_attestation(path: Path | None = None) -> dict:
    target = path or Path(os.environ.get("LITELLM_PRODUCT_ATTESTATION_PATH", str(_DEFAULT_PATH)))
    value = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("LiteLLM product attestation must be an object")
    missing = _REQUIRED_FIELDS - set(value)
    if missing:
        raise ValueError(f"LiteLLM product attestation missing fields: {sorted(missing)}")
    for field in ("config_sha256", "bundle_sha256"):
        digest = value[field]
        if not isinstance(digest, str) or len(digest) != 64:
            raise ValueError(f"LiteLLM product attestation {field} must be SHA-256")
    return value


def verify_attested_sources(attestation: dict, config_path: Path | None = None) -> None:
    """Fail closed unless the running config and callback bundle match the receipt."""
    target = config_path or Path(
        os.environ.get("LITELLM_PRODUCT_CONFIG_PATH", str(Path(__file__).resolve().with_name("config.yaml")))
    )
    config = yaml.safe_load(target.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ValueError("LiteLLM product config must be a YAML object")
    config_bytes = json.dumps(config, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    config_digest = hashlib.sha256(config_bytes).hexdigest()
    if config_digest != attestation["config_sha256"]:
        raise ValueError("LiteLLM product config digest does not match attestation")

    callbacks = sorted(set(config.get("litellm_settings", {}).get("callbacks", [])))
    bundle = hashlib.sha256(config_bytes)
    source_dir = Path(__file__).resolve().parent
    for callback in callbacks:
        module = callback.split(".", 1)[0]
        callback_path = source_dir / f"{module}.py"
        if not callback_path.is_file():
            raise ValueError(f"configured callback source is missing: {callback_path.name}")
        bundle.update(callback_path.name.encode() + b"\0" + callback_path.read_bytes())
    if bundle.hexdigest() != attestation["bundle_sha256"]:
        raise ValueError("LiteLLM product callback bundle digest does not match attestation")


class ProductAttestation(CustomLogger):
    def __init__(self) -> None:
        self.attestation = load_attestation()
        verify_attested_sources(self.attestation)
        expected = os.environ.get("LITELLM_PRODUCT_PROFILE")
        if expected and self.attestation["profile"] != expected:
            raise ValueError(
                f"LiteLLM product profile mismatch: expected {expected!r}, "
                f"got {self.attestation['profile']!r}"
            )
        logger.info(
            "[litellm_product_attestation] %s",
            json.dumps(self.attestation, sort_keys=True, separators=(",", ":")),
        )

    async def async_pre_call_hook(
        self,
        user_api_key_dict: UserAPIKeyAuth,
        cache,
        data: dict,
        call_type: str,
    ) -> dict:
        if not isinstance(data, dict):
            return data
        metadata = data.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
            data["metadata"] = metadata
        targets = [metadata]
        litellm_metadata = data.get("litellm_metadata")
        if isinstance(litellm_metadata, dict) and litellm_metadata is not metadata:
            targets.append(litellm_metadata)
        for target in targets:
            target["litellm_product_version"] = self.attestation["product_version"]
            target["litellm_product_profile"] = self.attestation["profile"]
            target["litellm_config_sha256"] = self.attestation["config_sha256"]
        return data


product_attestation = ProductAttestation()
