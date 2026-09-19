from scripts import issue_lifecycle as life


def issue(number=1, *, body="", bot=True, assignees=()):
    return {"number": number, "body": body, "comments_url": f"https://api.github.com/comments/{number}",
            "user": {"login": "github-actions[bot]" if bot else "human", "type": "Bot" if bot else "User"},
            "assignees": [{"login": a, "type": "User"} for a in assignees]}


def test_signal_comments_on_canonical_issue():
    calls = []
    existing = issue(body=life.marker("prod-build"))
    def http(url, method, token, payload=None):
        calls.append((url, method, payload))
        if method == "GET": return 200, [existing]
        return 201, {}
    assert life.signal("o", "r", "t", "prod-build", "bad", "details", ["incident"], "run", http) == ("commented", 1)
    assert calls[-1][0].endswith("/comments/1")


def test_recover_closes_only_unattended_machine_issue():
    calls = []
    issues = [issue(1, body=life.marker("deploy")), issue(2, body=life.marker("deploy"), assignees=("adrian",)), issue(3, body=life.marker("deploy"), bot=False)]
    def http(url, method, token, payload=None):
        calls.append((url, method, payload))
        if method == "GET": return 200, issues
        return (201 if url.endswith("/comments") else 200), {}
    assert life.recover("o", "r", "t", "deploy", "healthy", "run", http) == [1]
    assert [c[1] for c in calls] == ["GET", "POST", "PATCH"]


def test_select_uses_exact_stable_key_marker():
    assert life.select([issue(body=life.marker("a")), issue(2, body=life.marker("ab"))], "a")[0]["number"] == 1
