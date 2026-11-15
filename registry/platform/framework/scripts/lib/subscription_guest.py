"""Load the fixed guest text closure as source, without repository packages.

This is a guest-only compatibility adapter, never host/account authorization.
The qualified controller must build and mount the accepted complete closure.
"""

import os
from pathlib import Path
import stat
import sys
import types
import uuid

CONTEXT = Path("/input/codex-automation.json")
SOURCE = Path(__file__).resolve().parents[1] / "ci"
FILES = ("codex_automation_context", "codex_review_models", "codex_text_response",
         "codex_subscription_text", "codex_automation_adapter")


def runtime():
    if os.environ.get("CODEX_AUTOMATION_MODE") != "required":
        raise RuntimeError("subscription_mode_required")
    if os.environ.get("GITHUB_ACTIONS") == "true":
        raise RuntimeError("subscription_isolated_guest_required")
    if any(os.environ.get(k) for k in os.environ
           if "DOPPLER" in k or k == "GH_PAT" or k.endswith(
               ("_TOKEN", "_API_KEY", "_MASTER_KEY", "_PRIVATE_KEY", "_SECRET_KEY", "_PASSWORD"))):
        raise RuntimeError("subscription_isolated_guest_required")
    try:
        info = CONTEXT.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o222:
            raise ValueError("context not immutable")
    except (OSError, ValueError):
        raise RuntimeError("subscription_isolated_guest_required") from None
    name = "_subscription_guest_" + uuid.uuid4().hex
    package = types.ModuleType(name)
    package.__path__ = [str(SOURCE)]
    package.__package__ = name
    sys.modules[name] = package
    loaded = [name]
    try:
        for leaf in FILES:
            qualified = name + "." + leaf
            path = SOURCE / (leaf + ".py")
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_size > 262144:
                raise OSError("invalid guest source")
            module = types.ModuleType(qualified)
            module.__file__ = str(path)
            module.__package__ = name
            sys.modules[qualified] = module
            loaded.append(qualified)
            setattr(package, leaf, module)
            # No ci/__init__.py, ambient module or candidate bytecode cache.
            source = path.read_bytes()
            if len(source) > 262144:
                raise OSError("guest source exceeds bound")
            exec(compile(source, str(path), "exec"), module.__dict__)
        return package.codex_automation_adapter
    except (OSError, ImportError, SyntaxError):
        raise RuntimeError("subscription_guest_runtime_unavailable") from None
    finally:
        for qualified in loaded:
            sys.modules.pop(qualified, None)
