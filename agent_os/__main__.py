from __future__ import annotations

import argparse
import logging

import uvicorn

from agent_os.api import create_app
from agent_os.config import Settings


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m agent_os")
    parser.add_argument("command", choices=("serve",))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    # HTTPX's INFO request logs would expose Telegram tokens embedded in URLs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = Settings()
    if args.command == "serve":
        uvicorn.run(create_app(settings), host=settings.AOS_BIND, port=settings.AOS_PORT)


if __name__ == "__main__":
    main()
