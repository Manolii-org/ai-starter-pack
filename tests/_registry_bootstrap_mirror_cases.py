"""Synthetic bootstrap cases exercised by the legacy registry test entrypoint."""
from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

def _bootstrap_env(tmp_path):
    """env for bootstrap subprocess calls: a `gh` stub reporting 'private'
    (the visibility check fails closed when gh can't answer), PATH
    including it, and MIRROR_TRUST_DIRS declaring the stub — a binary
    outside the system dirs is untrusted unless the operator declares
    the root."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    gh = bin_dir / "gh"
    gh.write_text("#!/bin/sh\necho \"$@\" > \"" + str(tmp_path / "gh_args")
                  + "\"\necho private\n")
    gh.chmod(0o755)
    # Isolate the harness machine's GLOBAL git config — e.g. a Devin box
    # rewrites every github.com url through its auth proxy via
    # url.insteadOf, which would silently steer push-target checks.
    empty_cfg = tmp_path / "gitconfig.empty"
    empty_cfg.write_text("")
    return dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}",
                GIT_CONFIG_GLOBAL=str(empty_cfg),
                MIRROR_TRUST_DIRS=os.pathsep.join(filter(None,
                    (str(bin_dir), os.environ.get("MIRROR_TRUST_DIRS", "")))))


def test_bootstrap_mirror_fails_closed(tmp_path):
    """Seeding refuses (rc 2, nothing written) when repo visibility cannot
    be verified or is public — and refuses at the staging step when --root
    is not a git checkout (an empty rglob-fallback allowlist would leave
    the first pushed CI run red)."""
    import subprocess as sp
    pack = Path(__file__).resolve().parent.parent

    def run(root, env, slug="buro-built/buro-registry", universe="buro"):
        return sp.run(
            [sys.executable, "scripts/bootstrap-mirror.py", "--root",
             str(root), "--universe", universe, "--slug", slug],
            cwd=pack, capture_output=True, text=True, env=env)

    # gh absent from PATH (git still resolvable) → visibility
    # unverifiable → refuse before writes.
    import shutil as _shutil
    root = tmp_path / "nogh"
    root.mkdir()
    sp.run(["git", "init", "-q"], cwd=root, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=root, capture_output=True)
    empty_bin = tmp_path / "emptybin"
    empty_bin.mkdir()
    os.symlink(_shutil.which("git"), empty_bin / "git")
    r = run(root, dict(os.environ, PATH=str(empty_bin)))
    assert r.returncode == 2, r.stderr
    assert not (root / "registry").exists()

    # A PATH-shadowing git (a real file, not a symlink into the
    # system dirs) cannot attest to itself — refuse before writes
    # even though the wrapper delegates honestly.
    eb = tmp_path / "evilbin"
    eb.mkdir()
    (eb / "git").write_text("#!/bin/sh\nexec /usr/bin/git \"$@\"\n")
    os.chmod(eb / "git", 0o755)
    ebroot = tmp_path / "ebroot"
    ebroot.mkdir()
    sp.run(["git", "init", "-q"], cwd=ebroot, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=ebroot, capture_output=True)
    r = run(ebroot,
            dict(os.environ,
                 PATH=f"{eb}{os.pathsep}{os.environ['PATH']}"))
    assert r.returncode == 2, r.stderr
    assert "trusted" in r.stderr or "origin" in r.stderr
    assert not (ebroot / "registry").exists()

    # --slug naming ANOTHER accessible repo → the digest would not bind
    # to this checkout's origin → refuse.
    mism = tmp_path / "mism"
    mism.mkdir()
    sp.run(["git", "init", "-q"], cwd=mism, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Impaktful-Platform/impaktful-registry.git"],
           cwd=mism, capture_output=True)
    r = run(mism, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert not (mism / "registry").exists()

    # --universe not matching the origin's owning org → refuse (the lint
    # exempts patterns by owner; a buro scope under impaktful-platform
    # would seed the wrong universe AND exempt the wrong org).
    r = run(mism, _bootstrap_env(tmp_path),
            slug="impaktful-platform/impaktful-registry")
    assert r.returncode == 2, r.stderr
    assert "requires the mirror to live under" in r.stderr
    assert not (mism / "registry").exists()

    # gh reports public → refuse.
    pub_bin = tmp_path / "pubbin"
    pub_bin.mkdir()
    gh = pub_bin / "gh"
    gh.write_text("#!/bin/sh\necho public\n")
    gh.chmod(0o755)
    r = run(root, dict(os.environ,
                       PATH=f"{pub_bin}:{os.environ['PATH']}"))
    assert r.returncode == 2, r.stderr
    assert not (root / "registry").exists()

    # A pushurl redirecting `git push` to a different repo than the
    # verified fetch url → refuse (seeded content would land elsewhere).
    pu = tmp_path / "pushurl"
    pu.mkdir()
    sp.run(["git", "init", "-q"], cwd=pu, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=pu, capture_output=True)
    sp.run(["git", "config", "remote.origin.pushurl",
            "https://github.com/Other-Org/public-repo.git"], cwd=pu,
           capture_output=True)
    r = run(pu, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert "public-repo" in r.stderr
    assert not (pu / "registry").exists()

    # A pushInsteadOf rewrite doing the same redirect → refuse.
    pi = tmp_path / "pushinsteadof"
    pi.mkdir()
    sp.run(["git", "init", "-q"], cwd=pi, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=pi, capture_output=True)
    sp.run(["git", "config", "url.https://gitlab.com/.pushInsteadOf",
            "https://github.com/"], cwd=pi, capture_output=True)
    r = run(pi, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert "gitlab.com" in r.stderr
    assert not (pi / "registry").exists()

    # insteadOf rewrites pushes too when no pushInsteadOf rule exists —
    # a github→gitlab insteadOf must refuse even though the raw origin
    # url is GitHub.
    io = tmp_path / "insteadof"
    io.mkdir()
    sp.run(["git", "init", "-q"], cwd=io, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=io, capture_output=True)
    sp.run(["git", "config", "url.https://gitlab.com/.insteadOf",
            "https://github.com/"], cwd=io, capture_output=True)
    r = run(io, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert "gitlab.com" in r.stderr
    assert not (io / "registry").exists()

    # insteadOf still applies to an EXPLICIT pushurl (only pushInsteadOf
    # is ignored for those) — pushurl pinned to the verified slug but an
    # insteadOf redirecting github.com elsewhere must refuse.
    pio = tmp_path / "pushurl-insteadof"
    pio.mkdir()
    sp.run(["git", "init", "-q"], cwd=pio, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=pio, capture_output=True)
    sp.run(["git", "config", "remote.origin.pushurl",
            "https://github.com/Buro-Built/buro-registry.git"], cwd=pio,
           capture_output=True)
    sp.run(["git", "config", "url.https://gitlab.com/.insteadOf",
            "https://github.com/"], cwd=pio, capture_output=True)
    r = run(pio, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert "gitlab.com" in r.stderr
    assert not (pio / "registry").exists()

    # remote.pushDefault pointing at a second remote whose slug differs —
    # a plain `git push` would select that remote over origin → refuse.
    pd = tmp_path / "pushdefault"
    pd.mkdir()
    sp.run(["git", "init", "-q"], cwd=pd, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=pd, capture_output=True)
    sp.run(["git", "remote", "add", "evil",
            "https://github.com/Other-Org/public-repo.git"],
           cwd=pd, capture_output=True)
    sp.run(["git", "config", "remote.pushDefault", "evil"], cwd=pd,
           capture_output=True)
    r = run(pd, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert "public-repo" in r.stderr
    assert not (pd / "registry").exists()

    # branch.<name>.pushRemote has the highest precedence — same
    # redirect via the current branch's pushRemote → refuse.
    pr = tmp_path / "pushremote"
    pr.mkdir()
    sp.run(["git", "init", "-q"], cwd=pr, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=pr, capture_output=True)
    sp.run(["git", "remote", "add", "evil",
            "https://github.com/Other-Org/public-repo.git"],
           cwd=pr, capture_output=True)
    cur = sp.run(["git", "symbolic-ref", "--short", "HEAD"], cwd=pr,
                 capture_output=True, text=True).stdout.strip()
    sp.run(["git", "config", f"branch.{cur}.pushRemote", "evil"], cwd=pr,
           capture_output=True)
    r = run(pr, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert "public-repo" in r.stderr
    assert not (pr / "registry").exists()

    # A push destination may be a literal URL, not a remote name —
    # a URL-valued remote.pushDefault pointing elsewhere → refuse.
    pv = tmp_path / "pushdefault-url"
    pv.mkdir()
    sp.run(["git", "init", "-q"], cwd=pv, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=pv, capture_output=True)
    sp.run(["git", "config", "remote.pushDefault",
            "https://github.com/Other-Org/public-repo.git"], cwd=pv,
           capture_output=True)
    r = run(pv, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert "public-repo" in r.stderr
    assert not (pv / "registry").exists()

    # When NO pushInsteadOf rule matches the URL destination, git falls
    # back to insteadOf rules — a URL pushDefault at the verified slug
    # with an unrelated pushInsteadOf rule AND a github→gitlab
    # insteadOf still redirects the push → refuse.
    pf = tmp_path / "pushdefault-url-fallback"
    pf.mkdir()
    sp.run(["git", "init", "-q"], cwd=pf, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=pf, capture_output=True)
    sp.run(["git", "config", "remote.pushDefault",
            "https://github.com/Buro-Built/buro-registry.git"], cwd=pf,
           capture_output=True)
    sp.run(["git", "config",
            "url.ssh://git@bitbucket.org/.pushInsteadOf",
            "ssh://git@bitbucket.org/"], cwd=pf, capture_output=True)
    sp.run(["git", "config", "url.https://gitlab.com/.insteadOf",
            "https://github.com/"], cwd=pf, capture_output=True)
    r = run(pf, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert "gitlab.com" in r.stderr
    assert not (pf / "registry").exists()

    # remote.<name>.vcs delegates pushes to a git-remote-<vcs> helper
    # that can forward the pack anywhere — the configured URL is not
    # evidence of the real destination → refuse.
    vc = tmp_path / "vcs-helper"
    vc.mkdir()
    sp.run(["git", "init", "-q"], cwd=vc, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=vc, capture_output=True)
    sp.run(["git", "config", "remote.origin.vcs",
            "helper--token=SUPERSECRET"], cwd=vc,
           capture_output=True)
    r = run(vc, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert "vcs" in r.stderr
    # The helper name itself may carry a credential — never echoed.
    assert "SUPERSECRET" not in r.stderr
    assert "helper--token" not in r.stderr
    assert not (vc / "registry").exists()

    # And a credential-bearing remote NAME is not echoed either.
    vn = tmp_path / "vcs-name"
    vn.mkdir()
    sp.run(["git", "init", "-q"], cwd=vn, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=vn, capture_output=True)
    sp.run(["git", "remote", "add", "SUPERSECRETTOKEN",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=vn, capture_output=True)
    sp.run(["git", "config", "remote.pushDefault", "SUPERSECRETTOKEN"],
           cwd=vn, capture_output=True)
    sp.run(["git", "config", "remote.SUPERSECRETTOKEN.vcs", "evil"],
           cwd=vn, capture_output=True)
    r = run(vn, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert "vcs" in r.stderr
    assert "SUPERSECRETTOKEN" not in r.stderr
    assert not (vn / "registry").exists()

    # GIT_EXEC_PATH swaps which git-remote-<scheme> helper the push
    # execs — a verified https URL is no evidence of the transport.
    gx = tmp_path / "execpath"
    gx.mkdir()
    sp.run(["git", "init", "-q"], cwd=gx, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=gx, capture_output=True)
    r = run(gx, dict(_bootstrap_env(tmp_path),
                     GIT_EXEC_PATH="/tmp/evil-exec"))
    assert r.returncode == 2, r.stderr
    assert "GIT_EXEC_PATH" in r.stderr
    assert not (gx / "registry").exists()

    # An ssh/scp URL parses to the verified slug, but core.sshCommand
    # replaces the transport entirely — the push can land anywhere →
    # refuse.
    sc = tmp_path / "sshcommand"
    sc.mkdir()
    sp.run(["git", "init", "-q"], cwd=sc, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "git@github.com:Buro-Built/buro-registry.git"],
           cwd=sc, capture_output=True)
    sp.run(["git", "config", "core.sshCommand", "evil-ssh"], cwd=sc,
           capture_output=True)
    r = run(sc, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert "sshCommand" in r.stderr
    assert not (sc / "registry").exists()

    # GIT_SSH_COMMAND is the environment-level equivalent of
    # core.sshCommand → refuse.
    se = tmp_path / "sshenv"
    se.mkdir()
    sp.run(["git", "init", "-q"], cwd=se, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "git@github.com:Buro-Built/buro-registry.git"],
           cwd=se, capture_output=True)
    env = _bootstrap_env(tmp_path)
    env["GIT_SSH_COMMAND"] = "evil-ssh"
    r = run(se, env)
    assert r.returncode == 2, r.stderr
    assert "GIT_SSH_COMMAND" in r.stderr
    assert not (se / "registry").exists()

    # A mixed-case scp host ('GitHub.com') still binds the verified slug
    # via the case-insensitive _slug_of — the ssh transport checks must
    # run on that same normalised view or GIT_SSH_COMMAND slips past.
    mc = tmp_path / "mixedcase-scp"
    mc.mkdir()
    sp.run(["git", "init", "-q"], cwd=mc, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=mc, capture_output=True)
    sp.run(["git", "config", "remote.origin.pushurl",
            "git@GitHub.com:Buro-Built/buro-registry.git"], cwd=mc,
           capture_output=True)
    r = run(mc, env)
    assert r.returncode == 2, r.stderr
    assert "GIT_SSH_COMMAND" in r.stderr
    assert not (mc / "registry").exists()

    # A rejected credential-bearing push URL must not print the
    # credential to stderr (it lands in transcripts/CI logs).
    cr = tmp_path / "creds"
    cr.mkdir()
    sp.run(["git", "init", "-q"], cwd=cr, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=cr, capture_output=True)
    sp.run(["git", "remote", "set-url", "--push", "origin",
            "https://x-access-token:SECRETTOKEN@github.com/"
            "Other-Org/public-repo.git"],
           cwd=cr, capture_output=True)
    r = run(cr, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert "SECRETTOKEN" not in r.stderr
    assert not (cr / "registry").exists()

    # Same for a credential-bearing literal URL in remote.pushDefault —
    # the configured destination itself must be redacted.
    cl = tmp_path / "credlit"
    cl.mkdir()
    sp.run(["git", "init", "-q"], cwd=cl, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=cl, capture_output=True)
    sp.run(["git", "config", "remote.pushDefault",
            "https://x-access-token:SECRETTOKEN@github.com/"
            "Other-Org/public-repo.git"],
           cwd=cl, capture_output=True)
    r = run(cl, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert "SECRETTOKEN" not in r.stderr
    assert not (cl / "registry").exists()

    # An 'ssh' resolved from PATH to a non-system location is a
    # wrapper — it would attest to its own '-G' output while forwarding
    # the push anywhere, so refuse without asking it.
    sh = tmp_path / "sshuntrusted"
    sh.mkdir()
    stub = tmp_path / "sshstub"
    stub.mkdir()
    s = stub / "ssh"
    s.write_text("#!/bin/sh\necho 'hostname github.com'\n")
    s.chmod(0o755)
    sp.run(["git", "init", "-q"], cwd=sh, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "git@github.com:Buro-Built/buro-registry.git"],
           cwd=sh, capture_output=True)
    env = _bootstrap_env(tmp_path)
    r = run(sh, dict(env, PATH=f"{stub}:{env['PATH']}"))
    assert r.returncode == 2, r.stderr
    assert "ssh transport cannot be verified" in r.stderr
    assert not (sh / "registry").exists()

    # git:// is plaintext transport (no encryption, no server
    # authentication) — refused outright regardless of gitProxy config.
    gp = tmp_path / "gitproto"
    gp.mkdir()
    sp.run(["git", "init", "-q"], cwd=gp, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "git://github.com/Buro-Built/buro-registry.git"],
           cwd=gp, capture_output=True)
    r = run(gp, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert "plaintext git" in r.stderr
    assert not (gp / "registry").exists()

    # An opaque helper destination can carry credentials in its
    # arguments — they must not reach stderr.
    ex = tmp_path / "extcreds"
    ex.mkdir()
    sp.run(["git", "init", "-q"], cwd=ex, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=ex, capture_output=True)
    sp.run(["git", "config", "remote.origin.pushurl",
            "ext::helper --token=SUPERSECRET %S"], cwd=ex,
           capture_output=True)
    r = run(ex, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert "SUPERSECRET" not in r.stderr
    assert not (ex / "registry").exists()

    # The opaque helper address can also carry a credential with no
    # space at all — the whole '<transport>::<address>' is withheld.
    e2 = tmp_path / "extcreds-nospace"
    e2.mkdir()
    sp.run(["git", "init", "-q"], cwd=e2, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=e2, capture_output=True)
    sp.run(["git", "config", "remote.origin.pushurl",
            "ext::helper--token=SUPERSECRET"], cwd=e2,
           capture_output=True)
    r = run(e2, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert "SUPERSECRET" not in r.stderr
    assert "helper" not in r.stderr
    assert not (e2 / "registry").exists()

    # An ordinary URL path can carry a credential too — the diagnostic
    # must withhold the path, keeping only scheme+host.
    cp = tmp_path / "credpath"
    cp.mkdir()
    sp.run(["git", "init", "-q"], cwd=cp, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=cp, capture_output=True)
    sp.run(["git", "config", "remote.origin.pushurl",
            "https://example.test/git/SECRETPATH/repo.git"], cwd=cp,
           capture_output=True)
    r = run(cp, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert "SECRETPATH" not in r.stderr
    assert not (cp / "registry").exists()

    # Userinfo may contain MORE than one '@' — git preserves the whole
    # value, so masking through only the first delimiter would leak the
    # remainder to stderr.
    mu = tmp_path / "multiat"
    mu.mkdir()
    sp.run(["git", "init", "-q"], cwd=mu, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=mu, capture_output=True)
    sp.run(["git", "config", "remote.origin.pushurl",
            "https://foo@SUPERSECRETTOKEN@github.com/Other/repo.git"],
           cwd=mu, capture_output=True)
    r = run(mu, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert "SUPERSECRETTOKEN" not in r.stderr
    assert not (mu / "registry").exists()

    # A local filesystem path is a valid git push destination — any of
    # its components may be sensitive, so none reach stderr.
    lp = tmp_path / "localpath"
    lp.mkdir()
    sp.run(["git", "init", "-q"], cwd=lp, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=lp, capture_output=True)
    sp.run(["git", "config", "remote.pushDefault",
            "/tmp/SECRETDIR/repo.git"], cwd=lp,
           capture_output=True)
    r = run(lp, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert "SECRETDIR" not in r.stderr
    assert "<opaque destination>" in r.stderr
    assert not (lp / "registry").exists()

    # 'file://' is the URL form of a local path — opaque too.
    lf = tmp_path / "localfile"
    lf.mkdir()
    sp.run(["git", "init", "-q"], cwd=lf, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=lf, capture_output=True)
    sp.run(["git", "config", "remote.pushDefault",
            "file:///tmp/SECRETFILE/repo.git"], cwd=lf,
           capture_output=True)
    r = run(lf, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert "SECRETFILE" not in r.stderr
    assert "file:***" in r.stderr
    assert not (lf / "registry").exists()

    # Even on github.com a path exceeding owner/repo(.git) may carry a
    # credential — it is withheld wholesale, not exempted by the host.
    gp = tmp_path / "ghpath"
    gp.mkdir()
    sp.run(["git", "init", "-q"], cwd=gp, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=gp, capture_output=True)
    sp.run(["git", "config", "remote.origin.pushurl",
            "https://github.com/Other-Org/public-repo/SECRETX"], cwd=gp,
           capture_output=True)
    r = run(gp, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert "SECRETX" not in r.stderr
    assert "github.com/***" in r.stderr
    assert not (gp / "registry").exists()

    # Plain-HTTP is refused outright — it is plaintext transport and
    # its effective proxy chain cannot be trusted for a private push.
    ht = tmp_path / "httppush"
    ht.mkdir()
    sp.run(["git", "init", "-q"], cwd=ht, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "http://github.com/Buro-Built/buro-registry.git"],
           cwd=ht, capture_output=True)
    r = run(ht, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert "plaintext http" in r.stderr
    assert not (ht / "registry").exists()

    # https with TLS verification disabled is MITM-able — refuse
    # (global http.sslVerify=false, a URL-scoped override, and the
    # GIT_SSL_NO_VERIFY environment variable all count).
    for i, cfg in enumerate((["http.sslVerify", "false"],
                             ["http.https://github.com/.sslVerify",
                              "false"],
                             # An explicitly-EMPTY value canonicalises
                             # to false — its blank --get-urlmatch
                             # record must not read as 'enabled'.
                             ["http.sslVerify", ""])):
        sv = tmp_path / f"ssloff{i}"
        sv.mkdir()
        sp.run(["git", "init", "-q"], cwd=sv, capture_output=True)
        sp.run(["git", "remote", "add", "origin",
                "https://github.com/Buro-Built/buro-registry.git"],
               cwd=sv, capture_output=True)
        sp.run(["git", "config", *cfg], cwd=sv, capture_output=True)
        r = run(sv, _bootstrap_env(tmp_path))
        assert r.returncode == 2, r.stderr
        assert "tls verification" in r.stderr
        assert not (sv / "registry").exists()

    sv = tmp_path / "sslenv"
    sv.mkdir()
    sp.run(["git", "init", "-q"], cwd=sv, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=sv, capture_output=True)
    # GIT_SSL_NO_VERIFY is defined by presence — '=0' still disables.
    r = run(sv, dict(_bootstrap_env(tmp_path), GIT_SSL_NO_VERIFY="0"))
    assert r.returncode == 2, r.stderr
    assert "tls verification" in r.stderr
    assert not (sv / "registry").exists()

    # A custom CA trust store keeps verification ON while trusting a
    # root the attacker may control — refuse env and config forms.
    ca = tmp_path / "sslcaenv"
    ca.mkdir()
    sp.run(["git", "init", "-q"], cwd=ca, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=ca, capture_output=True)
    r = run(ca, dict(_bootstrap_env(tmp_path),
                     GIT_SSL_CAINFO="/tmp/evil-ca.pem"))
    assert r.returncode == 2, r.stderr
    assert "custom CA" in r.stderr
    assert not (ca / "registry").exists()

    cb = tmp_path / "sslcacfg"
    cb.mkdir()
    sp.run(["git", "init", "-q"], cwd=cb, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=cb, capture_output=True)
    sp.run(["git", "config", "http.sslCAInfo", "/tmp/evil-ca.pem"],
           cwd=cb, capture_output=True)
    r = run(cb, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert "custom CA" in r.stderr
    assert not (cb / "registry").exists()

    # The same URL-scoped key repeated in a later scope: git resolves
    # equal-specificity entries by order — the later value wins.
    genv = _bootstrap_env(tmp_path)
    eq = tmp_path / "ssleq"
    eq.mkdir()
    sp.run(["git", "init", "-q"], cwd=eq, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=eq, capture_output=True)
    sp.run(["git", "config", "--global",
            "http.https://github.com/.sslVerify", "true"],
           cwd=eq, capture_output=True, env=genv)
    sp.run(["git", "config", "http.https://github.com/.sslVerify",
            "false"], cwd=eq, capture_output=True)
    r = run(eq, genv)
    assert r.returncode == 2, r.stderr
    assert "tls verification" in r.stderr
    assert not (eq / "registry").exists()

    # An insteadOf <base> containing spaces still applies — dropping
    # the rule via whitespace parsing would pass the pre-rewrite
    # github.com URL while git pushes to the rewritten helper.
    es = tmp_path / "extspace"
    es.mkdir()
    sp.run(["git", "init", "-q"], cwd=es, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=es, capture_output=True)
    sp.run(["git", "config", "remote.pushDefault",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=es, capture_output=True)
    sp.run(["git", "config",
            'url."ext::echo something ".insteadOf',
            "https://github.com/"], cwd=es, capture_output=True)
    r = run(es, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert not (es / "registry").exists()

    # scp-style 'user@host:path' userinfo is credential-bearing too —
    # it has no '://' for the userinfo rule to catch.
    cs = tmp_path / "credscp"
    cs.mkdir()
    sp.run(["git", "init", "-q"], cwd=cs, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=cs, capture_output=True)
    sp.run(["git", "config", "remote.origin.pushurl",
            "SECRETSCP@github.com:Other/public.git"], cwd=cs,
           capture_output=True)
    r = run(cs, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert "SECRETSCP" not in r.stderr
    assert not (cs / "registry").exists()

    # The auth-proxy exemption is for https proxies only — a non-https
    # prefix (e.g. an 'ext::… ' helper transport) must not exempt the
    # rewritten destination.
    ep = tmp_path / "extproxy"
    ep.mkdir()
    sp.run(["git", "init", "-q"], cwd=ep, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=ep, capture_output=True)
    sp.run(["git", "config",
            'url."ext::helper ".insteadOf', "https://github.com/"],
           cwd=ep, capture_output=True)
    r = run(ep, dict(_bootstrap_env(tmp_path),
                     MIRROR_GITHUB_PROXY_PREFIX="ext::helper "))
    assert r.returncode == 2, r.stderr
    assert not (ep / "registry").exists()

    # Credentials in the URL query or fragment must not reach stderr.
    cq = tmp_path / "credquery"
    cq.mkdir()
    sp.run(["git", "init", "-q"], cwd=cq, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=cq, capture_output=True)
    sp.run(["git", "config", "remote.origin.pushurl",
            "https://github.com/Other/public.git?access_token="
            "SECRETQUERY#frag=SECRETQUERY"], cwd=cq,
           capture_output=True)
    r = run(cq, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert "SECRETQUERY" not in r.stderr
    assert not (cq / "registry").exists()

    # A non-GitHub origin that parses to a valid-looking slug — gh would
    # verify an UNRELATED github.com repo of the same name → refuse.
    gl = tmp_path / "gitlab"
    gl.mkdir()
    sp.run(["git", "init", "-q"], cwd=gl, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://gitlab.com/Buro-Built/buro-registry.git"],
           cwd=gl, capture_output=True)
    r = run(gl, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert not (gl / "registry").exists()

    # gh stub OK but --root is not a git checkout → refused (no origin).
    plain = tmp_path / "plainroot"
    plain.mkdir()
    r = run(plain, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr

    # A credential.helper configured INSIDE the clone (local scope or
    # a file it includes) executes during the https push — the clone
    # is untrusted input; refuse it.
    ch = tmp_path / "credhelper"
    ch.mkdir()
    sp.run(["git", "init", "-q"], cwd=ch, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=ch, capture_output=True)
    sp.run(["git", "config", "credential.helper", "!leak"],
           cwd=ch, capture_output=True)
    r = run(ch, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert "credential.helper" in r.stderr
    assert not (ch / "registry").exists()

    # The same helper in the operator's GLOBAL config is the env trust
    # channel (like MIRROR_TRUST_DIRS), not untrusted input → allowed.
    chg = tmp_path / "credhelper-global"
    chg.mkdir()
    gcfg = tmp_path / "gitconfig.helper"
    gcfg.write_text("[credential]\n\thelper = !leak\n")
    sp.run(["git", "init", "-q"], cwd=chg, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=chg, capture_output=True)
    r = run(chg, dict(_bootstrap_env(tmp_path),
                      GIT_CONFIG_GLOBAL=str(gcfg)))
    assert r.returncode == 0, r.stderr

    # A LOCAL include.path pointing outside the checkout keeps scope
    # 'local' — the helper is still clone-derived untrusted input.
    inc = tmp_path / "incl"
    inc.mkdir()
    outside = tmp_path / "outside-helper.cfg"
    outside.write_text("[credential]\n\thelper = !leak\n")
    sp.run(["git", "init", "-q"], cwd=inc, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=inc, capture_output=True)
    sp.run(["git", "config", "include.path", str(outside)],
           cwd=inc, capture_output=True)
    r = run(inc, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert "credential.helper" in r.stderr
    assert not (inc / "registry").exists()

    # core.askPass answers the push's authentication prompt — a
    # clone-local value is the same untrusted-input class.
    ap = tmp_path / "askpass"
    ap.mkdir()
    sp.run(["git", "init", "-q"], cwd=ap, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=ap, capture_output=True)
    sp.run(["git", "config", "core.askPass", "/tmp/upload-creds"],
           cwd=ap, capture_output=True)
    r = run(ap, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert "askPass" in r.stderr
    assert not (ap / "registry").exists()

    # commit.gpgSign + a LOCAL gpg.program execs the program during
    # 'git commit' — clone-local signing config is untrusted input.
    sg = tmp_path / "signer"
    sg.mkdir()
    sp.run(["git", "init", "-q"], cwd=sg, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=sg, capture_output=True)
    sp.run(["git", "config", "commit.gpgsign", "true"],
           cwd=sg, capture_output=True)
    sp.run(["git", "config", "gpg.program", "/tmp/sign"],
           cwd=sg, capture_output=True)
    r = run(sg, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert "signing" in r.stderr
    assert not (sg / "registry").exists()

    # Attributes binding against a path the bootstrap CREATES
    # (README.md / schemas/**) must refuse even though the path does
    # not exist yet — the seeded file enters the filter during
    # 'git add'.
    fg = tmp_path / "filtgen"
    fg.mkdir()
    sp.run(["git", "init", "-q"], cwd=fg, capture_output=True)
    sp.run(["git", "remote", "add", "origin",
            "https://github.com/Buro-Built/buro-registry.git"],
           cwd=fg, capture_output=True)
    sp.run(["git", "config", "filter.leak.clean", "cat > /tmp/out2"],
           cwd=fg, capture_output=True)
    (fg / ".gitattributes").write_text(
        "README.md filter=leak\nschemas/** filter=leak\n")
    r = run(fg, _bootstrap_env(tmp_path))
    assert r.returncode == 2, r.stderr
    assert "filter" in r.stderr
    assert not (fg / "registry").exists()
