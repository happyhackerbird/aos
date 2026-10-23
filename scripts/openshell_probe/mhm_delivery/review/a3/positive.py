"""Narrow positive checks; these do not certify unbuilt adapters."""
import json
import os
from pathlib import Path
import subprocess
import tempfile

root=Path(__file__).resolve().parents[5]
scratch_parent = os.environ.get('MHM_SCRATCH') or None
if scratch_parent: Path(scratch_parent).mkdir(parents=True, exist_ok=True)
base = Path(tempfile.mkdtemp(prefix='mhm-positive-', dir=scratch_parent))
for shape in ['hooks','config','directory_alias','leaf_alias','plain_codex','clean']:
    directory=base/shape;directory.mkdir()
    def git(*args):
        return subprocess.run(['git',*args],cwd=directory,text=True,capture_output=True,check=True).stdout.strip()
    git('init','-q')
    if shape=='directory_alias':
        (directory/'settings').mkdir();(directory/'settings/hooks.json').write_text('{}')
        (directory/'.codex').symlink_to('settings')
    elif shape=='plain_codex':(directory/'.codex').write_text('plain file')
    else:
        (directory/'.codex').mkdir()
        if shape=='hooks':(directory/'.codex/hooks.json').write_text('{}')
        elif shape=='config':(directory/'.codex/config.toml').write_text('x=1')
        elif shape=='leaf_alias':
            (directory/'external.json').write_text('{}')
            (directory/'.codex/hooks.json').symlink_to('../external.json')
        else:(directory/'.codex/ordinary').write_text('clean')
    git('add','.')
    tree=git('write-tree')
    listing=git('ls-tree','--full-tree',tree,'--','.codex','.codex/config.toml','.codex/hooks.json')
    parsed=[(line.split()[0],line.split('\t',1)[1]) for line in listing.splitlines()]
    refused=any(path in ('.codex/config.toml','.codex/hooks.json') or
                (path=='.codex' and mode!='040000') for mode,path in parsed)
    print(json.dumps({'D23_shape':shape,'listing':listing,'refused':refused}))
evidence=Path(os.environ.get('MHM_EVIDENCE', str(root/'docs/probes/2026-10-17-multi-harness-delivery')))
case=evidence/'smoke-codex'
if (case/'harness.out').is_file() and (case/'fake.log').is_file():
    stdout=(case/'harness.out').read_text();requests=(case/'fake.log').read_text()
    print(json.dumps({'D34_Codex_stdout_has_frame':'operator-message' in stdout,
     'stdout_has_word':'INJECTED-MID-RUN' in stdout,'provider_requests_have_word':'INJECTED-MID-RUN' in requests}))
else:
    print("D34: skipped (raw smoke-codex evidence absent; the fact is the probe record's F124)")
case=evidence/'a2-omp-instance'
if (case/'fires.jsonl').is_file():
    events=[json.loads(l) for l in (case/'fires.jsonl').read_text().splitlines()]
    sessions=[e for e in events if e.get('event')=='session_start']
    print(json.dumps({'D41_omp_sessions':len({e.get('session_id') for e in sessions}),
     'extension_instances':len({e.get('instance') for e in sessions})}))
else:
    print("D41: skipped (raw a2-omp-instance evidence absent; the fact is the probe record's F129)")
