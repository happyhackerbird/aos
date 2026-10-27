"""Render a v2 ``aos-policy.toml`` as an OpenShell sandbox policy.

Canonical copy of the renderer used by the OpenShell adapter (``agent_os.openshell_runner``)
and by the probe tooling under ``scripts/openshell_probe``. Every rule here was measured in
docs/probes/openshell-2026-10-11.md: ports are mandatory, wildcards need three labels, the
bus host differs per platform (the gateway alias on Linux; on colima-mac the IPv4 literal the
sandbox can reach, since the alias does not resolve inside a workload there, measured in
docs/probes/2026-10-14-agent-comms-p1b.md and -p1c.md), no rule may name the gateway port or
host alias, a host that a provider credential binds gets its rule from the provider, and harness
API hosts are an operator-level setting rather than part of the repository policy.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import re
import sys
import tomllib
from dataclasses import dataclass, field

LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")
CACHE_NAME = re.compile(r"[A-Za-z0-9_-]+")
GATEWAY_PORT = 17670
BUS_ALIAS = {"linux": "host.openshell.internal", "colima-mac": "host.docker.internal"}
HOST_ALIASES = frozenset(BUS_ALIAS.values())
# The host the sandbox reaches the bus by (operator decision 2026-10-14, option 1): on colima-mac the IPv4 literal
# the per-run database already uses, equal to openshell_services.DB_DEFAULTS["colima-mac"][1] (pinned by a test,
# not imported: this module is synced alone to probe hosts); on Linux the alias stays until measured.
BUS_HOST = {"linux": BUS_ALIAS["linux"], "colima-mac": "192.168.5.2"}
READ_ONLY_BASELINE = ("/bin", "/usr", "/lib", "/lib64", "/etc", "/proc", "/dev/urandom", "/var/log")
DEFAULT_EXTRA_RW = ("/tmp", "/dev/null", "/home/sandbox")
V1_FILESYSTEM_KEYS = {
    "deny_read": "filesystem.deny_read is v1; use filesystem.exclude (repo-relative paths omitted at provisioning)",
    "write": "filesystem.write is v1; the job directory is implied and caches use filesystem.caches",
}


class PolicyError(ValueError):
    """Every message starts with ``policy:`` so callers can refuse uniformly."""


def _error(what: str) -> PolicyError:
    return PolicyError("policy: " + what)


@dataclass(frozen=True)
class PolicyV2:
    exclude: tuple[str, ...] = ()
    caches: tuple[str, ...] = ()
    allow: tuple[tuple[str, int], ...] = field(default_factory=tuple)


def _table(data: dict, name: str, required: bool) -> dict:
    value = data.get(name)
    if value is None:
        if required:
            raise _error(f"missing [{name}]")
        return {}
    if type(value) is not dict:
        raise _error(f"{name} must be a table")
    return value


def _strings(value: object, name: str) -> list[str]:
    if type(value) is not list or any(type(item) is not str for item in value):
        raise _error(f"{name} must be a list of strings")
    return value


def _exclude_problem(entry: str) -> str | None:
    if not entry:
        return "is empty"
    if entry.startswith("/") or entry.startswith("~"):
        return "is absolute"
    if entry.endswith("/"):
        return "has a trailing slash"
    if any(part in ("", ".", "..") for part in entry.split("/")):
        return "has an empty, . or .. segment"
    return None


def parse_allow_entry(entry: str) -> tuple[str, int]:
    if "://" in entry:
        raise _error(f"network.allow entry {entry!r} has a scheme")
    if "/" in entry:
        raise _error(f"network.allow entry {entry!r} has a path")
    if "@" in entry:
        raise _error(f"network.allow entry {entry!r} has userinfo")
    host, separator, port = entry.rpartition(":")
    if not separator:
        raise _error(f"network.allow entry {entry!r} has no port; add :443")
    if not port.isdecimal() or not 1 <= int(port) <= 65535:
        raise _error(f"network.allow entry {entry!r} has an invalid port")
    if "**" in host:
        raise _error(f"network.allow entry {entry!r} uses **; only a leading *. wildcard is supported")
    labels = host.split(".")
    if labels[0] == "*":
        if len(labels) < 3:
            raise _error(f"network.allow entry {entry!r} wildcard needs at least three labels (*.example.com)")
        labels = labels[1:]
    if not host or any(not LABEL.fullmatch(label) for label in labels):
        raise _error(f"network.allow entry {entry!r} is not a hostname")
    return host.lower(), int(port)


def validate(data: object) -> PolicyV2:
    if type(data) is not dict:
        raise _error("top level must be a table")
    unknown = set(data) - {"execution", "filesystem", "network"}
    if unknown:
        raise _error(f"unknown section {sorted(unknown)[0]}")
    execution = _table(data, "execution", True)
    if set(execution) != {"fail_closed"} or execution.get("fail_closed") is not True:
        raise _error("execution.fail_closed must be true and its only key")
    filesystem = _table(data, "filesystem", False)
    for key, message in V1_FILESYSTEM_KEYS.items():
        if key in filesystem:
            raise _error(message)
    unknown = set(filesystem) - {"exclude", "caches"}
    if unknown:
        raise _error(f"unknown key filesystem.{sorted(unknown)[0]}")
    exclude = _strings(filesystem.get("exclude", []), "filesystem.exclude")
    for entry in exclude:
        problem = _exclude_problem(entry)
        if problem:
            raise _error(f"filesystem.exclude entry {entry!r} {problem}")
    caches = _strings(filesystem.get("caches", []), "filesystem.caches")
    for entry in caches:
        if not CACHE_NAME.fullmatch(entry):
            raise _error(f"filesystem.caches entry {entry!r} is not a short name")
    network = _table(data, "network", False)
    if "local_binding" in network:
        raise _error("network.local_binding is retired: loopback inside the sandbox is always local")
    unknown = set(network) - {"allow"}
    if unknown:
        raise _error(f"unknown key network.{sorted(unknown)[0]}")
    allow = tuple(parse_allow_entry(entry) for entry in _strings(network.get("allow", []), "network.allow"))
    return PolicyV2(tuple(dict.fromkeys(exclude)), tuple(dict.fromkeys(caches)), tuple(dict.fromkeys(allow)))


def load_policy_text(text: str) -> PolicyV2:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise _error(f"invalid TOML: {exc}") from None
    return validate(data)


def load_policy(path: str) -> PolicyV2:
    with open(path, "rb") as stream:
        return load_policy_text(stream.read().decode("utf-8"))


def render_exclusions(policy: PolicyV2) -> list[str]:
    return list(policy.exclude)


def _check_grants(policy: PolicyV2, bus_port: int, provider_hosts: set[str]) -> None:
    if bus_port == GATEWAY_PORT:
        raise _error(f"bus port {GATEWAY_PORT} is the gateway port")
    for host, port in policy.allow:
        entry = f"{host}:{port}"
        if host in provider_hosts:
            raise _error(f"network.allow entry {entry!r} names a provider-credentialed host; the provider rule supplies it")
        if host in HOST_ALIASES:
            raise _error(f"network.allow entry {entry!r} names the gateway host alias; only the bus rule may")
        if port == GATEWAY_PORT:
            raise _error(f"network.allow entry {entry!r} names the gateway port")


def _check_services(services: tuple[tuple[str, int], ...]) -> None:
    """The per-run database endpoints: an IPv4 literal (a name does not resolve inside the workload, F14/F16)
    and never the gateway port."""
    for host, port in services:
        try:
            ipaddress.IPv4Address(host)
        except ValueError:
            raise _error(f"services host {host!r} must be an IPv4 literal") from None
        if type(port) is not int or not 1 <= port <= 65535:
            raise _error(f"services entry {host}:{port} has an invalid port")
        if port == GATEWAY_PORT:
            raise _error(f"services entry {host}:{port} names the gateway port")


def bus_host(platform: str, override: str | None = None) -> str:
    """The host the aos-bus rule names and the run file's ``bus_url`` carries: the override when given (an IPv4
    literal, or one of the ``BUS_ALIAS`` values for a host whose alias is known to resolve), else the platform's
    measured default."""
    if platform not in BUS_ALIAS:
        raise _error(f"unknown platform {platform!r}; use one of {', '.join(sorted(BUS_ALIAS))}")
    if override is None:
        return BUS_HOST[platform]
    if override in HOST_ALIASES:
        return override
    try:
        return str(ipaddress.IPv4Address(override))
    except ValueError:
        raise _error(f"bus host {override!r} must be an IPv4 literal or one of {', '.join(sorted(HOST_ALIASES))}") from None


_resolve_bus_host = bus_host  # render's keyword argument of the same name shadows the function inside it


def parse_harness_hosts(entries: tuple[str, ...], provider_hosts: set[str]) -> list[tuple[str, int]]:
    """Operator-level harness API hosts (Codex: chatgpt.com, auth.openai.com, ab.chatgpt.com).

    They are not part of the repository policy; the operator configures them per harness.
    The same grammar and refusals as network.allow apply, and a provider-bound host is refused.
    """
    hosts: list[tuple[str, int]] = []
    for entry in entries:
        host, port = parse_allow_entry(entry)
        if host in provider_hosts:
            raise _error(f"harness host {entry!r} names a provider-credentialed host; the provider rule supplies it")
        if host in HOST_ALIASES or port == GATEWAY_PORT:
            raise _error(f"harness host {entry!r} names the gateway alias or port")
        if (host, port) not in hosts:
            hosts.append((host, port))
    return hosts


def render(policy: PolicyV2, *, platform: str, bus_port: int, provider_hosts: tuple[str, ...] = (),
           uid: int = 1000, extra_rw: tuple[str, ...] = DEFAULT_EXTRA_RW, harness_hosts: tuple[str, ...] = (),
           services: tuple[tuple[str, int], ...] = (), bus_host: str | None = None) -> str:
    host = _resolve_bus_host(platform, bus_host)  # refuses an unknown platform and a malformed override
    if not 1 <= bus_port <= 65535:
        raise _error("bus port must be between 1 and 65535")
    if uid < 1:
        raise _error("uid must be a non-root numeric identity")
    providers = {host.lower() for host in provider_hosts}
    _check_grants(policy, bus_port, providers)
    harness = parse_harness_hosts(harness_hosts, providers)
    _check_services(services)
    lines = ["version: 1", "filesystem_policy:", "  include_workdir: true", "  read_write:"]
    lines += [f"    - {json.dumps(path)}" for path in extra_rw]
    lines.append("  read_only:")
    lines += [f"    - {json.dumps(path)}" for path in READ_ONLY_BASELINE]
    lines += ["landlock:", "  compatibility: hard_requirement", "process:",
              f"  run_as_user: {json.dumps(str(uid))}", f"  run_as_group: {json.dumps(str(uid))}",
              "network_policies:", "  aos-bus:", "    binaries:", f"      - path: {json.dumps('/**')}",
              "    endpoints:", f"      - host: {host}", f"        port: {bus_port}"]
    if services:
        lines += ["  aos-services:", "    binaries:", f"      - path: {json.dumps('/**')}", "    endpoints:"]
        for host, port in services:
            lines += [f"      - host: {host}", f"        port: {port}"]
    if policy.allow:
        lines += ["  repo:", "    binaries:", f"      - path: {json.dumps('/**')}", "    endpoints:"]
        for host, port in policy.allow:
            # A leading * is a YAML alias token; quote wildcard hosts.
            spelled = json.dumps(host) if host.startswith("*") else host
            lines += [f"      - host: {spelled}", f"        port: {port}"]
    if harness:
        lines += ["  harness:", "    binaries:", f"      - path: {json.dumps('/**')}", "    endpoints:"]
        for host, port in harness:
            spelled = json.dumps(host) if host.startswith("*") else host
            lines += [f"      - host: {spelled}", f"        port: {port}"]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render a v2 aos-policy.toml as an OpenShell policy.")
    parser.add_argument("policy", help="path to aos-policy.toml (v2 shape)")
    parser.add_argument("--platform", choices=sorted(BUS_ALIAS), required=True)
    parser.add_argument("--bus-port", type=int, required=True)
    parser.add_argument("--provider-host", action="append", default=[], help="host a provider credential binds; repeatable")
    parser.add_argument("--harness-host", action="append", default=[],
                        help="operator-level harness API host:port (not part of the repo policy); repeatable")
    parser.add_argument("--workdir", default="/sandbox", help="recorded only; the image's WORKDIR is the sandbox workdir")
    parser.add_argument("--uid", type=int, default=1000)
    parser.add_argument("--extra-rw", action="append", default=None, help="writable path besides the workdir; repeatable")
    parser.add_argument("--out", help="write the YAML here instead of stdout")
    parser.add_argument("--print-exclusions", action="store_true", help="print the exclude globs, one per line, to stderr")
    args = parser.parse_args(argv)
    try:
        policy = load_policy(args.policy)
        text = render(policy, platform=args.platform, bus_port=args.bus_port, provider_hosts=tuple(args.provider_host),
                      uid=args.uid, extra_rw=tuple(args.extra_rw) if args.extra_rw else DEFAULT_EXTRA_RW,
                      harness_hosts=tuple(args.harness_host))
    except PolicyError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"policy: cannot read {args.policy}: {exc.strerror}", file=sys.stderr)
        return 2
    if args.out:
        with open(args.out, "w") as stream:
            stream.write(text)
    else:
        sys.stdout.write(text)
    if args.print_exclusions:
        for entry in render_exclusions(policy):
            print(entry, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
