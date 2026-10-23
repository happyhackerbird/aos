"""Tests for the OpenShell probe tooling: renderer refusals, YAML shape, provisioning, runner script, effective check."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent / "scripts" / "openshell_probe"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"openshell_probe_{name}", ROOT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


render_policy = _load("render_policy")
run_job = _load("run_job")

VALID = """[execution]
fail_closed = true

[filesystem]
exclude = [".env", "secrets/**"]
caches = ["uv", "npm"]

[network]
allow = ["pypi.org:443", "*.pythonhosted.org:443", "registry.npmjs.org:443"]
"""

EXPECTED_LINUX = """version: 1
filesystem_policy:
  include_workdir: true
  read_write:
    - "/tmp"
    - "/dev/null"
    - "/home/sandbox"
  read_only:
    - "/bin"
    - "/usr"
    - "/lib"
    - "/lib64"
    - "/etc"
    - "/proc"
    - "/dev/urandom"
    - "/var/log"
landlock:
  compatibility: hard_requirement
process:
  run_as_user: "1000"
  run_as_group: "1000"
network_policies:
  aos-bus:
    binaries:
      - path: "/**"
    endpoints:
      - host: host.openshell.internal
        port: 8010
  repo:
    binaries:
      - path: "/**"
    endpoints:
      - host: pypi.org
        port: 443
      - host: "*.pythonhosted.org"
        port: 443
      - host: registry.npmjs.org
        port: 443
"""

# Sample of `openshell policy get aos-a1 --full` as printed on 2026-10-10 (probe record, lane S3):
# header lines, a `---` separator, then the YAML with the provider rule appended.
EFFECTIVE_SAMPLE = """Version:      1
Hash:         a7029eb0485d72e539dad24e935fa6c1627cdc32fac88aaf298f9a6b2aa5d71f
Status:       Effective
Source:       sandbox
Config rev:   9013638984770165611
---
version: 1
filesystem_policy:
  include_workdir: true
  read_only:
    - /bin
    - /usr
  read_write:
    - /tmp
    - /dev/null
    - /home/sandbox
landlock:
  compatibility: hard_requirement
process:
  run_as_user: "1000"
  run_as_group: "1000"
network_policies:
  aos-bus:
    name: aos-bus
    endpoints:
      - host: host.openshell.internal
        port: 8010
    binaries:
      - path: /**
  repo:
    name: repo
    endpoints:
      - host: pypi.org
        port: 443
      - host: '*.pythonhosted.org'
        port: 443
      - host: registry.npmjs.org
        port: 443
    binaries:
      - path: /**
  _provider_aos_claude_oauth:
    name: _provider_aos_claude_oauth
    endpoints:
      - host: api.anthropic.com
        port: 443
        protocol: rest
        enforcement: enforce
        access: read-write
    binaries:
      - path: /usr/local/bin/node
      - path: /usr/bin/bash
"""


def write(tmp_path: Path, text: str) -> str:
    path = tmp_path / "aos-policy.toml"
    path.write_text(text)
    return str(path)


def test_valid_policy_renders_exact_yaml_linux(tmp_path):
    policy = render_policy.load_policy(write(tmp_path, VALID))
    text = render_policy.render(policy, platform="linux", bus_port=8010, provider_hosts=("api.anthropic.com",))
    assert text == EXPECTED_LINUX


def test_wildcard_hosts_are_quoted_for_yaml(tmp_path):
    policy = render_policy.load_policy(write(tmp_path, VALID))
    text = render_policy.render(policy, platform="linux", bus_port=8010, provider_hosts=("api.anthropic.com",))
    assert '      - host: "*.pythonhosted.org"\n' in text
    assert "host: *." not in text


def test_valid_policy_renders_colima_bus_host(tmp_path):
    # Endpoint lane E1: on colima-mac the bus rule names the IPv4 literal the sandbox can reach (P1b Q2, P1c), not
    # the alias, which does not resolve inside a workload there.
    policy = render_policy.load_policy(write(tmp_path, VALID))
    text = render_policy.render(policy, platform="colima-mac", bus_port=8020)
    assert "      - host: 192.168.5.2\n        port: 8020\n" in text
    assert "host.docker.internal" not in text and "host.openshell.internal" not in text


def test_bus_host_defaults_per_platform_and_pins_the_database_address():
    from agent_os import openshell_services

    assert render_policy.bus_host("colima-mac") == "192.168.5.2" == openshell_services.DB_DEFAULTS["colima-mac"][1]
    assert render_policy.bus_host("linux") == render_policy.BUS_ALIAS["linux"] == "host.openshell.internal"
    assert render_policy.bus_host("colima-mac", None) == "192.168.5.2"
    with pytest.raises(render_policy.PolicyError) as info:
        render_policy.bus_host("docker-desktop")
    assert str(info.value).startswith("policy: ") and "unknown platform" in str(info.value)


@pytest.mark.parametrize("override", ["10.0.0.9", "192.168.5.2", "host.docker.internal", "host.openshell.internal"])
def test_bus_host_override_is_an_ipv4_literal_or_a_bus_alias(override):
    assert render_policy.bus_host("colima-mac", override) == override
    assert render_policy.bus_host("linux", override) == override


@pytest.mark.parametrize("override", ["bus.example.com", "::1", "", "192.168.5.2:8012", "999.1.1.1", "*.example.com"])
def test_bus_host_refuses_other_overrides(override):
    with pytest.raises(render_policy.PolicyError) as info:
        render_policy.bus_host("colima-mac", override)
    assert str(info.value).startswith("policy: ") and "bus host" in str(info.value)


def test_render_bus_host_override_writes_the_bus_rule(tmp_path):
    policy = render_policy.load_policy(write(tmp_path, VALID))
    text = render_policy.render(policy, platform="colima-mac", bus_port=8020, bus_host="host.docker.internal")
    assert "  aos-bus:\n    binaries:\n      - path: \"/**\"\n    endpoints:\n      - host: host.docker.internal\n        port: 8020\n" in text
    assert "192.168.5.2" not in text
    text = render_policy.render(policy, platform="linux", bus_port=8010, bus_host="10.0.0.9")
    assert "      - host: 10.0.0.9\n        port: 8010\n" in text and "host.openshell.internal" not in text
    assert render_policy.render(policy, platform="linux", bus_port=8010, bus_host=None) == EXPECTED_LINUX
    with pytest.raises(render_policy.PolicyError) as info:
        render_policy.render(policy, platform="linux", bus_port=8010, bus_host="bus.example.com")
    assert "bus host" in str(info.value)


def test_render_exclusions(tmp_path):
    policy = render_policy.load_policy(write(tmp_path, VALID))
    assert render_policy.render_exclusions(policy) == [".env", "secrets/**"]
    assert policy.caches == ("uv", "npm")


def test_no_allow_omits_repo_rule(tmp_path):
    policy = render_policy.load_policy(write(tmp_path, "[execution]\nfail_closed = true\n"))
    text = render_policy.render(policy, platform="linux", bus_port=8010)
    assert "  repo:" not in text and "  aos-bus:" in text


@pytest.mark.parametrize("toml_text, phrase", [
    ("[execution]\nfail_closed = false\n", "fail_closed must be true"),
    ("[filesystem]\nexclude = []\n", "missing [execution]"),
    ("[execution]\nfail_closed = true\n[network]\nallow = [\"pypi.org\"]\n", "has no port; add :443"),
    ("[execution]\nfail_closed = true\n[network]\nallow = [\"*.org:443\"]\n", "at least three labels"),
    ("[execution]\nfail_closed = true\n[network]\nallow = [\"**.example.com:443\"]\n", "uses **"),
    ("[execution]\nfail_closed = true\n[network]\nallow = [\"https://pypi.org:443\"]\n", "has a scheme"),
    ("[execution]\nfail_closed = true\n[network]\nallow = [\"pypi.org:99999\"]\n", "invalid port"),
    ("[execution]\nfail_closed = true\n[network]\nlocal_binding = false\n", "network.local_binding is retired"),
    ("[execution]\nfail_closed = true\n[filesystem]\ndeny_read = [\".env\"]\n", "filesystem.deny_read is v1; use filesystem.exclude"),
    ("[execution]\nfail_closed = true\n[filesystem]\nwrite = [\"$WORKTREE\"]\n", "filesystem.write is v1"),
    ("[execution]\nfail_closed = true\n[filesystem]\nexclude = [\"/etc/x\"]\n", "is absolute"),
    ("[execution]\nfail_closed = true\n[filesystem]\nexclude = [\"a/../b\"]\n", "has an empty, . or .. segment"),
    ("[execution]\nfail_closed = true\n[filesystem]\nexclude = [\"secrets/\"]\n", "has a trailing slash"),
    ("[execution]\nfail_closed = true\n[tools]\nweb_fetch = false\n", "unknown section tools"),
    ("[execution]\nfail_closed = true\n[network]\nextra = 1\n", "unknown key network.extra"),
])
def test_refusals(tmp_path, toml_text, phrase):
    with pytest.raises(render_policy.PolicyError) as info:
        render_policy.load_policy(write(tmp_path, toml_text))
    assert str(info.value).startswith("policy: ")
    assert phrase in str(info.value)


@pytest.mark.parametrize("allow, kwargs, phrase", [
    ('["api.anthropic.com:443"]', {"provider_hosts": ("api.anthropic.com",)}, "provider-credentialed host"),
    ('["host.openshell.internal:8010"]', {}, "gateway host alias"),
    ('["host.docker.internal:8010"]', {}, "gateway host alias"),
    ('["example.com:17670"]', {}, "names the gateway port"),
])
def test_render_refusals(tmp_path, allow, kwargs, phrase):
    policy = render_policy.load_policy(write(tmp_path, f"[execution]\nfail_closed = true\n[network]\nallow = {allow}\n"))
    with pytest.raises(render_policy.PolicyError) as info:
        render_policy.render(policy, platform="linux", bus_port=8010, **kwargs)
    assert str(info.value).startswith("policy: ") and phrase in str(info.value)


def test_bus_port_may_not_be_the_gateway_port(tmp_path):
    policy = render_policy.load_policy(write(tmp_path, "[execution]\nfail_closed = true\n"))
    with pytest.raises(render_policy.PolicyError) as info:
        render_policy.render(policy, platform="linux", bus_port=17670)
    assert "gateway port" in str(info.value)


def test_cli_exit_codes(tmp_path, capsys):
    good = write(tmp_path, VALID)
    assert render_policy.main([good, "--platform", "linux", "--bus-port", "8010", "--provider-host", "api.anthropic.com"]) == 0
    assert capsys.readouterr().out == EXPECTED_LINUX
    bad = tmp_path / "bad.toml"
    bad.write_text('[execution]\nfail_closed = true\n[network]\nallow = ["pypi.org"]\n')
    assert render_policy.main([str(bad), "--platform", "linux", "--bus-port", "8010"]) == 2
    assert capsys.readouterr().err.startswith("policy: ")


def test_provision_excludes_paths_and_keeps_git(tmp_path):
    repo = tmp_path / "src"
    repo.mkdir()
    (repo / "app.py").write_text("print('hi')\n")
    (repo / "keep").mkdir()
    (repo / "keep" / "file.txt").write_text("x\n")
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base"], check=True)
    # Untracked local secrets never reach a clone; provision must still accept the entries.
    (repo / ".env").write_text("SECRET=1\n")
    (repo / "secrets").mkdir()
    (repo / "secrets" / "key").write_text("k\n")
    out = tmp_path / "job.tgz"
    summary = run_job.provision(str(repo), [".env", "secrets/**"], str(out))
    with tarfile.open(out) as tar:
        names = tar.getnames()
    assert ".env" not in names and not any(n.startswith("secrets") for n in names)
    assert "app.py" in names and "keep/file.txt" in names
    assert any(n == ".git" or n.startswith(".git/") for n in names)
    assert summary["excluded"] == []
    assert summary["files"] >= 2


def test_provision_refuses_exclude_matching_tracked_path(tmp_path):
    repo = tmp_path / "src"
    repo.mkdir()
    (repo / "app.py").write_text("print('hi')\n")
    (repo / ".env").write_text("SECRET=1\n")
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A", "-f"], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base"], check=True)
    with pytest.raises(SystemExit) as excinfo:
        run_job.provision(str(repo), [".env"], str(tmp_path / "job.tgz"))
    assert str(excinfo.value).startswith("policy: exclude '.env' matches tracked path .env")
    assert not (tmp_path / "job.tgz").exists()


def test_wait_for_file_accepts_directories(monkeypatch):
    seen = []
    def fake_exec(name, script, *, timeout=60, check=False):
        seen.append(script)
        return subprocess.CompletedProcess([], 0, "", "")
    monkeypatch.setattr(run_job, "sandbox_exec", fake_exec)
    run_job.wait_for_file("s", "/sandbox/incoming", 5)
    assert seen == ["test -e /sandbox/incoming"]


@pytest.mark.parametrize("harness, model, needle", [
    ("claude", None, 'claude -p "$(cat /sandbox/prompt.txt)" --output-format json --setting-sources "" --settings /sandbox/claude-settings.json </dev/null'),
    ("codex", None, 'CODEX_HOME=/sandbox/codex-home codex exec --json --skip-git-repo-check --sandbox danger-full-access -c approval_policy="never" "$(cat /sandbox/prompt.txt)" </dev/null'),
    ("pi", "anthropic/claude-haiku-4-5", 'pi -p "$(cat /sandbox/prompt.txt)" --mode json --model anthropic/claude-haiku-4-5 --no-session --no-extensions --no-skills </dev/null'),
])
def test_main_script_runs_harness_with_stdin_closed(harness, model, needle):
    script = run_job.build_main_script(harness, model)
    assert needle in script
    assert ">/sandbox/harness.out 2>/sandbox/harness.err" in script
    assert "echo $rc > /sandbox/harness.rc" in script
    # leftover daemons are swept before the completion marker appears
    assert script.index('pgrep -u "$(id -u)"') < script.index("echo $rc > /sandbox/harness.rc")
    assert "case \"$p\" in 1|$$|$PPID)" in script
    assert "while [ ! -f /sandbox/job.tgz ]" in script


def test_pi_requires_model():
    with pytest.raises(ValueError):
        run_job.build_main_script("pi", None)


def test_effective_matches_rendered_policy_with_provider_rule():
    assert run_job.compare_effective(EXPECTED_LINUX, EFFECTIVE_SAMPLE) == []


def test_effective_flags_missing_and_extra_rules():
    missing = EFFECTIVE_SAMPLE.replace("      - host: registry.npmjs.org\n        port: 443\n", "")
    problems = run_job.compare_effective(EXPECTED_LINUX, missing)
    assert any("rule repo endpoints differ" in p for p in problems)
    extra = EFFECTIVE_SAMPLE + "  global-extra:\n    endpoints:\n      - host: evil.example.com\n        port: 443\n"
    assert any("unexpected rule global-extra" in p for p in run_job.compare_effective(EXPECTED_LINUX, extra))
    weak = EFFECTIVE_SAMPLE.replace("compatibility: hard_requirement", "compatibility: best_effort")
    assert any("landlock" in p for p in run_job.compare_effective(EXPECTED_LINUX, weak))


def test_run_cli_logs_argv_without_env(monkeypatch, capsys):
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="NAME  CREATED  PHASE\n", stderr="")

    monkeypatch.setattr(run_job.subprocess, "run", fake_run)
    monkeypatch.setattr(run_job, "OPENSHELL", "openshell-fake")
    result = run_job.openshell("sandbox", "list")
    assert result.returncode == 0 and calls[0][:3] == ["openshell-fake", "sandbox", "list"]
    assert "+ openshell-fake sandbox list" in capsys.readouterr().err


def test_cancel_waits_until_unlisted(monkeypatch, capsys):
    listings = iter(["NAME  CREATED  PHASE\naos-x  t  Ready\n", "No sandboxes found.\n"])

    def fake_openshell(*args, **kw):
        if args[:2] == ("sandbox", "list"):
            return subprocess.CompletedProcess(args, 0, stdout=next(listings), stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(run_job, "openshell", fake_openshell)
    monkeypatch.setattr(run_job.time, "sleep", lambda s: None)
    ns = run_job.build_parser().parse_args(["cancel", "--name", "aos-x"])
    assert ns.func(ns) == 0
    assert json.loads(capsys.readouterr().out)["sandbox"] == "aos-x"


def test_render_harness_hosts_rule(tmp_path):
    policy = render_policy.load_policy(write(tmp_path, VALID))
    text = render_policy.render(policy, platform="linux", bus_port=8010, provider_hosts=("api.anthropic.com",),
                                harness_hosts=("chatgpt.com:443", "auth.openai.com:443", "ab.chatgpt.com:443"))
    assert "  harness:\n    binaries:\n      - path: \"/**\"\n    endpoints:\n      - host: chatgpt.com\n        port: 443\n" in text
    assert text.count("- host: auth.openai.com") == 1


@pytest.mark.parametrize("entry, phrase", [
    ("chatgpt.com", "has no port"),
    ("api.anthropic.com:443", "provider-credentialed host"),
    ("host.openshell.internal:8010", "gateway alias or port"),
    ("chatgpt.com:17670", "gateway alias or port"),
])
def test_render_refuses_bad_harness_hosts(tmp_path, entry, phrase):
    policy = render_policy.load_policy(write(tmp_path, VALID))
    with pytest.raises(render_policy.PolicyError) as excinfo:
        render_policy.render(policy, platform="linux", bus_port=8010, provider_hosts=("api.anthropic.com",),
                            harness_hosts=(entry,))
    assert str(excinfo.value).startswith("policy:") and phrase in str(excinfo.value)


def test_provision_ref_checks_out_the_requested_commit(tmp_path):
    repo = tmp_path / "src"
    repo.mkdir()
    (repo / "a.txt").write_text("one\n")
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(git + ["add", "-A"], check=True)
    subprocess.run(git + ["commit", "-qm", "base"], check=True)
    subprocess.run(git + ["checkout", "-qb", "fix"], check=True)
    (repo / "a.txt").write_text("two\n")
    subprocess.run(git + ["commit", "-qam", "fix"], check=True)
    subprocess.run(git + ["checkout", "-q", "-"], check=True)
    out = tmp_path / "job.tgz"
    run_job.provision(str(repo), [], str(out), ref="fix")
    with tarfile.open(out) as tar:
        content = tar.extractfile("a.txt").read()
    assert content == b"two\n"


def test_accept_script_runs_each_command_and_records_results():
    script = run_job.build_accept_script(["/tmp/venv/bin/python -m pytest -q tests", "bash verify.sh"])
    assert "while [ ! -f /sandbox/job.tgz ]" in script
    assert "( cd /sandbox/repo && /tmp/venv/bin/python -m pytest -q tests ) </dev/null >/sandbox/accept-0.out 2>&1; echo $? > /sandbox/accept-0.rc" in script
    assert "( cd /sandbox/repo && bash verify.sh ) </dev/null >/sandbox/accept-1.out 2>&1; echo $? > /sandbox/accept-1.rc" in script
    assert script.index("accept-1.rc") < script.index("touch /sandbox/accept.done")


# -- the per-run database grant (unit U2) --------------------------------------------------------

def test_services_group_renders_after_the_bus_and_before_the_repo_rule(tmp_path):
    policy = render_policy.load_policy(write(tmp_path, VALID))
    text = render_policy.render(policy, platform="colima-mac", bus_port=8020, services=(("192.168.5.2", 55432),))
    group = ('  aos-services:\n    binaries:\n      - path: "/**"\n    endpoints:\n'
             '      - host: 192.168.5.2\n        port: 55432\n')
    assert group in text
    assert text.index("  aos-bus:") < text.index("  aos-services:") < text.index("  repo:")
    assert render_policy.render(policy, platform="colima-mac", bus_port=8020) == \
        render_policy.render(policy, platform="colima-mac", bus_port=8020, services=())
    assert "aos-services" not in render_policy.render(policy, platform="colima-mac", bus_port=8020)


def test_services_group_without_repo_grants_and_with_two_endpoints(tmp_path):
    policy = render_policy.load_policy(write(tmp_path, "[execution]\nfail_closed = true\n"))
    text = render_policy.render(policy, platform="linux", bus_port=8010, services=(("10.0.0.9", 5432), ("10.0.0.9", 5433)))
    assert text.count("  aos-services:") == 1 and text.count("      - host: 10.0.0.9\n") == 2
    assert "        port: 5433\n" in text and "  repo:" not in text


@pytest.mark.parametrize("services, phrase", [
    ((("db.example.com", 5432),), "must be an IPv4 literal"),
    ((("host.docker.internal", 5432),), "must be an IPv4 literal"),
    ((("::1", 5432),), "must be an IPv4 literal"),
    ((("192.168.5.2", 17670),), "gateway port"),
    ((("192.168.5.2", 0),), "port"),
    ((("192.168.5.2", 70000),), "port"),
])
def test_services_group_refuses_bad_endpoints(tmp_path, services, phrase):
    policy = render_policy.load_policy(write(tmp_path, VALID))
    with pytest.raises(render_policy.PolicyError) as info:
        render_policy.render(policy, platform="colima-mac", bus_port=8020, services=services)
    assert str(info.value).startswith("policy: ") and phrase in str(info.value)


def test_a_repo_grant_naming_the_services_address_stays_possible(tmp_path):
    # D25 INVALIDATED dropped the repo-grant ban: owner-controlled default-branch policy carries it.
    policy = render_policy.load_policy(write(tmp_path, '[execution]\nfail_closed = true\n[network]\nallow = ["192.168.5.2:5432"]\n'))
    text = render_policy.render(policy, platform="colima-mac", bus_port=8020, services=(("192.168.5.2", 55432),))
    assert "  repo:" in text and "  aos-services:" in text
