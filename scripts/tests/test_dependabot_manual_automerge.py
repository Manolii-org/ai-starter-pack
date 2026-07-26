import json
import subprocess
from pathlib import Path

WORKFLOW = Path('.github/workflows/dependabot-manual-automerge.yml')
FILTER = '''.state == "open" and .draft == false and
.user.login == "dependabot[bot]" and
.head.repo.full_name == .base.repo.full_name and .base.ref == "main" and
([.labels[].name] | index("security") == null)'''


def _jq(payload: dict) -> subprocess.CompletedProcess[str]:
    return subprocess.run(['jq', '-e', FILTER], input=json.dumps(payload), text=True, capture_output=True, check=False)


def _valid_pr() -> dict:
    return {'state':'open','draft':False,'user':{'login':'dependabot[bot]'},'head':{'repo':{'full_name':'x/r'}},'base':{'repo':{'full_name':'x/r'},'ref':'main'},'labels':[]}


def test_security_label_is_rejected_executably() -> None:
    payload = _valid_pr(); payload['labels'] = [{'name':'security'}]
    assert _jq(payload).returncode != 0


def test_valid_dependabot_pr_is_accepted_executably() -> None:
    assert _jq(_valid_pr()).returncode == 0


def test_workflow_uses_context_urls_and_hides_token_from_argv() -> None:
    text = WORKFLOW.read_text()
    assert '${{ github.api_url }}' in text and '${{ github.graphql_url }}' in text
    assert '--config "$auth_config"' in text
    assert '-H "Authorization:' not in text
    assert 'actions/checkout' not in text
