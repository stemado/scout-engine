"""One-shot: copy scout-engine workflow rows from localhost to 192.168.150.52.

Ruling 2026-08-08: the shared server's scout_engine database is canonical.
This script makes that true without losing anything authored on localhost.

- Reads credentials from the repo's .mcp.json (remote) and scout-engine/.env
  (local); never takes credentials as arguments and never prints them.
- Copies workflow rows the remote is missing, PRESERVING ids so the tracked
  registry .claude/scout-engine-workflow-ids.json stays valid.
- Copies nothing else: executions/steps are run history, not configuration.
- Verifies the remote alembic version matches the local one before writing.

After it succeeds:
  1. Edit scout-engine/.env DATABASE_URL to point at 192.168.150.52 (URL-encode
     the password — it contains % and $).
  2. Restart the engine and confirm GET /api/workflows lists the full set.

Run:  python deploy/migrate-workflows-to-152.py          (dry run)
      python deploy/migrate-workflows-to-152.py --apply  (write)
"""

import asyncio
import json
import sys
from pathlib import Path
from urllib.parse import quote

import asyncpg

REPO = Path(__file__).resolve().parents[3]  # -> caladrius-onboarding
MCP = REPO / ".mcp.json"
ENV = Path(__file__).resolve().parents[1] / ".env"


def local_dsn() -> str:
    for line in ENV.read_text(encoding="utf-8").splitlines():
        if line.startswith("DATABASE_URL="):
            return line.split("=", 1)[1].strip().replace("+asyncpg", "")
    raise SystemExit("no DATABASE_URL in scout-engine/.env")


def remote_dsn() -> str:
    cfg = json.loads(MCP.read_text(encoding="utf-8"))
    env = cfg["mcpServers"]["postgres"]["env"]
    pw = quote(env["POSTGRES_PASSWORD"], safe="")
    return (
        f"postgresql://{env['POSTGRES_USER']}:{pw}@"
        f"{env['POSTGRES_HOST']}:{env['POSTGRES_PORT']}/scout_engine"
    )


async def main(apply: bool) -> int:
    src = await asyncpg.connect(local_dsn(), timeout=10)
    try:
        dst = await asyncpg.connect(remote_dsn(), timeout=15)
    except Exception as e:
        print(f"remote connect FAILED ({type(e).__name__}) — is 5432 open on .52?")
        await src.close()
        return 1

    try:
        sv = await src.fetchval("SELECT version_num FROM alembic_version")
        try:
            dv = await dst.fetchval("SELECT version_num FROM alembic_version")
        except Exception:
            dv = None
        print(f"alembic local={sv} remote={dv}")
        if dv != sv:
            print("remote is not at the same alembic head — run "
                  "'python -m alembic upgrade head' against it first (mind the "
                  "%%-escaping gotcha for the password in ConfigParser context).")
            return 1

        src_rows = await src.fetch(
            "SELECT id, name, description, schema_version, workflow_json, created_at, "
            "COALESCE(updated_at, created_at) AS updated_at "
            "FROM workflows"
        )
        have = {r["id"] for r in await dst.fetch("SELECT id FROM workflows")}
        have_names = {r["name"] for r in await dst.fetch("SELECT name FROM workflows")}

        missing = [r for r in src_rows if r["id"] not in have]
        for r in src_rows:
            state = "already on remote" if r["id"] in have else (
                "NAME EXISTS with different id — skipping, resolve by hand"
                if r["name"] in have_names else "copy"
            )
            print(f"  {r['id']} {r['name']}: {state}")

        to_copy = [r for r in missing if r["name"] not in have_names]
        if not apply:
            print(f"dry run: would copy {len(to_copy)} workflow(s). "
                  f"Re-run with --apply to write.")
            return 0

        for r in to_copy:
            await dst.execute(
                "INSERT INTO workflows (id, name, description, schema_version, "
                "workflow_json, created_at, updated_at) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7)",
                r["id"], r["name"], r["description"], r["schema_version"],
                r["workflow_json"], r["created_at"], r["updated_at"],
            )
        n = await dst.fetchval("SELECT count(*) FROM workflows")
        print(f"copied {len(to_copy)}; remote now has {n} workflows")
        return 0
    finally:
        await src.close()
        await dst.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main(apply="--apply" in sys.argv)))
