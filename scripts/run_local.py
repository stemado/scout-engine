"""Windows-friendly launcher for the scout-engine FastAPI service.

The relay's Python lane runs every app as `<.venv>\\Scripts\\python.exe scripts/run_local.py`
and reads HOST/PORT from the environment, so this file is the contract between the
deploy runner and the app. Mirrors DexchangeTriage's launcher.

asyncpg's async driver cannot run on Windows' ProactorEventLoop, and `uvicorn.run`
re-establishes the Proactor loop via `config.setup_event_loop()` even if the policy was
set beforehand. So we own the loop: set the SelectorEventLoop policy, then drive
`Server.serve()` inside our own `asyncio.run()` with `loop="none"` so uvicorn never
touches it.

    uv run python scripts/run_local.py        # env: DATABASE_URL, [HOST], [PORT]
"""
import asyncio
import os
import sys

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from uvicorn import Config, Server  # noqa: E402


async def _serve() -> None:
    config = Config(
        "app.main:app",
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8200")),
        loop="none",  # use the loop asyncio.run created under our Selector policy
    )
    await Server(config).serve()


if __name__ == "__main__":
    asyncio.run(_serve())
