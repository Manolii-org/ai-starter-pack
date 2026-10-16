"""Native subscription execution: protocol, isolation and paid-work recovery."""
import base64
import importlib.util
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/muse-subscription.py"
spec = importlib.util.spec_from_file_location("muse_subscription", SCRIPT)
muse = importlib.util.module_from_spec(spec)
spec.loader.exec_module(muse)


def event(state="completed", text="Repository example; task complete."):
    return {"schema_version": 1, "record_type": "event", "payload_type": "run.terminal." + state,
            "payload": {"kind": "run_terminal", "terminal": state, "text": text}}


def setup(tmp_path, records=None, *, pause=0):
    repo, binary = tmp_path / "repo", tmp_path / "bin"
    repo.mkdir()
    binary.mkdir()
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Read the repository instructions and complete the approved task.")
    calls = tmp_path / "calls.txt"
    executable = binary / "muse"
    executable.write_text(
        '#!/usr/bin/env python3\nimport os,sys,json,time\nfrom pathlib import Path\n'
        'if "--help" in sys.argv:\n print("Safety (approval and the sandbox are ON by default):");sys.exit(0)\n'
        f'with open({str(calls)!r},"a") as f:f.write("version\\n" if "--version" in sys.argv else "exec\\n")\n'
        'if "--version" in sys.argv:\n assert "META_API_KEY" not in os.environ\n print("muse 1.4.9");sys.exit(0)\n'
        'print(json.dumps({"env":dict(os.environ),"argv":sys.argv}),file=sys.stderr)\n'
        + ''.join(f'print({json.dumps(row)!r},flush=True)\n' for row in (records if records is not None else [event()]))
        + f'time.sleep({pause})\n'
    )
    executable.chmod(0o700)
    args = SimpleNamespace(operation_id="op-1", prompt_file=prompt, output_dir=tmp_path / "artifacts",
                           workdir=repo, dry_run=False, read_only=False, max_model_steps=8,
                           reasoning_effort="high", timeout_seconds=5)
    env = {"PATH": str(binary) + os.pathsep + os.environ["PATH"], "MUSE_CODE_SUBSCRIPTION_KEY": "fixture-subscription-key"}
    return args, env, calls


def test_completed_task_preserves_final_and_strips_parent_credentials(tmp_path):
    args, env, _ = setup(tmp_path)
    env.update(GH_TOKEN="fixture-github", DOPPLER_TOKEN="fixture-doppler", DATABASE_URL="postgres://fixture",
               SSH_AUTH_SOCK="/tmp/fixture.sock", AZURE_CLIENT_SECRET="fixture-azure", RUNNER_TRACKING_ID="fixture-runner")
    assert muse.run(args, env) == 0
    receipt = json.loads((args.output_dir / "receipt.json").read_text())
    observed = json.loads((args.output_dir / "stderr.txt").read_text())
    assert receipt["provider_terminal"] == "completed" and receipt["final_message_available"]
    assert receipt["allow_usage_priced_api"] is False
    assert observed["env"]["META_API_KEY"] == env["MUSE_CODE_SUBSCRIPTION_KEY"]
    assert observed["env"]["RUNNER_TRACKING_ID"] == "fixture-runner"
    assert not any(key in observed["env"] for key in ("GH_TOKEN", "DOPPLER_TOKEN", "DATABASE_URL", "SSH_AUTH_SOCK", "AZURE_CLIENT_SECRET"))
    assert "--disable-request-user-input" in observed["argv"] and "--disable-sandbox" not in observed["argv"]
    assert observed["argv"][observed["argv"].index("--contributor-mode") + 1] == "off"
    assert (args.output_dir.stat().st_mode & 0o777) == 0o700
    assert (args.output_dir / "receipt.json").stat().st_mode & 0o777 == 0o600


def test_completed_operation_is_reused_without_second_paid_execution(tmp_path):
    args, env, calls = setup(tmp_path)
    assert muse.run(args, env) == 0
    args.workdir.rmdir()  # Historical reuse is independent of a disposed checkout.
    assert muse.run(args, {}) == 0
    assert calls.read_text().splitlines() == ["version", "exec"]


def test_relative_path_discovery_survives_a_different_target_checkout(tmp_path, monkeypatch):
    args, env, _ = setup(tmp_path)
    monkeypatch.chdir(tmp_path)
    env["PATH"] = "." + os.sep + "bin" + os.pathsep + os.environ["PATH"]
    assert args.workdir != Path.cwd()
    assert muse.run(args, env) == 0
    observed = json.loads((args.output_dir / "stderr.txt").read_text())
    assert Path(observed["argv"][0]).is_absolute()


def test_reuse_requires_identical_execution_controls(tmp_path):
    args, env, calls = setup(tmp_path)
    assert muse.run(args, env) == 0
    args.reasoning_effort = "low"
    with pytest.raises(ValueError, match="reconciliation"):
        muse.run(args, env)
    assert calls.read_text().splitlines() == ["version", "exec"]
    args.prompt_file.write_text("A different task")
    with pytest.raises(ValueError, match="reconciliation"):
        muse.run(args, env)
    assert calls.read_text().splitlines() == ["version", "exec"]


@pytest.mark.parametrize("records", [[], [event("failed")], [event(text="")],
                                     [{**event(), "payload_type": "run.terminal.failed"}]])
def test_exit_zero_without_usable_completed_terminal_fails(tmp_path, records):
    args, env, _ = setup(tmp_path, records)
    assert muse.run(args, env) == 2
    assert json.loads((args.output_dir / "receipt.json").read_text())["status"] == "failed"


def test_final_redacts_the_subscription_key_and_common_encoding(tmp_path):
    key = "fixture-subscription-key"
    args, env, _ = setup(tmp_path, [event(text=key + " " + base64.b64encode(key.encode()).decode())])
    assert muse.run(args, env) == 0
    assert (args.output_dir / "final.md").read_text() == "[REDACTED] [REDACTED]"


def test_readonly_canary_disables_write_and_shell_tools(tmp_path):
    args, env, _ = setup(tmp_path)
    args.read_only = True
    assert muse.run(args, env) == 0
    argv = json.loads((args.output_dir / "stderr.txt").read_text())["argv"]
    assert "--disable-write" in argv and "--disable-shell" in argv


def test_timeout_retains_partial_output_and_never_claims_completion(tmp_path):
    args, env, _ = setup(tmp_path, [{"payload_type": "run.output.delta", "payload": {"text": "partial work"}}], pause=60)
    args.timeout_seconds = 1
    assert muse.run(args, env) == 2
    assert (args.output_dir / "final.md").read_text() == "partial work"
    assert json.loads((args.output_dir / "receipt.json").read_text())["status"] == "timed_out"


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group cancellation")
def test_parent_cancellation_stops_inference_and_saves_partial_receipt(tmp_path):
    args, env, calls = setup(tmp_path, [{"payload_type": "run.output.delta", "payload": {"text": "partial work"}}], pause=60)
    process = subprocess.Popen([sys.executable, str(SCRIPT), "--operation-id", args.operation_id,
                                "--prompt-file", str(args.prompt_file), "--output-dir", str(args.output_dir),
                                "--workdir", str(args.workdir)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        for _ in range(40):
            if calls.exists() and "exec" in calls.read_text():
                break
            time.sleep(0.05)
        else:
            pytest.fail("fixture inference did not start")
        process.send_signal(signal.SIGTERM)
        process.communicate(timeout=8)
        receipt = json.loads((args.output_dir / "receipt.json").read_text())
        assert process.returncode == 2 and receipt["status"] == "interrupted"
        assert receipt["exit_code"] == 143
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)


@pytest.mark.parametrize("name", muse.FORBIDDEN)
def test_payg_shadows_fail_before_any_cli_request(tmp_path, name):
    args, env, calls = setup(tmp_path)
    env[name] = "fixture-payg"
    with pytest.raises(ValueError, match="usage-priced"):
        muse.run(args, env)
    assert not calls.exists() and not args.output_dir.exists()


def test_missing_tenant_credential_cannot_borrow_other_keys(tmp_path):
    args, env, calls = setup(tmp_path)
    env.pop("MUSE_CODE_SUBSCRIPTION_KEY")
    env["OTHER_TENANT_SUBSCRIPTION_KEY"] = "fixture-foreign"
    with pytest.raises(ValueError, match="Tenant-owned"):
        muse.run(args, env)
    assert not calls.exists()


def test_dryrun_has_no_model_turn_or_artifact_writes(tmp_path):
    args, env, calls = setup(tmp_path)
    args.dry_run = True
    assert muse.run(args, env) == 0
    assert calls.read_text().splitlines() == ["version"]
    assert not args.output_dir.exists()


def test_terminal_after_large_trace_is_not_lost(tmp_path):
    path = tmp_path / "events.jsonl"
    with path.open("w") as output:
        for _ in range(17):
            output.write("x" * (1 << 20) + "\n")
        output.write(json.dumps(event()) + "\n")
    assert muse.terminal_output(path, "")[0] == "completed"


def test_large_completed_terminal_is_retained_in_full(tmp_path):
    text = "completed answer " * (1 << 17)
    path = tmp_path / "events.jsonl"
    path.write_text(json.dumps(event(text=text)) + "\n")
    assert muse.terminal_output(path, "") == ("completed", text)


def test_old_cli_is_blocked_before_credential_injection(tmp_path):
    args, env, calls = setup(tmp_path)
    cli = Path(env["PATH"].split(os.pathsep)[0]) / "muse"
    cli.write_text(cli.read_text().replace("muse 1.4.9", "muse 1.3.0"))
    with pytest.raises(ValueError, match="1.4.9 or newer"):
        muse.run(args, env)
    assert calls.read_text().splitlines() == ["version"]
    assert not args.output_dir.exists()


def test_partial_deltas_cannot_make_empty_completed_terminal_succeed(tmp_path):
    partial = "partial " * (1 << 14)
    args, env, _ = setup(tmp_path, [{"payload_type": "run.output.delta", "payload": {"text": partial}}, event(text="")])
    assert muse.run(args, env) == 2
    assert json.loads((args.output_dir / "receipt.json").read_text())["status"] == "failed"
    assert (args.output_dir / "final.md").read_text() == partial[-65536:]


def test_official_muse_code_version_banner_is_supported(tmp_path):
    args, env, _ = setup(tmp_path)
    cli = Path(env["PATH"].split(os.pathsep)[0]) / "muse"
    cli.write_text(cli.read_text().replace("muse 1.4.9", "Muse Code 1.4.9 (1.4.9-R6297.1)"))
    assert muse.run(args, env) == 0


def test_common_encoded_keys_are_removed_from_final_output():
    from urllib.parse import quote
    key = "fixture+key/value="
    encoded = base64.b64encode(key.encode()).decode().rstrip("=")
    assert muse.safe_text(quote(key, safe="") + " " + encoded, key) == "[REDACTED] [REDACTED]"


def test_dryrun_revalidates_setup_instead_of_reusing_historical_success(tmp_path):
    args, env, calls = setup(tmp_path)
    assert muse.run(args, env) == 0
    before = (args.output_dir / "receipt.json").read_bytes()
    args.dry_run = True
    with pytest.raises(ValueError, match="not provisioned"):
        muse.run(args, {})
    assert muse.run(args, env) == 0
    assert (args.output_dir / "receipt.json").read_bytes() == before
    assert calls.read_text().splitlines() == ["version", "exec", "version"]


def test_oversized_record_is_explicit_failure_with_raw_trace_available(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text(json.dumps(event(text="x" * (muse.MAX_EVENT_RECORD_BYTES + 1))) + "\n")
    assert muse.terminal_output(path, "")[0] == "output_limit_exceeded"
    assert path.stat().st_size > muse.MAX_EVENT_RECORD_BYTES


@pytest.mark.skipif(os.name != "posix", reason="POSIX child signal return codes")
def test_child_signal_is_interrupted_with_partial_output(tmp_path):
    args, env, _ = setup(tmp_path, [{"payload_type": "run.output.delta", "payload": {"text": "partial"}}])
    cli = Path(env["PATH"].split(os.pathsep)[0]) / "muse"
    cli.write_text(cli.read_text() + "import signal\nos.kill(os.getpid(), signal.SIGTERM)\n")
    assert muse.run(args, env) == 2
    receipt = json.loads((args.output_dir / "receipt.json").read_text())
    assert receipt["status"] == "interrupted" and receipt["exit_code"] == -signal.SIGTERM
    assert (args.output_dir / "final.md").read_text() == "partial"


def test_malformed_existing_receipt_is_structured_reconciliation_failure(tmp_path):
    args, env, calls = setup(tmp_path)
    assert muse.run(args, env) == 0
    (args.output_dir / "receipt.json").write_text("[]")
    with pytest.raises(ValueError, match="reconciliation"):
        muse.run(args, {})
    assert calls.read_text().splitlines() == ["version", "exec"]


def test_unicode_completed_result_is_reused_on_non_utf8_hosts(tmp_path, monkeypatch):
    args, env, calls = setup(tmp_path, [event(text="Completed — résumé ✅")])
    assert muse.run(args, env) == 0
    original = Path.read_text

    def locale_read(path, *positional, **kwargs):
        kwargs.setdefault("encoding", "ascii")
        return original(path, *positional, **kwargs)

    monkeypatch.setattr(Path, "read_text", locale_read)
    assert muse.run(args, {}) == 0
    assert calls.read_text().splitlines() == ["version", "exec"]


@pytest.mark.skipif(os.name != "posix", reason="POSIX process group race")
def test_exit_between_wait_and_forced_kill_still_completes_cleanup(monkeypatch):
    process = SimpleNamespace(pid=42, wait=Mock(side_effect=[subprocess.TimeoutExpired("muse", 5), 0]))
    kill = Mock(side_effect=[None, ProcessLookupError()])
    monkeypatch.setattr(muse.os, "killpg", kill)
    muse.stop_process(process)
    assert process.wait.call_count == 2
    assert kill.call_args_list == [((process.pid, signal.SIGTERM), {}), ((process.pid, signal.SIGKILL), {})]


def test_missing_workdir_is_blocked_before_setup_or_receipt(tmp_path):
    args, env, calls = setup(tmp_path)
    args.workdir = tmp_path / "missing"
    with pytest.raises(ValueError, match="existing directory"):
        muse.run(args, env)
    assert not calls.exists() and not args.output_dir.exists()


def test_executable_disappearing_after_preflight_saves_startup_failure(tmp_path):
    args, env, calls = setup(tmp_path)
    cli = Path(env["PATH"].split(os.pathsep)[0]) / "muse"
    statement = 'print("Safety (approval and the sandbox are ON by default):");sys.exit(0)'
    cli.write_text(cli.read_text().replace(statement, statement.replace(";sys.exit(0)", ";Path(__file__).unlink();sys.exit(0)")))
    assert muse.run(args, env) == 2
    saved = json.loads((args.output_dir / "receipt.json").read_text())
    assert saved["status"] == "startup_failed" and saved["startup_error"] == "FileNotFoundError"
    assert saved["final_message_available"] is False
    assert calls.read_text().splitlines() == ["version"]


def test_provider_executes_hashed_snapshot_when_original_prompt_changes(tmp_path, monkeypatch):
    args, env, _ = setup(tmp_path)
    expected, execute = args.prompt_file.read_text(), muse.execute

    def change_source(command, *positional):
        args.prompt_file.write_text("Concurrent edit must not become this paid operation.")
        snapshot = Path(command[command.index("--prompt-file") + 1])
        assert snapshot != args.prompt_file and snapshot.read_text() == expected
        assert snapshot.stat().st_mode & 0o777 == 0o600
        return execute(command, *positional)

    monkeypatch.setattr(muse, "execute", change_source)
    assert muse.run(args, env) == 0
    assert json.loads((args.output_dir / "receipt.json").read_text())["prompt_sha256"] == muse.digest(expected)
