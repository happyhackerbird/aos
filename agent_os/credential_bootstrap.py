"""Exec the runner with a clean initial environment and a private token pipe.

The token is consumed and the descriptor closed before any untrusted code runs.
It never appears in the replacement process's argv or initial environment.
"""
from __future__ import annotations

import os
import sys

import httpx
import psutil


def require_clean_initial_environment() -> None:
    """Library callers must also avoid an inspectable startup credential."""
    try:
        present = bool(psutil.Process().environ().get("AOS_TOKEN"))
    except (psutil.Error, OSError) as exc:
        raise ValueError("cannot verify the runner's initial credential environment") from exc
    if present:
        raise ValueError("runner startup environment contains AOS_TOKEN; use the aos CLI credential handoff")


def exec_clean(argv: list[str], token: str) -> None:
    read_fd, write_fd = os.pipe()
    try:
        raw = token.encode("utf-8")
        # A bounded, atomic write cannot block before exec starts the reader.
        if len(raw) > os.fpathconf(write_fd, "PC_PIPE_BUF"):
            raise ValueError("AOS_TOKEN exceeds the credential handoff limit")
        os.write(write_fd, raw)
        os.close(write_fd)
        write_fd = -1
        os.set_inheritable(read_fd, True)
        env = {key: value for key, value in os.environ.items() if key != "AOS_TOKEN"}
        os.execve(sys.executable, [sys.executable, "-P", "-m", __name__, str(read_fd), *argv], env)
    finally:
        os.close(read_fd)
        if write_fd >= 0:
            os.close(write_fd)


def main() -> int:
    from agent_os.cli import DEFAULT_URL, main as cli_main

    with os.fdopen(int(sys.argv[1]), "rb") as stream:
        token = stream.read().decode("utf-8")
    if not token or "AOS_TOKEN" in os.environ:
        raise ValueError("runner credential handoff requires a clean environment")
    with httpx.Client(base_url=os.environ.get("AOS_URL", DEFAULT_URL),
                      headers={"Authorization": f"Bearer {token}"}, timeout=30) as client:
        return cli_main(sys.argv[2:], client=client)


if __name__ == "__main__":
    sys.exit(main())
