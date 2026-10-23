from pathlib import Path
import os
import tempfile
import subprocess

scratch_parent = os.environ.get('MHM_SCRATCH') or None
if scratch_parent: Path(scratch_parent).mkdir(parents=True, exist_ok=True)
base = Path(tempfile.mkdtemp(prefix='mhm-guards-', dir=scratch_parent))
def run(cmd,cwd):
    return subprocess.run(cmd,cwd=cwd,text=True,capture_output=True,check=True).stdout
for kind in ['ordinary','alias']:
    root=base/f'guard-{kind}'; root.mkdir(exist_ok=True)
    run(['git','init','-q'],root)
    target=root/('.codex' if kind=='ordinary' else 'settings')
    target.mkdir(exist_ok=True)
    (target/'hooks.json').write_text('{}\n')
    if kind=='alias' and not (root/'.codex').is_symlink(): (root/'.codex').symlink_to('settings')
    run(['git','add','.'],root)
    tree=run(['git','write-tree'],root).strip()
    listing=run(['git','ls-tree','--full-tree',tree,'--','.codex/config.toml','.codex/hooks.json'],root)
    print(f'D8 {kind}: guard_detected={bool(listing.strip())} readable_hook={(root/".codex/hooks.json").is_file()}')

root=base/'readiness'; root.mkdir(exist_ok=True)
binary=root/'aos-agent'; binary.write_text('#!/bin/sh\nexit 0\n'); binary.chmod(0o755)
extension=root/'aos_pi.ts'; extension.write_text('this is malformed TypeScript {\n')
result=subprocess.run(['sh','-c','test -x "$1" && test -f "$2"','sh',str(binary),str(extension)])
print(f'D10 malformed: readiness_exit={result.returncode}')

root=base/'empty-deliverable'
kit=root/'scripts/openshell_probe/mhm_delivery'; kit.mkdir(parents=True,exist_ok=True)
probe=kit/'probe.sh'; probe.write_text('#!/bin/sh\nexit 0\n'); probe.chmod(0o755)
for path in ['docs/probes/2026-10-17-multi-harness-delivery.md','docs/probes/2026-10-17-multi-harness-delivery/case-summaries.txt']:
    file=root/path; file.parent.mkdir(parents=True,exist_ok=True); file.write_text('')
(root/'docs/probes/README.md').write_text('2026-10-17-multi-harness-delivery\n')
design=root/'docs/design'; design.mkdir(parents=True,exist_ok=True)
(design/'2026-10-17-multi-harness-messaging-decomposition.md').write_text('## placeholder\n'*7)
(design/'README.md').write_text('2026-10-17-multi-harness-messaging-decomposition\n')
directives=root/'docs/directives'; directives.mkdir(parents=True,exist_ok=True)
for u in ['su1','su2','su3','su4']:
    (directives/f'2026-10-17-mhm-{u}.md').write_text('## Write boundaries\n')
(directives/'README.md').write_text('2026-10-17-mhm-su4\n')
pins={
 'U1':'test -x scripts/openshell_probe/mhm_delivery/probe.sh && test -f docs/probes/2026-10-17-multi-harness-delivery.md && test -f docs/probes/2026-10-17-multi-harness-delivery/case-summaries.txt && grep -q 2026-10-17-multi-harness-delivery docs/probes/README.md',
 'U2':"test -f docs/design/2026-10-17-multi-harness-messaging-decomposition.md && grep -c '^## ' docs/design/2026-10-17-multi-harness-messaging-decomposition.md | grep -qx 7 && grep -q 2026-10-17-multi-harness-messaging-decomposition docs/design/README.md",
 'U3':"for u in su1 su2 su3 su4; do test -f docs/directives/2026-10-17-mhm-$u.md && grep -q '^## Write boundaries' docs/directives/2026-10-17-mhm-$u.md || exit 1; done && grep -q 2026-10-17-mhm-su4 docs/directives/README.md",
}
for unit,cmd in pins.items():
    result=subprocess.run(['sh','-c',cmd],cwd=root)
    print(f'D16 {unit}: empty_deliverable_pin_exit={result.returncode}')
