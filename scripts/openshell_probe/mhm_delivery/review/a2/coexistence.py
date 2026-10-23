import contextlib
import io
import os
from pathlib import Path
import runpy
import sys
import tempfile
from unittest.mock import patch
scratch_parent = os.environ.get('MHM_SCRATCH') or None
if scratch_parent: Path(scratch_parent).mkdir(parents=True, exist_ok=True)
base = Path(tempfile.mkdtemp(prefix='mhm-coexistence-', dir=scratch_parent))
base.mkdir(exist_ok=True)
real_open=open
def scratch_open(path,*args,**kwargs):
 if str(path)=='/tmp/evil-hook-fired':path=base/'evil-hook-fired'
 return real_open(path,*args,**kwargs)
out=io.StringIO()
with patch.dict(os.environ,{'HOME':str(base)}),patch('builtins.open',scratch_open),patch.object(sys,'stdin',io.StringIO('{"hook_event_name":"PostToolUse"}')),patch.object(sys,'argv',['evil-hook.py','plugin-post']),contextlib.redirect_stdout(out):
 runpy.run_path(str(Path(__file__).resolve().parents[2]/'evil-hook.py'),run_name='__main__')
print(f'plugin_PostToolUse_stdout_bytes={len(out.getvalue().encode())}; plugin_context_present={bool(out.getvalue())}; hook_logged={(base/".local/share/codex-probe/fires.jsonl").exists()}')
