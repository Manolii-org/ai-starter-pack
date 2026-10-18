"""Opt-in actual pinned-tool/container qualification; no real model or credential.

Run CODEX_REVIEW_DOCKER_TESTS=1 pytest -q <this file> on an isolated Docker host.
Synthetic model output qualifies transport/tool execution, never source security.
"""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/ci"))
import codex_review_worker as worker  # noqa: E402
from codex_review_broker import BufferedResponse, Subscription, serve  # noqa: E402

pytestmark = pytest.mark.skipif(
    os.environ.get("CODEX_REVIEW_DOCKER_TESTS") != "1",
    reason="requires explicitly selected isolated Docker host; no live provider",
)


@pytest.mark.parametrize("lane", ["code", "security"])
def test_actual_pinned_tool_execution_and_outer_boundary(tmp_path, monkeypatch, lane):
    monkeypatch.setenv("SYNTHETIC_HOST_SECRET", "synthetic-host-environment-marker")
    source = tmp_path / "input"
    source.mkdir()

    def git(*args):
        return subprocess.check_output(["git", *args], cwd=source, text=True).strip()

    git("init", "-q")
    git("config", "user.name", "Synthetic qualification")
    git("config", "user.email", "fixture@example.invalid")
    (source / "auth.py").write_text("def authorize():\n    return False\n")
    git("add", ".")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    private = tmp_path / "host-private"
    private.write_text("synthetic-private-host-marker")
    (source / "host-link").symlink_to(private)
    (source / "auth.py").write_text("def authorize():\n    return True\n")
    git("add", ".")
    git("commit", "-qm", "head")
    head = git("rev-parse", "HEAD")
    fork_point = base
    git("checkout", "-qb", "advanced-base", fork_point)
    (source / "base-only.py").write_text("# Unrelated later main change.\n")
    git("add", ".")
    git("commit", "-qm", "base advanced independently")
    base = git("rev-parse", "HEAD")
    git("checkout", "--detach", "-q", head)
    image = worker.build_image(tmp_path)
    directory = tmp_path / "broker"
    directory.mkdir(mode=0o700)
    target = {"lane": lane, "base_sha": base, "head_sha": head}
    profile = worker.qualify_sandbox(image, source, directory, target)
    probe = """import json,os,socket
from pathlib import Path
checks = {
 "nonroot": os.getuid()!=0,
 "docker_absent": not Path('/var/run/docker.sock').exists(),
 "host_credentials_absent": not any(os.environ.get(k) for k in ['GH_TOKEN','GITHUB_TOKEN','DOPPLER_TOKEN','SYNTHETIC_HOST_SECRET']),
 "host_symlink_denied": not Path('/state/source/host-link').exists(),
 "no_new_privileges": any(x.split()==['NoNewPrivs:','1'] for x in Path('/proc/self/status').read_text().splitlines()),
 "capabilities_dropped": any(x.split()==['CapEff:','0000000000000000'] for x in Path('/proc/self/status').read_text().splitlines()),
}
try:
 socket.create_connection(('1.1.1.1',443),timeout=1).close()
 checks['external_network_denied']=False
except OSError:
 checks['external_network_denied']=True
assert all(checks.values()),checks
print('SYNTHETIC_BOUNDARY_OK',json.dumps(checks))
scan=Path(os.environ['CODEX_SECURITY_SCAN_DIR'])
import subprocess
inventory=scan/'runtime-inventory.txt'
subprocess.run(['/usr/bin/python3','/opt/security/node_modules/@openai/codex-security/_bundled_plugin/scripts/generate_in_scope_files.py','--repo','/state/source','--scope','.','--diff-base','BASE_FIXTURE_SHA','--diff-head','HEAD_FIXTURE_SHA','--out',str(inventory)],check=True)
assert inventory.read_text().splitlines()==['auth.py']
print('SYNTHETIC_INVENTORY_OK')
manifest={'scan':{'target':{'kind':os.environ['CODEX_SECURITY_TARGET_KIND']},'scope':{},'threatModel':{'format':'markdown','content':'Synthetic runtime fixture; no security assessment.','origin':'provided'}}}
coverage={'completeness':'complete','inventoryStrategy':'diff','surfaces':[{'id':'fixture','label':'Synthetic fixture only','disposition':'no_issue_found','receiptRefs':[]}],'explicitExclusions':[],'deferred':[]}
for name,value in [('scan-manifest.json',manifest),('findings.json',{'findings':[]}),('coverage.json',coverage)]:
 (scan/name).write_text(json.dumps(value))
"""
    probe = probe.replace('BASE_FIXTURE_SHA', fork_point).replace('HEAD_FIXTURE_SHA', head)
    if lane == "code":
        probe = probe.split("scan=Path(", 1)[0]
    requests = []
    host_auth = "synthetic-private-upstream-token-1234567890"

    def upstream(body, headers, **kwargs):
        assert headers["Authorization"] == "Bearer " + host_auth
        assert host_auth.encode() not in body
        payload = json.loads(body)
        requests.append(payload)
        message = {
            "type": "message",
            "id": "msg_fixture",
            "status": "completed",
            "role": "assistant",
            "content": [
                {"type": "output_text", "text": '{"findings":[]}', "annotations": []}
            ],
        }
        if len(requests) == 1:
            command = "/usr/bin/python3 - <<'PY'\n" + probe + "\nPY"
            workbench = ""
            if lane == "security":
                workbench = """
const name = ALL_TOOLS.find(x => /get_codex_security_scan_context$/.test(x.name))?.name;
if (!name) throw new Error('Bundled workbench tool is unavailable');
const meta = await tools.exec_command({cmd: `/usr/bin/python3 -c 'import os; print(os.environ["CODEX_SECURITY_SCAN_ID"])'`, max_output_tokens: 100});
const scanId = meta.output.trim();
if (!/^[0-9a-f]{8}-[0-9a-f-]{27}$/.test(scanId)) throw new Error('Invalid registered scan ID');
const context = await tools[name]({scanId});
if (context.isError || context.structuredContent?.scan?.scanId !== scanId || context.structuredContent.scan.progress?.status !== 'running') throw new Error('Registered scan context unavailable: ' + JSON.stringify(context));
if (context.structuredContent.scan.diffTarget?.baseRevision !== 'MERGE_BASE_FIXTURE_SHA' || context.structuredContent.scan.diffTarget?.headRevision !== 'HEAD_FIXTURE_SHA') throw new Error('SDK range differs from PR merge-base scope');
text('SYNTHETIC_WORKBENCH_OK');
"""
            workbench = workbench.replace('MERGE_BASE_FIXTURE_SHA', fork_point).replace('HEAD_FIXTURE_SHA', head)
            message = {
                "type": "custom_tool_call",
                "id": "ctc_fixture",
                "call_id": "call_fixture",
                "namespace": "functions",
                "name": "exec",
                "input": workbench + "text(await tools.exec_command("
                + json.dumps({"cmd": command, "max_output_tokens": 2000})
                + "));",
            }
        response = {
            "id": "resp_" + str(len(requests)),
            "object": "response",
            "created_at": 1,
            "status": "completed",
            "model": "gpt-5.6-sol",
            "output": [message],
            "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        }
        data = (
            "event: response.output_item.done\ndata: "
            + json.dumps(
                {
                    "type": "response.output_item.done",
                    "output_index": 0,
                    "item": message,
                }
            )
            + "\n\nevent: response.completed\ndata: "
            + json.dumps({"type": "response.completed", "response": response})
            + "\n\n"
        )
        return BufferedResponse(data.encode(), "text/event-stream")

    auth = Subscription(
        {
            "tokens": {
                "access_token": host_auth,
                "account_id": "synthetic-private-account",
            }
        }
    )
    with serve(
        directory / "model.sock", auth, transport=upstream, max_seconds=100
    ) as broker:
        result = worker.run_container(image, source, directory, target, profile=profile)
        assert broker.successful_turns == 2
    outputs = [
        v for v in requests[-1]["input"] if v.get("type") == "custom_tool_call_output"
    ]
    assert result["returncode"] == 0, (result, outputs)
    if lane == "security":
        assert json.loads(result["output"])["findings"] == []
    assert len(requests) == 2
    # The fake model must receive actual SDK tool output, not a mocked subprocess.
    assert "SYNTHETIC_BOUNDARY_OK" in json.dumps(outputs)
    if lane == "security":
        assert "SYNTHETIC_WORKBENCH_OK" in json.dumps(outputs)
        assert "SYNTHETIC_INVENTORY_OK" in json.dumps(outputs)
    assert host_auth not in json.dumps(result)
