"""`uv run serve` — run the AgentPhone webhook on this machine."""

from __future__ import annotations

import argparse
import os
import sys

import uvicorn

from model.provider import _load_dotenv
from phone.webhook import SECRET_ENV, WEBHOOK_PATH


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the AgentPhone webhook.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    _load_dotenv()
    if not os.environ.get(SECRET_ENV):
        print(f"warning: {SECRET_ENV} is not set, so every webhook will get a 401")
    print(f"webhook: http://{args.host}:{args.port}{WEBHOOK_PATH}")
    uvicorn.run("phone.webhook:create_served_app", factory=True, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
