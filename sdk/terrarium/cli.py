"""terra-cli — a thin terminal client for the Terrarium orchestrator.

Reads ``TERRA_URL`` (default http://127.0.0.1:8900) and ``TERRA_TOKEN`` from the
environment. Each command is a small wrapper over :class:`TerrariumClient`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any

from .client import TerrariumClient
from .errors import TerrariumError


def _client() -> TerrariumClient:
    return TerrariumClient(
        os.environ.get("TERRA_URL", "http://127.0.0.1:8900"),
        os.environ.get("TERRA_TOKEN") or None,
    )


def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=str))


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="terra-cli", description="Terrarium orchestrator client")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("health", help="orchestrator health + readiness")
    sub.add_parser("fleet", help="fleet summary (running/total/spend)")
    sub.add_parser("templates", help="list built-in agent templates")
    sub.add_parser("sessions", help="list sessions")

    ve = sub.add_parser("verify-egress", help="verify a session's tamper-evident egress audit chain")
    ve.add_argument("session_id")

    ag = sub.add_parser("agents", help="manage agents")
    agsub = ag.add_subparsers(dest="op", required=True)
    agsub.add_parser("list")
    agc = agsub.add_parser("create")
    agc.add_argument("name")
    agc.add_argument("--template")
    agc.add_argument("--model")
    agc.add_argument("--system-mode")
    agc.add_argument("--memory-scope")
    agd = agsub.add_parser("delete")
    agd.add_argument("id")
    agd.add_argument("--purge-memory", action="store_true")

    run = sub.add_parser("run", help="run a one-shot prompt and stream the reply")
    run.add_argument("prompt")
    run.add_argument("--agent")
    run.add_argument("--model")
    run.add_argument("--budget", type=float)
    run.add_argument("--keep", action="store_true", help="don't delete the session afterwards")

    sc = sub.add_parser("schedules", help="manage recurring agents")
    scsub = sc.add_subparsers(dest="op", required=True)
    scsub.add_parser("list")
    sca = scsub.add_parser("add")
    sca.add_argument("prompt")
    sca.add_argument("--agent", required=True)
    sca.add_argument("--cron", required=True, help="5-field cron, e.g. '0 7 * * *'")
    sca.add_argument("--name", default="schedule")
    sca.add_argument("--budget", type=float)
    scr = scsub.add_parser("run")
    scr.add_argument("id")
    scd = scsub.add_parser("rm")
    scd.add_argument("id")

    tk = sub.add_parser("tokens", help="manage scoped API tokens (admin)")
    tksub = tk.add_subparsers(dest="op", required=True)
    tksub.add_parser("list")
    tka = tksub.add_parser("add")
    tka.add_argument("name")
    tka.add_argument("--scope", action="append", default=[], help="repeatable: read|run|admin (default run)")
    tkr = tksub.add_parser("rm")
    tkr.add_argument("id")
    return p


async def _do_run(c: TerrariumClient, args: argparse.Namespace) -> None:
    kw: dict[str, Any] = {}
    if args.model:
        kw["model"] = args.model
    if args.budget:
        kw["max_budget_usd"] = args.budget
    sess = c.session(agent_id=args.agent, title="terra-cli run", **kw)
    await sess.connect()
    try:
        async for ev in sess.ask(args.prompt):
            t = ev["type"]
            if t == "assistant_text":
                sys.stdout.write(ev["text"])
                sys.stdout.flush()
            elif t == "tool_use":
                sys.stderr.write(f"\n[tool: {ev.get('name')}]\n")
            elif t == "result":
                sys.stderr.write(f"\n[done · ${ev.get('total_cost_usd')}]\n")
            elif t == "error":
                sys.stderr.write(f"\n[error: {ev.get('message')}]\n")
        sys.stdout.write("\n")
    finally:
        if not args.keep:
            await sess.close()


async def _amain(args: argparse.Namespace) -> int:
    c = _client()
    try:
        if args.cmd == "health":
            _print(await c.health())
        elif args.cmd == "fleet":
            _print(await c.fleet())
        elif args.cmd == "templates":
            _print(await c.templates())
        elif args.cmd == "sessions":
            _print(await c.sessions.list())
        elif args.cmd == "verify-egress":
            res = await c.sessions.verify_egress(args.session_id)
            _print(res)
            if not res.get("ok"):
                return 2  # chain broken/incomplete — distinct from a transport error (1)
        elif args.cmd == "agents":
            if args.op == "list":
                _print(await c.agents.list())
            elif args.op == "create":
                kw = {k: v for k, v in (("model", args.model), ("system_mode", args.system_mode)) if v}
                _print(await c.agents.create(args.name, template=args.template, memory_scope=args.memory_scope, **kw))
            elif args.op == "delete":
                _print(await c.agents.delete(args.id, purge_memory=args.purge_memory))
        elif args.cmd == "schedules":
            if args.op == "list":
                _print(await c.schedules.list())
            elif args.op == "add":
                _print(await c.schedules.create(name=args.name, agent_id=args.agent, prompt=args.prompt,
                                                cron=args.cron, max_budget_usd=args.budget))
            elif args.op == "run":
                _print(await c.schedules.run(args.id))
            elif args.op == "rm":
                await c.schedules.delete(args.id)
                print("deleted")
        elif args.cmd == "tokens":
            if args.op == "list":
                _print(await c.tokens.list())
            elif args.op == "add":
                _print(await c.tokens.create(args.name, args.scope or ["run"]))
            elif args.op == "rm":
                await c.tokens.delete(args.id)
                print("deleted")
        elif args.cmd == "run":
            await _do_run(c, args)
    except TerrariumError as exc:
        # The SDK raises typed TerrariumError (not raw httpx) for every API/transport
        # failure, so this is the ONE handler that keeps the CLI from dumping a
        # traceback on a 401/403/404/409/429/5xx or a lost connection.
        prefix = f"error {exc.status}: " if exc.status else "error: "
        sys.stderr.write(f"{prefix}{exc}\n")
        return 1
    finally:
        await c.aclose()
    return 0


def main(argv: list[str] | None = None) -> int:
    """Sync entry point — the SDK is async-only, so the CLI bridges via ``asyncio.run``."""
    args = _build_parser().parse_args(argv)
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    sys.exit(main())
