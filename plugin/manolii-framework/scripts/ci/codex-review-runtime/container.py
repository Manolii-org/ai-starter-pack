"""Trusted entrypoint inside the outer credential-free, networkless container."""

import base64
from datetime import datetime, timezone
import http.server
import json
import os
from pathlib import Path
import selectors
import socket
import subprocess
import sys
import threading
import time

LIMIT = 1024 * 1024


class Relay(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *_):
        return

    def do_POST(self):
        if self.path != "/v1/responses" or self.headers.get("Transfer-Encoding"):
            self.send_error(400)
            return
        try:
            length = int(self.headers["Content-Length"])
            if not 0 < length <= 2 * LIMIT:
                raise ValueError("length")
            body = self.rfile.read(length)
            with socket.socket(socket.AF_UNIX) as remote:
                remote.settimeout(310)
                remote.connect("/broker/model.sock")
                remote.sendall(
                    b"POST /v1/responses HTTP/1.0\r\nContent-Type: application/json\r\nContent-Length: "
                    + str(len(body)).encode()
                    + b"\r\n\r\n"
                    + body
                )
                while block := remote.recv(16384):
                    self.connection.sendall(block)
        except (OSError, ValueError, TypeError):
            self.close_connection = True


def bounded(command, *, cwd, seconds=1100):
    with subprocess.Popen(
        command,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    ) as process:
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        blocks = []
        size = 0
        deadline = time.monotonic() + seconds
        try:
            while time.monotonic() < deadline:
                events = selector.select(
                    timeout=min(0.1, max(0, deadline - time.monotonic()))
                )
                if events:
                    block = os.read(process.stdout.fileno(), 16384)
                    if not block:
                        return process.wait(timeout=3), b"".join(blocks).decode(
                            errors="replace"
                        )
                    size += len(block)
                    if size > LIMIT:
                        return 1, "review_output_budget_exhausted"
                    blocks.append(block)
            return 1, "review_time_budget_exhausted"
        finally:
            selector.close()
            if process.poll() is None:
                import signal

                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=3)


def main():
    lane, base, head = sys.argv[1:]
    if lane not in {"code", "security"} or any(
        len(sha) != 40 or any(c not in "0123456789abcdef" for c in sha)
        for sha in (base, head)
    ):
        raise SystemExit("invalid target")
    state = Path("/state")
    home = state / "home"
    home.mkdir(mode=0o700)
    # Public, deliberately synthetic client state. The broker authenticates the
    # fixed upstream separately; this placeholder grants access to nothing.
    payload = {
        "sub": "public-review-placeholder",
        "exp": 4102444800,
        "email": "review@example.invalid",
        "https://api.openai.com/auth": {
            "chatgpt_account_id": "public-review-placeholder",
            "chatgpt_plan_type": "pro",
        },
    }
    token = (
        ".".join(
            base64.urlsafe_b64encode(json.dumps(item).encode()).decode().rstrip("=")
            for item in ({"alg": "none"}, payload)
        )
        + ".public-placeholder"
    )
    (home / "auth.json").write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "tokens": {
                    "access_token": token,
                    "id_token": token,
                    "refresh_token": "public-placeholder",
                    "account_id": "public-review-placeholder",
                },
                "last_refresh": datetime.now(timezone.utc)
                .isoformat()
                .replace("+00:00", "Z"),
            }
        )
    )
    os.environ.update(
        HOME=str(state),
        CODEX_HOME=str(home),
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL="/dev/null",
        GIT_CONFIG_COUNT="2",
        GIT_CONFIG_KEY_0="core.hooksPath",
        GIT_CONFIG_VALUE_0="/dev/null",
        GIT_CONFIG_KEY_1="core.fsmonitor",
        GIT_CONFIG_VALUE_1="false",
    )
    subprocess.run(
        [
            "git",
            "-c",
            "safe.directory=/input/repo",
            "clone",
            "--shared",
            "--no-checkout",
            "--no-hardlinks",
            "/input/repo",
            "/state/source",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    source = state / "source"
    subprocess.run(
        ["git", "checkout", "--detach", head],
        cwd=source,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if (
        subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=source, text=True
        ).strip()
        != head
    ):
        raise SystemExit("source revision mismatch")
    relay = http.server.HTTPServer(("127.0.0.1", 8123), Relay)
    threading.Thread(target=relay.serve_forever, daemon=True).start()
    config = [
        'model="gpt-5.6-sol"',
        'model_provider="review"',
        'model_providers.review={name="review",base_url="http://127.0.0.1:8123/v1",wire_api="responses",requires_openai_auth=false,supports_websockets=false,request_max_retries=0,stream_max_retries=0}',
        "analytics.enabled=false",
    ]
    if lane == "code":
        command = [
            "/opt/code/node_modules/.bin/codex",
            "exec",
            "--ignore-user-config",
            "--ignore-rules",
            "--ephemeral",
            "-C",
            str(source),
        ]
        for item in config + [
            "features.hooks=false",
            "features.plugins=false",
            "features.apps=false",
            "project_doc_max_bytes=0",
            'shell_environment_policy.inherit="none"',
        ]:
            command.extend(["-c", item])
        # The outer container is the security boundary. Keep the CLI's own
        # default sandbox; no approval/sandbox bypass is needed or requested.
        command.extend(["review", "--base", base])
    else:
        # The SDK registers the scan before model work, but its diff prompt
        # requires workbench tools. Register the bundled stdio server explicitly
        # against the same ephemeral state, outside the CLI tool sandbox and
        # inside the outer credential-free/networkless container.
        # The SDK compares two exact commits; PR scope is merge-base..head,
        # matching the host's three-dot admission even when main has advanced.
        scan_base = subprocess.check_output(
            ["git", "merge-base", base, head], cwd=source, text=True, timeout=30
        ).strip()
        if len(scan_base) != 40 or any(c not in "0123456789abcdef" for c in scan_base):
            raise SystemExit("invalid review merge base")
        os.environ["CODEX_SECURITY_STATE_DIR"] = "/state/security"
        command = [
            "/opt/security/node_modules/.bin/codex-security",
            "scan",
            str(source),
            "--diff",
            scan_base,
            "--head",
            head,
            "--auth",
            "chatgpt",
            "--headless",
            "--python",
            "/usr/bin/python3",
            "--output-dir",
            "/state/results",
        ]
        # The Security wrapper enables V2 independently of embedded CLI defaults.
        # Disable V1 and bound V2 to its documented parent-only fallback.
        for item in config + [
            "features.multi_agent=false",
            "features.multi_agent_v2.max_concurrent_threads_per_session=1",
            'mcp_servers.codex_security={command="/opt/security/node_modules/@openai/codex-security/_bundled_plugin/scripts/launch_codex_security_mcp",args=["--stdio"],startup_timeout_sec=120,tool_timeout_sec=300,env={CODEX_SECURITY_STATE_DIR="/state/security",PYTHON="/usr/bin/python3"}}',
        ]:
            command.extend(["--codex", item])
    rc, log = bounded(command, cwd=state)
    output = log
    if lane == "security":
        findings = state / "results/findings.json"
        output = (
            findings.read_text()
            if findings.is_file()
            and not findings.is_symlink()
            and findings.stat().st_size <= LIMIT
            else ""
        )
    print(
        json.dumps(
            {
                "returncode": rc,
                "output": output,
                "diagnostic": log[-2000:] if rc else "",
            }
        )
    )
    relay.shutdown()


if __name__ == "__main__":
    main()
