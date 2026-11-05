"""scripts/herdr/aos-herdr-tab against stub herdr and aos executables."""
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "herdr" / "aos-herdr-tab"
CREATED = {"result": {"root_pane": {"pane_id": "w7:p3", "tab_id": "w7:t3"},
                      "tab": {"tab_id": "w7:t3", "label": "AO-31", "pane_ids": ["w7:p3"]},
                      "type": "tab_created"}}


def _stub(path: Path, body: str) -> None:
    path.write_text(f"#!{sys.executable}\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def stubs(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.jsonl"
    imports = "import json, os, sys\nfrom pathlib import Path\n"
    record = (imports + f"with Path({str(log)!r}).open('a') as stream:\n"
              "    stream.write(json.dumps([Path(sys.argv[0]).name, *sys.argv[1:]]) + '\\n')\n")
    _stub(bin_dir / "herdr", record +
          f"if sys.argv[3:5] == ['tab', 'create']:\n    print({json.dumps(CREATED)!r})\n")
    status_index = tmp_path / "status-index"
    _stub(bin_dir / "aos", record +
          f"index_file = Path({str(status_index)!r})\n"
          "if sys.argv[1] == 'list':\n"
          "    statuses = os.environ.get('AOS_TEST_STATUSES', 'review').split(',')\n"
          "    index = int(index_file.read_text()) if index_file.exists() else 0\n"
          "    index_file.write_text(str(index + 1))\n"
          "    current = statuses[min(index, len(statuses) - 1)]\n"
          "    if current == 'FAIL':\n"
          "        sys.exit(1)\n"
          "    if current == 'EMPTY':\n"
          "        sys.exit(0)\n"
          "    if current == 'PARTIAL_FAIL':\n"
          "        print('AO-31\\tdone\\tIncomplete response')\n"
          "        sys.exit(1)\n"
          "    print('AO-310\\tdone\\tAnother task')\n"
          "    if current != 'MISSING':\n"
          "        print('AO-31\\t' + current + '\\tHerdr tab')\n"
          "    print('AO-3\\tblocked\\tUnrelated task')\n"
          "    if os.environ.get('AOS_TEST_LONG_LIST'):\n"
          "        for index in range(10000):\n"
          "            print(f'AO-{index + 1000}\\tdone\\tAnother task')\n"
          "elif sys.argv[1] == 'show':\n"
          "    print(os.environ.get('AOS_TEST_BRIEF', 'Task brief'))\n"
          "    print('status: review')\n")
    # Advance a virtual clock instead of waiting 15 minutes in the boundary test.
    clock = tmp_path / "clock"
    clock.write_text("1000")
    _stub(bin_dir / "date", imports + f"print(Path({str(clock)!r}).read_text())\n")
    _stub(bin_dir / "sleep", record + f"clock = Path({str(clock)!r})\n"
          "clock.write_text(str(int(clock.read_text()) + int(sys.argv[1])))\n")

    def calls():
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    # The hook's environment as AO-30's runner builds it (stub bin first so plain herdr/aos resolve).
    env = {"PATH": f"{bin_dir}:{os.defpath}", "AOS_TASK_ID": "AO-31",
           "AOS_RUN_ID": "AO-31-r1", "AOS_SANDBOX_NAME": "a1b-ao-31-r1", "AOS_HARNESS": "claude",
           "AOS_ATTACH_COMMAND": "aos attach AO-31-r1"}
    return env, calls


def _run(env, *args, argv=None):
    # The runner shlex-splits the hook command and starts it without a shell.
    started = time.monotonic()
    proc = subprocess.run(argv or [str(SCRIPT), *args], env=env, capture_output=True, text=True, timeout=30)
    return proc, time.monotonic() - started


def _pane_command(calls):
    runs = [c for c in calls if c[:4] == ["herdr", "--session", "default", "pane"]]
    assert len(runs) == 1
    assert runs[0][4:6] == ["run", "w7:p3"]
    return runs[0][6]


def test_hook_opens_labelled_tab_attaches_and_returns_quickly(stubs):
    env, calls = stubs
    proc, elapsed = _run(env)
    assert proc.returncode == 0, proc.stderr
    assert elapsed < 5
    assert calls()[0] == ["herdr", "--session", "default", "tab", "create", "--label", "AO-31"]
    command = _pane_command(calls())
    assert command.startswith("aos attach AO-31-r1;")
    assert "sleep 60;" in command
    assert command.endswith("--session default tab close w7:t3")
    # Nothing ran the attach or closed the tab inside the hook itself.
    assert [c[0] for c in calls()] == ["herdr", "herdr"]


@pytest.mark.parametrize("shell", ["bash", "zsh", "dash"])
def test_pane_command_attaches_prints_status_and_closes_tab(stubs, shell):
    env, calls = stubs
    pane = _run_pane(env, calls, shell)
    assert pane.stdout == "status: review\n"
    assert calls()[2:] == [["aos", "attach", "AO-31-r1"], ["aos", "list"],
                           ["sleep", "60"],
                           ["herdr", "--session", "default", "tab", "close", "w7:t3"]]


def _run_pane(env, calls, shell="bash"):
    executable = shutil.which(shell)
    if executable is None:
        pytest.skip(f"{shell} is not installed")
    proc, elapsed = _run(env)
    assert proc.returncode == 0, proc.stderr
    assert elapsed < 5
    pane = subprocess.run([executable, "-c", _pane_command(calls())], env=env,
                          capture_output=True, text=True, timeout=180)
    assert pane.returncode == 0, pane.stderr
    assert pane.stderr == ""
    return pane


@pytest.mark.parametrize("shell", ["bash", "zsh", "dash"])
def test_pane_waits_for_acceptance_to_leave_running(stubs, shell):
    env, calls = stubs
    pane = _run_pane({**env, "AOS_TEST_STATUSES": "running,running,review"}, calls, shell)
    assert pane.stdout == "status: review\n"
    assert calls()[2:] == [
        ["aos", "attach", "AO-31-r1"],
        ["aos", "list"], ["sleep", "5"],
        ["aos", "list"], ["sleep", "5"],
        ["aos", "list"], ["sleep", "60"],
        ["herdr", "--session", "default", "tab", "close", "w7:t3"],
    ]


@pytest.mark.parametrize("shell", ["bash", "zsh", "dash"])
@pytest.mark.parametrize("unavailable", ["FAIL", "EMPTY", "MISSING", "PARTIAL_FAIL", ""])
def test_pane_keeps_polling_after_unavailable_status(stubs, shell, unavailable):
    env, calls = stubs
    pane = _run_pane({**env, "AOS_TEST_STATUSES": f"running,{unavailable},review"}, calls, shell)
    assert pane.stdout == "status: review\n"
    assert calls()[2:] == [
        ["aos", "attach", "AO-31-r1"],
        ["aos", "list"], ["sleep", "5"],
        ["aos", "list"], ["sleep", "5"],
        ["aos", "list"], ["sleep", "60"],
        ["herdr", "--session", "default", "tab", "close", "w7:t3"],
    ]


def test_pane_consumes_long_task_list_without_broken_pipe(stubs):
    env, calls = stubs
    pane = _run_pane({**env, "AOS_TEST_LONG_LIST": "1"}, calls)
    assert pane.stdout == "status: review\n"
    assert calls()[-1] == ["herdr", "--session", "default", "tab", "close", "w7:t3"]


def test_pane_ignores_status_line_in_task_brief(stubs):
    env, calls = stubs
    pane = _run_pane({**env, "AOS_TEST_BRIEF": "Example output:\nstatus: failed"}, calls)
    assert pane.stdout == "status: review\n"
    assert ["aos", "list"] in calls()
    assert not any(call[:2] == ["aos", "show"] for call in calls())


@pytest.mark.parametrize("statuses,expected", [
    ("running", "running"),
    ("running,MISSING", "running"),
    ("FAIL", "unknown"),
])
def test_pane_prints_last_status_after_fifteen_minutes(stubs, statuses, expected):
    env, calls = stubs
    pane = _run_pane({**env, "AOS_TEST_STATUSES": statuses}, calls)
    assert pane.stdout == f"status: {expected}\n"
    assert calls().count(["aos", "list"]) == 181
    assert [call for call in calls() if call[0] == "sleep"] == [["sleep", "5"]] * 180 + [["sleep", "60"]]
    assert calls()[-1] == ["herdr", "--session", "default", "tab", "close", "w7:t3"]


@pytest.mark.parametrize("delay,seconds", [("08", "8"), ("09", "9"), ("010", "10")])
def test_close_delay_is_decimal(stubs, delay, seconds):
    env, calls = stubs
    pane = _run_pane({**env, "AOS_HERDR_CLOSE_DELAY": delay}, calls)
    assert pane.stdout == "status: review\n"
    assert calls()[-2:] == [["sleep", seconds],
                           ["herdr", "--session", "default", "tab", "close", "w7:t3"]]


@pytest.mark.parametrize("task_id", ["", "AO-31 extra", "AO-31\n", "$(exit 99)"])
def test_invalid_hook_task_id_falls_back_to_run_task(stubs, task_id):
    env, calls = stubs
    pane = _run_pane({**env, "AOS_TASK_ID": task_id}, calls)
    assert calls()[0] == ["herdr", "--session", "default", "tab", "create", "--label", "AO-31"]
    assert pane.stdout == "status: review\n"


def test_manual_run_id_session_and_herdr_bin(stubs, tmp_path):
    env, calls = stubs
    custom = tmp_path / "bin" / "herdr"
    env = {k: v for k, v in env.items() if not k.startswith("AOS_")}
    env.update(PATH=os.defpath, HERDR_BIN=str(custom), AOS_HERDR_SESSION="ops")
    proc, _ = _run(env, "AO-28-r2")
    assert proc.returncode == 0, proc.stderr
    assert calls()[0] == ["herdr", "--session", "ops", "tab", "create", "--label", "AO-28"]
    command = calls()[1][6]
    assert calls()[1][:6] == ["herdr", "--session", "ops", "pane", "run", "w7:p3"]
    assert command.startswith("aos attach AO-28-r2;")
    assert command.endswith(f"{custom} --session ops tab close w7:t3")


def test_documented_hook_command_with_runner_environment(stubs, tmp_path):
    env, calls = stubs
    hook = (f"/usr/bin/env HERDR_BIN={tmp_path / 'bin' / 'herdr'} AOS_HERDR_SESSION=ops "
            f"{shlex.quote(str(SCRIPT))}")
    proc, elapsed = _run({**env, "PATH": os.defpath}, argv=shlex.split(hook))
    assert proc.returncode == 0, proc.stderr
    assert elapsed < 5
    assert calls()[0] == ["herdr", "--session", "ops", "tab", "create", "--label", "AO-31"]
    assert calls()[1][6].startswith("aos attach AO-31-r1;")


@pytest.mark.parametrize("run_id", ["", "AO-31", "rm -rf /"])
def test_rejects_missing_or_malformed_run_id(stubs, run_id):
    env, calls = stubs
    env = {k: v for k, v in env.items() if k != "AOS_RUN_ID"}
    proc, _ = _run(env, *([run_id] if run_id else []))
    assert proc.returncode == 2
    assert calls() == []


def test_missing_herdr_fails_without_hanging(stubs):
    env, _ = stubs
    proc, elapsed = _run({**env, "HERDR_BIN": "no-such-herdr"})
    assert proc.returncode == 1
    assert "set HERDR_BIN" in proc.stderr
    assert elapsed < 5


def test_script_is_executable():
    assert os.access(SCRIPT, os.X_OK)
