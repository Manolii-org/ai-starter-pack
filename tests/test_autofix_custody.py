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


@pytest.mark.parametrize('outcome', ['publication_admission_held','native_state_held','instruction_surface_held',
    'publication_credential_unavailable','publication_unknown','native_state_unverified','commit_subject_held','candidate_ancestry_held','publication_packet_held'])
def test_held_receipts_suppress_duplicate_editors_for_the_same_head(tmp_path,outcome):
    # Run the actual Bash guard with a bounded fake native API, not a broker.
    original=native_pr
    comments=[{'user':{'id':41898282},'body':f'<!-- pack-autofix-publication:v1 head={"a"*40} outcome={outcome} -->'}]
    bin_dir=tmp_path/'bin';bin_dir.mkdir();output=tmp_path/'out'
    data=tmp_path/'facts';data.write_text(json.dumps({'pr':original(),'comments':comments}))
    gh=bin_dir/'gh';gh.write_text('#!/usr/bin/python3\nimport json,os,sys\nd=json.load(open(os.environ["FACTS"]))\nprint(json.dumps(d["comments"] if "/comments?" in sys.argv[-1] else d["pr"]))\n');gh.chmod(0o755)
    env={**os.environ,'PATH':str(bin_dir)+':/usr/bin:/bin','FACTS':str(data),
         'PR_NUMBER':'7','EXPECTED_HEAD':'a'*40,'REPOSITORY':'example/repository','GITHUB_OUTPUT':str(output)}
    for user,head,expected in [(41898282,'a'*40,'true'),(41898282,'b'*40,'false'),(99,'a'*40,'false')]:
        comments[0]['user']['id']=user;comments[0]['body']=f'<!-- pack-autofix-publication:v1 head={head} outcome={outcome} -->'
        data.write_text(json.dumps({'pr':original(),'comments':comments}));output.write_text('')
        subprocess.run(['bash',str(ROOT/'scripts/ci/autofix-custody.sh')],env=env,check=True,capture_output=True,timeout=10)
        assert output.read_text()==f'skip={expected}\n'


@pytest.fixture
def candidate(tmp_path,monkeypatch):
    work=tmp_path/'work';work.mkdir()
    def git(*args):return subprocess.check_output(['/usr/bin/git',*args],cwd=work).decode().strip()
    git('init','-q');git('config','user.email','fixture@example.invalid');git('config','user.name','fixture')
    (work/'file.txt').write_text('initial');git('add','.');git('commit','-qm','initial')
    (work/'file.txt').write_text('before');git('commit','-qam','base');base=git('rev-parse','HEAD')
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


def test_unmarked_commit_is_held_without_losing_implementation(candidate,monkeypatch):
    work,base,head,git=candidate;m=load_publisher()
    (work/'file.txt').write_text('unmarked');git('commit','-qam','unmarked local repair')
    monkeypatch.setenv('PUBLISH_TOKEN','fixture-write-only')
    monkeypatch.setattr(m,'api',lambda *a,**kw:pytest.fail('unmarked candidate must not reach publication'))
    result=m.publish()
    assert result['outcome']=='commit_subject_held' and result['published_sha']==''
    git('bundle','verify',str(Path(result['recovery_path'])/'implementation.bundle'))


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


def test_held_receipt_names_dependency_and_does_not_claim_upload(candidate,monkeypatch):
    m=load_publisher();sent=[]
    monkeypatch.setenv('GITHUB_RUN_ID','123')
    def api(path,**kw):
        if kw.get('method')=='POST':sent.append(kw['body']['body']);return {}
        return []
    monkeypatch.setattr(m,'api',api)
    m.record({'outcome':'publication_credential_unavailable','published_sha':'','recovery_path':'fixture-local-path'})
    assert len(sent)==1 and 'Restore its approved scoped route' in sent[0]
    assert 'not a durable upload receipt' in sent[0] and '/actions/runs/123' in sent[0]


@pytest.mark.parametrize('race',['reset','deleted'])
def test_publication_lease_refuses_a_ref_changed_after_native_read(candidate,monkeypatch,tmp_path,race):
    work,base,head,git=candidate;m=load_publisher();real_git=m.git;pushes=[]
    remote=tmp_path/'remote.git'
    subprocess.run(['/usr/bin/git','clone','--bare','--quiet',str(work),str(remote)],check=True,capture_output=True)
    def remote_git(*args):
        return subprocess.check_output(['/usr/bin/git','--git-dir='+str(remote),*args],env=m.git_env()).decode().strip()
    remote_git('update-ref','refs/heads/fix/owned',base)
    ancestor=git('rev-parse',base+'^')
    monkeypatch.setenv('PUBLISH_TOKEN','fixture-write-only')
    def run(args,cwd,**kw):
        if 'push' in args:
            pushes.append(args)
            if race=='reset':remote_git('update-ref','refs/heads/fix/owned',ancestor)
            else:remote_git('update-ref','-d','refs/heads/fix/owned')
            assert '--force-with-lease=refs/heads/fix/owned:'+base in args
            args=[str(remote) if a=='https://github.com/example/repository.git' else a for a in args]
        return real_git(args,cwd,**kw)
    monkeypatch.setattr(m,'git',run)
    def api(path,**kw):
        if '/pulls/' in path:return native_pr(head=base)
        r=subprocess.run(['/usr/bin/git','--git-dir='+str(remote),'rev-parse','--verify','refs/heads/fix/owned'],capture_output=True,text=True)
        return {'object':{'sha':r.stdout.strip() if r.returncode==0 else ''}}
    monkeypatch.setattr(m,'api',api)
    result=m.publish()
    assert result['outcome']=='publication_unknown' and result['published_sha']=='' and len(pushes)==1
    if race=='reset':assert remote_git('rev-parse','refs/heads/fix/owned')==ancestor
    else:
        assert subprocess.run(['/usr/bin/git','--git-dir='+str(remote),'show-ref','--verify','refs/heads/fix/owned'],capture_output=True).returncode!=0


def test_replace_refs_cannot_hide_instruction_changes(candidate,monkeypatch):
    work,base,safe,git=candidate;m=load_publisher()
    (work/'.claude').mkdir();(work/'.claude/unsafe.md').write_text('fixture instruction change')
    git('add','.');git('commit','-qm','[autofix] unsafe fixture');bad=git('rev-parse','HEAD')
    git('replace',bad,safe)
    monkeypatch.setenv('PUBLISH_TOKEN','fixture-write-only')
    monkeypatch.setattr(m,'api',lambda *a,**kw:pytest.fail('instruction change must hold before publication'))
    assert m.publish()['outcome']=='instruction_surface_held'


@pytest.mark.parametrize('name',['.claude','.husky','nested/.claude'])
def test_new_instruction_directory_symlink_cannot_bypass_object_guard(candidate,monkeypatch,name):
    work,base,head,git=candidate;m=load_publisher()
    path=work/name;path.parent.mkdir(parents=True,exist_ok=True)
    path.symlink_to('../untrusted-instructions',target_is_directory=True)
    git('add','--',name);git('commit','-qm','[autofix] symlink fixture')
    monkeypatch.setenv('PUBLISH_TOKEN','fixture-write-only')
    monkeypatch.setattr(m,'api',lambda *a,**kw:pytest.fail('instruction symlink must hold before native/publication calls'))
    assert m.publish()['outcome']=='instruction_surface_held'


def test_legacy_grafts_cannot_authorize_a_history_rewrite(candidate,monkeypatch):
    work,base,head,git=candidate;m=load_publisher()
    git('checkout','--orphan','unrelated')
    (work/'file.txt').write_text('unrelated history');git('add','.');git('commit','-qm','[autofix] unrelated fixture')
    bad=git('rev-parse','HEAD');(work/'.git/info/grafts').write_text(bad+' '+base+'\n')
    # Reproduce the old false ancestry even with replacement refs disabled.
    old_env=m.git_env();old_env.pop('GIT_GRAFT_FILE')
    assert subprocess.run(['/usr/bin/git','merge-base','--is-ancestor',base,bad],cwd=work,env=old_env,capture_output=True).returncode==0
    monkeypatch.setenv('PUBLISH_TOKEN','fixture-write-only')
    monkeypatch.setattr(m,'api',lambda *a,**kw:pytest.fail('forged ancestry must hold before publication'))
    assert m.publish()['outcome']=='candidate_ancestry_held'


@pytest.mark.parametrize('variant',['standalone','reusable'])
def test_actual_editor_cannot_publish_and_controller_is_custody_gated(variant):
    workflow=yaml.safe_load((ROOT/'.github/workflows/pr-autofix-execution-reusable.yml').read_text())
    job=workflow['jobs'][variant+'_editor']; publisher_job=workflow['jobs'][variant+'_publisher']
    editor=next(s for s in job['steps'] if s.get('id')=='claude')
    publisher=next(s for s in publisher_job['steps'] if s.get('id')=='publish')
    assert workflow['permissions']['contents']=='read'
    assert editor['with']['github_token']=='${{ github.token }}'
    assert "steps.custody_dispatch.outputs.skip == 'false'" in editor['if']
    ids=[s.get('id') for s in job['steps']]
    assert ids.index('custody_dispatch') < ids.index('claude')
    assert publisher_job['needs']==variant+'_editor'
    assert publisher_job['runs-on']=='ubuntu-latest' and 'env' not in publisher_job
    assert job['timeout-minutes']+publisher_job['timeout-minutes']==30
    assert "steps.custody_publish.outputs.skip == 'false'" in publisher['env']['PUBLICATION_ADMITTED']
    assert publisher['env']['AUTOFIX_PACKET_DIR']=='${{ runner.temp }}/autofix-candidate-data'
    assert not any('checkout@' in s.get('uses','') or 'claude-code-action@' in s.get('uses','') for s in publisher_job['steps'])
    if variant=='reusable':
        activation=job['outputs']['activation']
        assert "steps.custody.outputs.skip == 'true'" in activation
        assert "steps.custody_dispatch.outputs.skip == 'true'" in activation
    assert 'do not push' in editor['with']['prompt']
    assert publisher['env']['PUBLISH_TOKEN'] in {'${{ secrets.GH_PAT }}','${{ secrets.MN_GH_AUTOMATION_TOKEN }}'}
    for other in job['steps']:
        assert not any('secrets.GH_PAT' in str(v) or 'secrets.MN_GH_AUTOMATION_TOKEN' in str(v)
                       for v in other.get('env',{}).values())


def test_embedded_controller_source_is_current():
    r=subprocess.run([os.sys.executable,str(ROOT/'scripts/sync-autofix-control.py'),'--check'],capture_output=True,text=True)
    assert r.returncode==0,r.stdout+r.stderr


@pytest.mark.parametrize('variant',['standalone','reusable'])
@pytest.mark.parametrize('defect',['missing','duplicate','late','empty-payload','fail-open','or-bypass',
    'publisher-payload','publisher-admission','publisher-condition','shared-environment','wrong-artifact','editor-secret','failure-capture-gate','failed-editor-publication'])
def test_generator_refuses_partial_or_unsafe_control_wiring(variant,defect):
    spec=importlib.util.spec_from_file_location('autofix_sync',ROOT/'scripts/sync-autofix-control.py')
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
    workflow=yaml.safe_load((ROOT/'.github/workflows/pr-autofix-execution-reusable.yml').read_text())
    steps=workflow['jobs'][variant+'_editor']['steps']; pub_job=workflow['jobs'][variant+'_publisher']
    guard=next(s for s in steps if s.get('id')=='custody_dispatch')
    if defect=='missing':steps.remove(guard)
    if defect=='duplicate':steps.append(guard.copy())
    if defect=='late':steps.remove(guard);steps.append(guard)
    if defect=='empty-payload':guard['run']='echo unsafe'
    editor=next(s for s in steps if s.get('id')=='claude')
    if defect=='fail-open':editor['if']="steps.custody_dispatch.outputs.skip != 'true'"
    if defect=='or-bypass':editor['if']="true || steps.custody_dispatch.outputs.skip == 'false'"
    publisher=next(s for s in pub_job['steps'] if s.get('id')=='publish')
    if defect=='publisher-payload':publisher['run']='python3 untrusted-checkout.py'
    if defect=='publisher-admission':publisher['env']['PUBLICATION_ADMITTED']='true'
    if defect=='publisher-condition':publisher['if']='always()'
    if defect=='shared-environment':pub_job['env']={'HTTPS_PROXY':'${{ needs.'+variant+'_editor.outputs.proxy }}'}
    if defect=='wrong-artifact':next(s for s in pub_job['steps'] if 'download-artifact@' in s.get('uses',''))['with']['run-id']='other-run'
    if defect=='failure-capture-gate':next(s for s in steps if s.get('id')=='prepare')['if']="steps.verify_exec.outcome == 'success'"
    if defect=='failed-editor-publication':pub_job['if']='always()'
    if defect=='editor-secret':editor['env']['PUBLISH_TOKEN']='${{ secrets.GH_PAT }}'
    with pytest.raises(ValueError):m.validate(yaml.safe_dump(workflow))


@pytest.mark.parametrize('variant',['standalone','reusable'])
@pytest.mark.parametrize('defect',['partial-lock','missing-call','nested-lock'])
def test_generator_preserves_whole_execution_lock_contract(variant,defect):
    spec=importlib.util.spec_from_file_location('autofix_sync',ROOT/'scripts/sync-autofix-control.py')
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
    path=ROOT/'.github/workflows'/('pr-autofix-loop'+('-reusable' if variant=='reusable' else '')+'.yml')
    workflow=yaml.safe_load(path.read_text());job=workflow['jobs']['autofix']
    if defect=='partial-lock':job['steps']=[{'run':'untrusted'}]
    if defect=='missing-call':job['uses']='./wrong.yml'
    if defect=='nested-lock':
        if variant=='reusable':job['concurrency']={'group':'caller-lock'}
        else:workflow['concurrency']=job.pop('concurrency')
    with pytest.raises(ValueError):m.validate_wrapper(yaml.safe_dump(workflow),variant=='reusable')


def test_protected_change_then_restore_still_holds(candidate,monkeypatch):
    work,base,head,git=candidate;m=load_publisher()
    path=work/'AGENTS.md';path.write_text('temporary instruction mutation')
    git('add','.');git('commit','-qm','[autofix] temporary mutation')
    path.unlink();git('add','.');git('commit','-qm','[autofix] restore net tree')
    assert 'AGENTS.md' not in git('diff','--name-only',base,'HEAD')
    monkeypatch.setattr(m,'api',lambda *a,**kw:pytest.fail('historical protected change must hold'))
    assert m.publish()['outcome']=='instruction_surface_held'


@pytest.mark.parametrize('outcome',['candidate_ancestry_held','instruction_surface_held'])
def test_no_bundle_receipt_does_not_promise_retained_code(candidate,monkeypatch,outcome):
    m=load_publisher();sent=[]
    monkeypatch.setattr(m,'api',lambda path,**kw:sent.append(kw['body']['body']) if kw.get('method')=='POST' else [])
    m.record({'outcome':outcome,'published_sha':''})
    assert len(sent)==1 and 'retained no recovery bundle' in sent[0]
    assert 'must recover retained implementation' not in sent[0]


def test_separate_publisher_imports_only_bundle_data(candidate,monkeypatch,tmp_path):
    work,base,head,git=candidate;m=load_publisher();real_git=m.git
    remote=tmp_path/'native.git'
    subprocess.run(['/usr/bin/git','clone','--bare','--quiet',str(work),str(remote)],check=True,capture_output=True)
    monkeypatch.setenv('HTTPS_PROXY','https://editor-controlled.invalid')
    monkeypatch.setenv('GIT_SSL_CAINFO','/editor-controlled-ca')
    packet=m.prepare_packet();packet_dir=Path(packet['recovery_path'])
    assert {p.name for p in packet_dir.iterdir()}=={'receipt.json','implementation.bundle'}
    assert 'editor-controlled' not in (packet_dir/'receipt.json').read_text()
    # Model the fresh job environment. These are actual Git objects but local
    # transport fixtures, not authenticated GitHub publication evidence.
    monkeypatch.delenv('HTTPS_PROXY');monkeypatch.delenv('GIT_SSL_CAINFO')
    monkeypatch.setenv('AUTOFIX_PACKET_DIR',str(packet_dir))
    def native_fetch(args,cwd,**kw):
        if 'fetch' in args:
            assert kw['env'].get('HTTPS_PROXY') is None and kw['env'].get('GIT_SSL_CAINFO') is None
            args=[str(remote) if a=='https://github.com/example/repository.git' else a for a in args]
        return real_git(args,cwd,**kw)
    monkeypatch.setattr(m,'git',native_fetch)
    target=tmp_path/'publisher';target.mkdir()
    imported=m.import_packet(target)
    assert m.git(['rev-parse','HEAD'],imported).decode().strip()==head
    monkeypatch.setenv('GITHUB_WORKSPACE',str(imported))
    monkeypatch.setattr(m,'api',lambda *a,**kw:native_pr(head=base))
    result=m.publish()
    assert result['outcome']=='publication_credential_unavailable'
    assert Path(result['recovery_path'],'implementation.bundle').is_file()


@pytest.mark.parametrize('tamper',['wrong-repo','wrong-head','wrong-pr','wrong-ref','symlink','extra-file','oversized'])
def test_data_packet_cannot_grant_publication(candidate,monkeypatch,tmp_path,tamper):
    m=load_publisher();packet=Path(m.prepare_packet()['recovery_path']);receipt=packet/'receipt.json'
    meta=json.loads(receipt.read_text())
    field={'wrong-repo':'repository','wrong-head':'base_sha','wrong-pr':'pr_number','wrong-ref':'head_ref'}.get(tamper)
    if field:meta[field]='different';receipt.write_text(json.dumps(meta))
    if tamper=='extra-file':(packet/'GITHUB_ENV').write_text('HTTPS_PROXY=untrusted')
    if tamper=='symlink':
        saved=tmp_path/'untrusted';saved.write_text(receipt.read_text());receipt.unlink();receipt.symlink_to(saved)
    if tamper=='oversized':
        with (packet/'implementation.bundle').open('ab') as stream:stream.truncate(64*1024*1024+1)
    monkeypatch.setenv('AUTOFIX_PACKET_DIR',str(packet))
    monkeypatch.setattr(m,'git',lambda *a,**kw:pytest.fail('invalid packet must not reach Git/network'))
    with pytest.raises(ValueError):m.import_packet(tmp_path/'import')


@pytest.mark.parametrize('editor_failed',[False,True])
def test_embedded_capture_executes_without_publisher_or_api_credentials(candidate,monkeypatch,tmp_path,editor_failed):
    workflow=yaml.safe_load((ROOT/'.github/workflows/pr-autofix-execution-reusable.yml').read_text())
    step=next(s for s in workflow['jobs']['standalone_editor']['steps'] if s.get('id')=='prepare')
    assert 'PUBLISH_TOKEN' not in step['run'] and 'def api(' not in step['run']
    assert step['if']=="always() && steps.claude.outcome != 'skipped' && steps.claude.outcome != ''"
    if editor_failed:
        failure=subprocess.run(['bash','-c','exit 7'],cwd=candidate[0],capture_output=True)
        assert failure.returncode==7
    output=tmp_path/'capture-output'
    env={k:v for k,v in os.environ.items() if k not in {'PUBLISH_TOKEN','READ_TOKEN','GITHUB_API_URL'}}
    env['GITHUB_OUTPUT']=str(output)
    result=subprocess.run(['bash','-c',step['run']],env=env,text=True,capture_output=True,timeout=20)
    assert result.returncode==0,result.stderr
    packet=Path(output.read_text().strip().split('=',1)[1])
    assert (packet/'implementation.bundle').is_file()
    assert json.loads((packet/'receipt.json').read_text())['candidate_sha']==candidate[2]
