"""Synthetic bootstrap cases exercised by the legacy registry test entrypoint."""
from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

def _load_bootstrap():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "bootstrap_mirror", "scripts/bootstrap-mirror.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_bootstrap_ssh_effective_config(monkeypatch, tmp_path):
    """_ssh_host_unchanged must refuse any effective-config evidence of
    redirection or disabled server authentication, and must not trust a
    PATH-shadowing ssh executable to attest to itself."""
    import subprocess as _sp
    import types
    mod = _load_bootstrap()
    calls = []

    class R:
        def __init__(self, out, rc=0, err=""):
            self.stdout, self.returncode, self.stderr = out, rc, err

    def patch(lines, which="/usr/bin/ssh"):
        calls.clear()

        def run(argv, **_kw):
            calls.append(argv)
            return R("".join(f"{k} {v}\n" for k, v in lines))

        monkeypatch.setattr(
            mod, "subprocess",
            types.SimpleNamespace(
                run=run, TimeoutExpired=_sp.TimeoutExpired))
        monkeypatch.setattr(
            mod, "shutil", types.SimpleNamespace(which=lambda _n: which))

    CLEAN = [("hostname", "github.com"),
             ("stricthostkeychecking", "ask"),
             ("userknownhostsfile", "~/.ssh/known_hosts")]
    URL = "git@github.com:Buro-Built/buro-registry.git"

    patch(CLEAN)
    assert mod._ssh_host_unchanged(URL) is None
    # git invokes 'ssh -G <user>@<host>' for an scp-style URL; '-v'
    # traces the config sources ssh actually read.
    assert calls == [["/usr/bin/ssh", "-G", "-v", "git@github.com"]]

    patch(CLEAN)
    assert mod._ssh_host_unchanged(
        "github.com:Buro-Built/buro-registry.git") is None
    assert calls[-1] == ["/usr/bin/ssh", "-G", "-v", "github.com"]

    # The -G target carries the URL's port and the RAW user — git hands
    # ssh userinfo byte-for-byte (no percent-decoding), so 'git%40x' is
    # a different user than 'git@x' to a Match user block. Encoded or
    # control-char users cannot be attested → refused.
    patch(CLEAN)
    assert mod._ssh_host_unchanged(
        "ssh://redir%65ct@github.com:2222/Buro-Built/buro-registry.git"
    ) is not None
    assert "cannot be attested" in mod._ssh_host_unchanged(
        "ssh://redir%65ct@github.com:2222/Buro-Built/buro-registry.git")
    # A plain user with a port probes exactly what git would invoke.
    patch(CLEAN)
    assert mod._ssh_host_unchanged(
        "ssh://redirect@github.com:2222/Buro-Built/buro-registry.git"
    ) is None
    assert calls[-1] == ["/usr/bin/ssh", "-G", "-v", "-p", "2222",
                         "redirect@github.com"]

    patch([(k, "evil.example.test" if k == "hostname" else v)
           for k, v in CLEAN])
    assert "redirects" in mod._ssh_host_unchanged(URL)

    patch(CLEAN + [("proxycommand", "ssh -W %h:%p bastion")])
    assert "ProxyCommand" in mod._ssh_host_unchanged(URL)

    patch(CLEAN + [("proxyjump", "bastion")])
    assert "ProxyCommand" in mod._ssh_host_unchanged(URL)

    for shkc in ("no", "off", "false", "0", "accept-new"):
        patch(CLEAN + [("stricthostkeychecking", shkc)])
        assert "host key" in mod._ssh_host_unchanged(URL), shkc
    # HostKeyAlias swaps the name used for host-key lookup while
    # hostname still reports github.com — refused unless unset or the
    # identity alias.
    patch(CLEAN + [("hostkeyalias", "attacker.example")])
    assert "HostKeyAlias" in mod._ssh_host_unchanged(URL)
    patch(CLEAN + [("hostkeyalias", "github.com")])
    assert mod._ssh_host_unchanged(URL) is None
    # A KnownHostsCommand supplies host keys beyond the files —
    # refused unless unset/'none'.
    patch(CLEAN + [("knownhostscommand", "emit-attacker-key")])
    assert "KnownHostsCommand" in mod._ssh_host_unchanged(URL)
    patch(CLEAN + [("knownhostscommand", "none")])
    assert mod._ssh_host_unchanged(URL) is None

    patch(CLEAN + [("userknownhostsfile", "/dev/null"),
                   ("globalknownhostsfile", "/dev/null")])
    assert "known-hosts" in mod._ssh_host_unchanged(URL)

    # A custom known-hosts path can point at an attacker-seeded file —
    # only ~/.ssh and /etc/ssh locations are trusted.
    patch(CLEAN + [("userknownhostsfile", "/tmp/attacker_hosts")])
    assert "untrusted" in mod._ssh_host_unchanged(URL)
    patch(CLEAN + [("userknownhostsfile", "/etc/ssh/ssh_known_hosts")])
    assert mod._ssh_host_unchanged(URL) is None

    # An approved LOCATION is not proof of KEY identity: ssh accepts
    # any matching entry, so every github.com entry under ~/.ssh must
    # fingerprint to GitHub's published host keys — content is checked,
    # not just the directory the file lives in.
    import base64
    import hmac
    home = tmp_path / "h"
    kh_file = home / ".ssh" / "attacker_hosts"
    kh_file.parent.mkdir(parents=True)
    monkeypatch.setattr(mod, "pwd", types.SimpleNamespace(
        getpwuid=lambda _uid: types.SimpleNamespace(
            pw_dir=str(home), pw_name="u")))
    bad = base64.b64encode(b"forged-attacker-key-blob").decode()
    kh_file.write_text(f"github.com ssh-rsa {bad}\n")
    patch([(k, str(kh_file) if k == "userknownhostsfile" else v)
           for k, v in CLEAN])
    assert "published" in mod._ssh_host_unchanged(URL)

    # The pinned set holds GitHub's real published fingerprints.
    assert "SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU" \
        in mod._GITHUB_HOST_KEY_SHA256
    assert "SHA256:uNiVztksCsDhcc0u9e8BujQXVUpKZIDTMczCvj3tD2s" \
        in mod._GITHUB_HOST_KEY_SHA256
    assert "SHA256:p2QAMXNIC1TJYWeIOttrVc98/R1BUFWu3/LiyKgUfQM" \
        in mod._GITHUB_HOST_KEY_SHA256

    # A github.com entry whose blob fingerprints to the pinned set
    # anchors the host — patch the pinned set to a test blob's digest.
    blob = b"fake-github-key-blob"
    fp = "SHA256:" + base64.b64encode(
        hashlib.sha256(blob).digest()).decode().rstrip("=")
    monkeypatch.setattr(mod, "_GITHUB_HOST_KEY_SHA256", {fp})
    good = base64.b64encode(blob).decode()
    kh_file.write_text(f"github.com ssh-rsa {good}\n")
    patch([(k, str(kh_file) if k == "userknownhostsfile" else v)
           for k, v in CLEAN])
    assert mod._ssh_host_unchanged(URL) is None

    # A certificate-authority entry covering github.com lets a CA sign
    # any host key — refused even beside a pinned key.
    kh_file.write_text(f"github.com ssh-rsa {good}\n"
                       f"@cert-authority github.com ssh-rsa {bad}\n")
    patch([(k, str(kh_file) if k == "userknownhostsfile" else v)
           for k, v in CLEAN])
    assert "certificate-authority" in mod._ssh_host_unchanged(URL)

    # Hashed known-hosts entries (HashKnownHosts) resolve via HMAC-SHA1
    # — a forged hashed github.com entry refuses the same way.
    salt = b"somesalt"
    tok = "|1|" + base64.b64encode(salt).decode() + "|" + \
        base64.b64encode(hmac.new(
            salt, b"github.com", hashlib.sha1).digest()).decode()
    kh_file.write_text(
        f"{tok} ssh-rsa {base64.b64encode(b'attacker-hashed').decode()}\n")
    patch([(k, str(kh_file) if k == "userknownhostsfile" else v)
           for k, v in CLEAN])
    assert "published" in mod._ssh_host_unchanged(URL)

    # No github.com entry anywhere → interactive TOFU stands (shkc=ask
    # already passed) — not refused.
    kh_file.write_text(f"other.example ssh-rsa {bad}\n")
    patch([(k, str(kh_file) if k == "userknownhostsfile" else v)
           for k, v in CLEAN])
    assert mod._ssh_host_unchanged(URL) is None

    # Non-default effective port: 'Host github.com / Port 443' makes
    # ssh look up '[github.com]:443' — a forged key under that hashed
    # token must fail too (the EFFECTIVE port, not the URL's).
    salt = b"portsalt"
    tok443 = "|1|" + base64.b64encode(salt).decode() + "|" + \
        base64.b64encode(hmac.new(
            salt, b"[github.com]:443", hashlib.sha1).digest()).decode()
    kh_file.write_text(
        f"{tok443} ssh-rsa {base64.b64encode(b'forged-443').decode()}\n")
    patch([(k, str(kh_file) if k == "userknownhostsfile" else v)
           for k, v in CLEAN] + [("port", "443")])
    assert "published" in mod._ssh_host_unchanged(URL)

    # A padded known-hosts file beyond the verification cap is refused
    # outright — skipping it would still let ssh read a forged entry.
    kh_file.write_text(f"github.com ssh-rsa {bad}\n" + "#" * (8 << 20))
    patch([(k, str(kh_file) if k == "userknownhostsfile" else v)
           for k, v in CLEAN])
    assert "size" in mod._ssh_host_unchanged(URL)

    # A quoted path containing spaces is emitted WITHOUT its quoting —
    # 'one two' parses as two absent fragments while ssh loads the
    # real spaced file. A spaced join naming an existing file is
    # ambiguous → refuse.
    (home / ".ssh").mkdir(exist_ok=True)
    (home / ".ssh" / "one two").write_text(f"github.com ssh-rsa {bad}\n")
    patch([(k, f"{home}/.ssh/one two"
           if k == "userknownhostsfile" else v) for k, v in CLEAN])
    assert "ambiguous" in mod._ssh_host_unchanged(URL)

    # PermitLocalCommand + LocalCommand executes a command locally
    # after connecting — an exfil path invisible to the host checks.
    patch([(k, str(kh_file) if k == "userknownhostsfile" else v)
           for k, v in CLEAN] + [("permitlocalcommand", "yes"),
                                 ("localcommand", "/bin/evil %h")])
    assert "local command" in mod._ssh_host_unchanged(URL)

    # 'Match exec' in ANY config source ssh read is state-dependent
    # local execution — a condition false now can be true at push
    # time, so -G's snapshot cannot attest it. The -v debug trace
    # names the sources; 'Match host exec.example.com' is a hostname
    # pattern and must NOT trip the scan.
    cfg = tmp_path / "ssh_match_exec"
    cfg.write_text('Match exec "test -f /tmp/marker"\n'
                   '  HostName attacker.example\n'
                   'Match host exec.example.com\n')

    def run_v(argv, **_kw):
        return R("hostname github.com\nstricthostkeychecking ask\n"
                 "userknownhostsfile ~/.ssh/known_hosts\n",
                 err=(f"debug1: Reading configuration data {cfg}\n"))
    monkeypatch.setattr(
        mod, "subprocess",
        types.SimpleNamespace(run=run_v,
                              TimeoutExpired=_sp.TimeoutExpired))
    monkeypatch.setattr(
        mod, "shutil",
        types.SimpleNamespace(which=lambda _n: "/usr/bin/ssh"))
    assert "Match exec" in mod._ssh_host_unchanged(URL)
    hostpat = tmp_path / "ssh_host_pattern"
    hostpat.write_text('Match host exec.example.com\n'
                       '  HostName attacker.example\n')
    assert mod._match_exec_in([str(hostpat)]) is False
    # OpenSSH accepts quoted criteria — Match "exec" must refuse too.
    qcfg = tmp_path / "ssh_quoted_exec"
    qcfg.write_text('Match "exec" "test -e /tmp/marker"\n')
    assert mod._match_exec_in([str(qcfg)]) is True
    # Optional '=' keyword separators: 'Match=exec cmd' and
    # 'Match exec="cmd"' are the same criterion to ssh.
    eqcfg = tmp_path / "ssh_eq_exec"
    eqcfg.write_text('Match=exec "test -e /tmp/marker"\n')
    assert mod._match_exec_in([str(eqcfg)]) is True
    eqcfg2 = tmp_path / "ssh_eq_exec2"
    eqcfg2.write_text('Match exec="test -e /tmp/marker"\n')
    assert mod._match_exec_in([str(eqcfg2)]) is True
    # A criterion name in ARGUMENT position is a value, not a
    # condition — 'Match host exec' targets a host literally named
    # exec and must not trip.
    argpos = tmp_path / "ssh_arg_pos"
    argpos.write_text("Match host exec\n  HostName attacker.example\n")
    assert mod._match_exec_in([str(argpos)]) is False
    # Flag criteria take no argument — 'final' must not swallow a
    # following 'exec' criterion as its value.
    flag = tmp_path / "ssh_flag_exec"
    flag.write_text('Match final exec "test -e /tmp/x"\n')
    assert mod._match_exec_in([str(flag)]) is True
    # Negated criteria: 'Match !exec "cmd"' is still state-dependent
    # execution — the command's result can differ between check and
    # push, so it refuses; a negated VALUE ('Match host
    # !exec.example.com') is a pattern and stays allowed.
    neg = tmp_path / "ssh_neg_exec"
    neg.write_text('Match !exec "test ! -e /tmp/marker"\n')
    assert mod._match_exec_in([str(neg)]) is True
    negval = tmp_path / "ssh_neg_val"
    negval.write_text('Match host !exec.example.com\n')
    assert mod._match_exec_in([str(negval)]) is False
    # GlobalKnownHostsFile 'none' disables the global file — the
    # remaining user file is still validated, not refused as a path.
    kh_file.write_text(f"github.com ssh-rsa {good}\n")
    patch([(k, str(kh_file) if k == "userknownhostsfile" else v)
           for k, v in CLEAN] + [("globalknownhostsfile", "none")])
    assert mod._ssh_host_unchanged(URL) is None

    # Provider libraries dlopen during authentication — non-default
    # PKCS11Provider/SecurityKeyProvider refuse, defaults pass.
    patch([(k, str(kh_file) if k == "userknownhostsfile" else v)
           for k, v in CLEAN] + [("pkcs11provider", "/tmp/libp11.so")])
    assert "PKCS" in mod._ssh_host_unchanged(URL)
    patch([(k, str(kh_file) if k == "userknownhostsfile" else v)
           for k, v in CLEAN] + [("securitykeyprovider", "/tmp/sk.so")])
    assert "security-key" in mod._ssh_host_unchanged(URL)
    patch([(k, str(kh_file) if k == "userknownhostsfile" else v)
           for k, v in CLEAN] + [("securitykeyprovider", "internal")])
    assert mod._ssh_host_unchanged(URL) is None

    # ControlMaster attaches the push to an existing session — the
    # peer may not be github.com though -G reads clean. Refuse any
    # enabled form; 'no' (the default) passes.
    patch([(k, str(kh_file) if k == "userknownhostsfile" else v)
           for k, v in CLEAN]
          + [("controlmaster", "auto"), ("controlpath", "/tmp/cm-%r@%h:%p")])
    assert "multiplex" in mod._ssh_host_unchanged(URL)
    patch([(k, str(kh_file) if k == "userknownhostsfile" else v)
           for k, v in CLEAN] + [("controlmaster", "no")])
    assert mod._ssh_host_unchanged(URL) is None
    # Reaching the source cap must fail closed — a partial scan can
    # leave a 'Match exec' in an unscanned Include'd file.
    benign = tmp_path / "ssh_benign"
    benign.write_text("Host *\n")
    assert mod._match_exec_in([str(benign)] * 64) is False
    assert mod._match_exec_in([str(benign)] * 65) is True

    # A PATH-resolved binary outside the system dirs earns no trust —
    # a wrapper can attest to itself.
    monkeypatch.setattr(
        mod, "shutil",
        types.SimpleNamespace(
            which=lambda _n: str(tmp_path / "nogit")))
    assert mod._trusted_prog("git") == ""
    monkeypatch.setattr(
        mod, "shutil",
        types.SimpleNamespace(which=lambda _n: "/usr/bin/ssh"))
    assert mod._trusted_prog("ssh").startswith("/usr/")

    # Windows: the trust root is OS-derived (_windows_dir → kernel32),
    # never the caller-controlled SystemRoot env. When the OS cannot
    # answer there is NO trusted directory — a system-looking ssh
    # still refuses, and on POSIX _windows_dir reports nothing.
    assert mod._windows_dir() == ""
    monkeypatch.setattr(mod, "_windows_dir", lambda: "")
    monkeypatch.setattr(mod.os, "name", "nt")
    patch(CLEAN)
    assert "cannot be verified" in mod._ssh_host_unchanged(URL)
    monkeypatch.setattr(mod.os, "name", "posix")

    # A PATH-resolved ssh outside the system dirs is never even asked.
    patch(CLEAN, which="/tmp/evil/ssh")
    assert "cannot be verified" in mod._ssh_host_unchanged(URL)
    assert calls == []
