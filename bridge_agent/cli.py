"""Bridge agent command line.

    python -m bridge_agent.cli keygen
    python -m bridge_agent.cli init --cloud-url ... --credential-id ... --public-key ...
    python -m bridge_agent.cli doctor
    python -m bridge_agent.cli run
    python -m bridge_agent.cli once
    python -m bridge_agent.cli status

The `doctor` and `status` commands are the local status screen in text form: they
read what is queued, whether Tally answers, and what the last error was, without
needing the cloud.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import pathlib
import sys

from bridge_agent import __version__
from bridge_agent.agent import BridgeAgent
from bridge_agent.config import DEFAULT_PATH, AgentConfig
from bridge_agent.queue import LocalQueue

log = logging.getLogger("bridge-agent")


def cmd_keygen(args: argparse.Namespace) -> int:
    """Generate the pair the *cloud* uses to sign commands.

    The private key stays with the engine; the agent is given only the public key,
    so it can verify a command but never mint one.
    """
    from engine.posting.commands import generate_keypair

    private, public = generate_keypair()
    print("cloud signing keypair (put the private key in the engine's .env):")
    print(f"AGENT_COMMAND_SIGNING_KEY={private}")
    print(f"AGENT_COMMAND_PUBLIC_KEY={public}")
    print()
    print(f"give this to the agent as --public-key: {public}")
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    config = AgentConfig(
        cloud_url=args.cloud_url,
        credential_id=args.credential_id,
        cloud_public_key=args.public_key or "",
        tally_url=args.tally_url,
        tally_company=args.company or "",
        app_version=__version__,
        state_dir=args.state_dir or str(DEFAULT_PATH.parent),
    )
    path = config.save(pathlib.Path(args.config) if args.config else None)
    print(f"configuration written to {path}")
    print("next: python -m bridge_agent.cli doctor")
    return 0


async def _doctor(config: AgentConfig) -> int:
    agent = BridgeAgent(config)
    state = await agent._tally_state()
    queue = agent.queue.counts()
    print(f"agent            {config.credential_id or '(not set)'}  v{config.app_version}")
    print(f"cloud            {config.cloud_url}")
    print(f"cloud public key {'set' if config.cloud_public_key else 'MISSING - commands will be refused'}")
    print(f"tally            {config.tally_url}  reachable={state['tally_running']}")
    print(f"company open     {state.get('company_open')}")
    print(f"company expected {config.tally_company}")
    print(f"period open      {state.get('period_open')}")
    print(f"queue            {json.dumps(queue)}")
    print(f"state dir        {config.state_dir}")
    print()
    for event in agent.queue.recent_events(10)[::-1]:
        print(f"  {event['level']:<5} {event['message']}")
    if not state["tally_running"]:
        print()
        print("TallyPrime is not answering. Open TallyPrime with the right company, and")
        print("enable its XML/HTTP interface (F1 > Settings > Connectivity).")
    return 0 if state["tally_running"] else 1


def cmd_doctor(args: argparse.Namespace) -> int:
    return asyncio.run(_doctor(_load(args)))


def cmd_status(args: argparse.Namespace) -> int:
    config = _load(args)
    if config.status_path.exists():
        print(config.status_path.read_text(encoding="utf-8"))
    else:
        print("no status yet; run `python -m bridge_agent.cli once` first")
    queue = LocalQueue(config.queue_path)
    print("recent events:")
    for event in queue.recent_events(10)[::-1]:
        print(f"  {event['level']:<5} {event['message']}  {event['detail'] or ''}")
    return 0


async def _run(config: AgentConfig, once: bool) -> int:
    agent = BridgeAgent(config)
    if once:
        summary = await agent.cycle()
        print(json.dumps(summary, indent=2))
        return 0
    print(f"bridge agent {__version__} running; polling every {config.poll_seconds}s; Ctrl+C to stop")
    await agent.run_forever()
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    return asyncio.run(_run(_load(args), once=False))


def cmd_once(args: argparse.Namespace) -> int:
    return asyncio.run(_run(_load(args), once=True))


def _load(args: argparse.Namespace) -> AgentConfig:
    path = pathlib.Path(args.config) if args.config else None
    return AgentConfig.load(path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bridge_agent", description="TallyPrime bridge agent")
    parser.add_argument("--config", default=None, help="path to agent.json")
    parser.add_argument("--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("keygen", help="generate the cloud's command signing keypair").set_defaults(func=cmd_keygen)

    init = sub.add_parser("init", help="write the agent configuration")
    init.add_argument("--cloud-url", required=True)
    init.add_argument("--credential-id", required=True)
    init.add_argument("--public-key", default=None, help="cloud command signing public key")
    init.add_argument("--tally-url", default="http://127.0.0.1:9000")
    init.add_argument("--company", default=None)
    init.add_argument("--state-dir", default=None)
    init.set_defaults(func=cmd_init)

    sub.add_parser("doctor", help="check Tally, the cloud key and the queue").set_defaults(func=cmd_doctor)
    sub.add_parser("status", help="show the last status and recent events").set_defaults(func=cmd_status)
    sub.add_parser("run", help="run the agent").set_defaults(func=cmd_run)
    sub.add_parser("once", help="run a single cycle").set_defaults(func=cmd_once)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
