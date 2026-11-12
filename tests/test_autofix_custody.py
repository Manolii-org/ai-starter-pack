"""Controller/API fixtures, not live agent or publication evidence."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess

import pytest
import yaml

ROOT=Path(__file__).resolve().parents[1]


def load_publisher():
    spec=importlib.util.spec_from_file_location('autofix_publisher',ROOT/'scripts/ci/autofix-publish.py')
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m


def native_pr(body='',head='a'*40):
    return dict(state='open',body=body,user={'id':17},labels=[],
        head={'sha':head,'ref':'fix/owned','repo':{'full_name':'example/repository'}},
        base={'ref':'main','repo':{'default_branch':'main'}})


@pytest.mark.parametrize('case,allowed',[
    ('unclaimed',True),('body',False),('sticky-author',False),('sticky-actions',False),
    ('forged-sticky',True),('fenced-example',True),('stale',False),('fork',False),
    ('unreadable',False),('incomplete',False),('human',False)])
def test_custody_uses_native_owner_and_fails_closed(tmp_path,case,allowed):
    pr=native_pr();comments=[]
    if case=='body':pr['body']='X-Follow-Through-Owner: opaque-retained-owner'
    if case=='fenced-example':pr['body']='```\nX-Follow-Through-Owner: example\n```'
    if 'sticky' in case:
        comments=[{'user':{'id':17 if case=='sticky-author' else 41898282 if case=='sticky-actions' else 99},
                   'body':'<!-- follow-through-owner:v1 -->\nretained'}]
    if case=='stale':pr['head']['sha']='b'*40
    if case=='fork':pr['head']['repo']['full_name']='outside/repository'
    if case=='human':pr['labels']=[{'name':'human-merge'}]
    if case=='incomplete':comments=[{'user':{'id':99},'body':'noise'}]*100
    fixtures=tmp_path/'data.json';fixtures.write_text(json.dumps({'pr':pr,'comments':comments}))
    bin_dir=tmp_path/'bin';bin_dir.mkdir()
    gh=bin_dir/'gh';gh.write_text('#!/usr/bin/python3\nimport json,os,sys\n'
        'd=json.load(open(os.environ["FIXTURE"]))\n'
        'if os.environ["CASE"]=="unreadable":sys.exit(1)\n'
        'print(json.dumps(d["comments"] if "/comments?" in sys.argv[-1] else d["pr"]))\n')
    gh.chmod(0o755);out=tmp_path/'output'
    r=subprocess.run(['bash',str(ROOT/'scripts/ci/autofix-custody.sh')],capture_output=True,text=True,
        env={**os.environ,'PATH':str(bin_dir)+':/usr/bin:/bin','FIXTURE':str(fixtures),'CASE':case,
             'PR_NUMBER':'7','EXPECTED_HEAD':'a'*40,'REPOSITORY':'example/repository','GITHUB_OUTPUT':str(out)},timeout=10)
    assert r.returncode==0,r.stderr
    assert out.read_text()==f'skip={str(not allowed).lower()}\n'


@pytest.fixture
def candidate(tmp_path,monkeypatch):
    work=tmp_path/'work';work.mkdir()
    def git(*args):return subprocess.check_output(['/usr/bin/git',*args],cwd=work).decode().strip()
    git('init','-q');git('config','user.email','fixture@example.invalid');git('config','user.name','fixture')
    (work/'file.txt').write_text('before');git('add','.');git('commit','-qm','base');base=git('rev-parse','HEAD')
    (work/'file.txt').write_text('after');git('commit','-qam','[autofix] fixture');head=git('rev-parse','HEAD')
    for k,v in dict(GITHUB_WORKSPACE=str(work),EXPECTED_HEAD=base,HEAD_REF='fix/owned',PR_NUMBER='7',
        GITHUB_REPOSITORY='example/repository',GITHUB_SERVER_URL='https://github.com',RUNNER_TEMP=str(tmp_path),
        GITHUB_API_URL='https://api.github.com',READ_TOKEN='fixture-read-only',PUBLICATION_ADMITTED='true').items():monkeypatch.setenv(k,v)
    monkeypatch.delenv('PUBLISH_TOKEN',raising=False)
    return work,base,head,git


def test_missing_publication_credential_preserves_local_implementation(candidate,monkeypatch):
    work,base,head,git=candidate;m=load_publisher()
    monkeypatch.setattr(m,'api',lambda path,**kw:native_pr(head=base) if '/pulls/' in path else pytest.fail('missing writer must not attempt publication'))
    result=m.publish()
    assert result['outcome']=='publication_credential_unavailable' and result['published_sha']==''
    assert git('rev-parse','HEAD')==head and (work/'file.txt').read_text()=='after'
    recovery=Path(result['recovery_path'])
    git('bundle','verify',str(recovery/'implementation.bundle'))
    assert json.loads((recovery/'receipt.json').read_text())['candidate_sha']==head


@pytest.mark.parametrize('hold',['custody','stale-head','registered-owner','unreadable'])
def test_late_publication_hold_retains_committed_recovery(candidate,monkeypatch,hold):
    work,base,head,git=candidate;m=load_publisher()
    monkeypatch.setenv('PUBLISH_TOKEN','fixture-write-only')
    if hold=='custody':monkeypatch.setenv('PUBLICATION_ADMITTED','false')
    def api(*a,**kw):
        if hold=='custody':pytest.fail('a denied guard must not trigger another native read')
        if hold=='unreadable':raise PermissionError('fixture denial')
        return native_pr(body='X-Follow-Through-Owner: retained' if hold=='registered-owner' else '',
                         head='b'*40 if hold=='stale-head' else base)
    monkeypatch.setattr(m,'api',api)
    result={'outcome':'unverified','published_sha':''}
    if hold=='unreadable':
        with pytest.raises(PermissionError):m.publish(result)
        assert result['outcome']=='native_state_unverified'
    else:
        m.publish(result)
        assert result['outcome']==('publication_admission_held' if hold=='custody' else 'native_state_held')
    git('bundle','verify',str(Path(result['recovery_path'])/'implementation.bundle'))
    assert result['published_sha']=='' and git('rev-parse','HEAD')==head


def test_guarded_publisher_uses_fresh_config_and_remote_readback(candidate,monkeypatch,tmp_path):
    work,base,head,git=candidate;m=load_publisher();calls=[];real_git=m.git
    git('config','url.https://outside.invalid/.insteadOf','https://github.com/')
    monitor=tmp_path/'untrusted-monitor'
    monitor.write_text('#!/bin/sh\ntouch '+str(tmp_path/'untrusted-monitor-ran')+'\n')
    monitor.chmod(0o755)
    git('config','core.fsmonitor',str(monitor))
    hooks=work/'.git/hooks';(hooks/'pre-push').write_text('#!/bin/sh\ntouch '+str(tmp_path/'untrusted-hook-ran')+'\n');(hooks/'pre-push').chmod(0o755)
    monkeypatch.setenv('PUBLISH_TOKEN','fixture-write-only')
    def run(args,cwd,**kw):
        if 'push' in args:
            calls.append(args)
            assert 'fixture-write-only' not in repr(args)
            assert 'READ_TOKEN' not in kw['env'] and 'PUBLISH_TOKEN' not in kw['env']
            config=subprocess.run(['/usr/bin/git',args[0],'config','--get-regexp','^url\\.'],cwd=cwd,capture_output=True,env=m.git_env())
            assert config.returncode==1
            return b''
        return real_git(args,cwd,**kw)
    monkeypatch.setattr(m,'git',run)
    def api(path,**kw):
        if '/pulls/' in path:return native_pr(head=base)
        assert '/git/ref/heads/fix%2Fowned' in path
        return {'object':{'sha':head}}
    monkeypatch.setattr(m,'api',api)
    result=m.publish()
    assert result['outcome']=='published' and result['published_sha']==head and len(calls)==1
    assert not (tmp_path/'untrusted-hook-ran').exists()
    assert not (tmp_path/'untrusted-monitor-ran').exists()


def test_uncertain_push_is_retained_and_never_retried(candidate,monkeypatch):
    work,base,head,git=candidate;m=load_publisher();real_git=m.git;pushes=[]
    monkeypatch.setenv('PUBLISH_TOKEN','fixture-write-only')
    def run(args,cwd,**kw):
        if 'push' in args:pushes.append(args);raise subprocess.CalledProcessError(1,args)
        return real_git(args,cwd,**kw)
    monkeypatch.setattr(m,'git',run)
    def api(path,**kw):
        if '/pulls/' in path:return native_pr(head=base)
        raise TimeoutError('fixture-only raw provider payload must not be reported')
    monkeypatch.setattr(m,'api',api)
    retained={'outcome':'unverified','published_sha':''}
    with pytest.raises(TimeoutError):m.publish(retained)
    assert retained['outcome']=='publication_unknown' and len(pushes)==1


def test_replace_refs_cannot_hide_instruction_changes(candidate,monkeypatch):
    work,base,safe,git=candidate;m=load_publisher()
    (work/'.claude').mkdir();(work/'.claude/unsafe.md').write_text('fixture instruction change')
    git('add','.');git('commit','-qm','[autofix] unsafe fixture');bad=git('rev-parse','HEAD')
    git('replace',bad,safe)
    monkeypatch.setenv('PUBLISH_TOKEN','fixture-write-only')
    monkeypatch.setattr(m,'api',lambda *a,**kw:pytest.fail('instruction change must hold before publication'))
    assert m.publish()['outcome']=='instruction_surface_held'


@pytest.mark.parametrize('file',['pr-autofix-loop.yml','pr-autofix-loop-reusable.yml'])
def test_actual_editor_cannot_publish_and_controller_is_custody_gated(file):
    workflow=yaml.safe_load((ROOT/'.github/workflows'/file).read_text());job=workflow['jobs']['autofix']
    editor=next(s for s in job['steps'] if s.get('id')=='claude')
    publisher=next(s for s in job['steps'] if s.get('id')=='publish')
    assert workflow['permissions']['contents']=='read'
    assert editor['with']['github_token']=='${{ github.token }}'
    assert 'steps.custody_dispatch.outputs.skip' in editor['if']
    assert publisher['if']=="steps.verify_exec.outcome == 'success'"
    assert publisher['env']['PUBLICATION_ADMITTED']=="${{ steps.custody_publish.outputs.skip == 'false' }}"
    if 'reusable' in file:
        activation=job['outputs']['activation']
        assert "steps.custody.outputs.skip == 'true'" in activation
        assert "steps.custody_dispatch.outputs.skip == 'true'" in activation
    assert 'do not push' in editor['with']['prompt']
    assert publisher['env']['PUBLISH_TOKEN'] in {'${{ secrets.GH_PAT }}','${{ secrets.MN_GH_AUTOMATION_TOKEN }}'}
    for other in job['steps']:
        if other is not publisher:
            assert not any('secrets.GH_PAT' in str(v) or 'secrets.MN_GH_AUTOMATION_TOKEN' in str(v)
                           for v in other.get('env',{}).values())
    assert 'fall back to GITHUB_TOKEN' not in (ROOT/'.github/workflows'/file).read_text()


def test_embedded_controller_source_is_current():
    r=subprocess.run([os.sys.executable,str(ROOT/'scripts/sync-autofix-control.py'),'--check'],capture_output=True,text=True)
    assert r.returncode==0,r.stdout+r.stderr
