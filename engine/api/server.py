"""Run the engine API.

Use this rather than calling uvicorn directly. psycopg's async mode cannot run on
Windows' default ProactorEventLoop, and uvicorn re-applies the platform default
when it sets up its own loop, so this entry point builds a selector loop and drives
uvicorn's server on it.

    python -m engine.api.server --port 8088
"""

from __future__ import annotations

import argparse
import asyncio
import selectors
import sys


def build_loop() -> asyncio.AbstractEventLoop:
    if sys.platform == "win32":  # pragma: no cover - platform specific
        return asyncio.SelectorEventLoop(selectors.SelectSelector())
    return asyncio.new_event_loop()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the autonomous data entry engine")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8088)
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args()

    import uvicorn

    config = uvicorn.Config(
        "engine.api.app:app",
        host=args.host,
        port=args.port,
        loop="asyncio",
        log_level=args.log_level,
    )
    server = uvicorn.Server(config)

    loop = build_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(server.serve())
    except KeyboardInterrupt:  # pragma: no cover - interactive
        pass
    finally:
        loop.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
