"""``rate-desk``: run the server, run the MCP server over stdio, and set things up."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from types import FrameType

import uvicorn

from rate_desk.config import Settings
from rate_desk.db import Database
from rate_desk.errors import RateDeskError
from rate_desk.money import Money, MoneyError
from rate_desk.service import RateDesk
from rate_desk.stream import Broadcaster


def _service(db_path: Path) -> RateDesk:
    db = Database(db_path)
    db.migrate()
    return RateDesk(db)


def _serve(args: argparse.Namespace) -> None:
    from rate_desk.api import create_app

    settings = Settings.from_env()
    settings = Settings(
        db_path=args.db, public_url=settings.public_url, demo=args.demo or settings.demo
    )
    app = create_app(settings)
    config = uvicorn.Config(
        app,
        host=args.host,
        port=args.port,
        access_log=False,  # replaced by one that leaves tokens out of the logs
        # A backstop: MCP clients can hold streams open too. Dashboards are
        # ended straight away by Server.handle_exit below.
        timeout_graceful_shutdown=3,
        log_config=None,
    )
    Server(config, app.state.broadcaster).run()


class Server(uvicorn.Server):
    """uvicorn, plus: on shutdown, end the dashboards' streams first.

    uvicorn waits for open connections before it runs the app's shutdown, and a
    live stream never finishes by itself, so without this every restart would
    hang until the timeout and then log the streams as errors.
    """

    def __init__(self, config: uvicorn.Config, broadcaster: Broadcaster) -> None:
        super().__init__(config)
        self._broadcaster = broadcaster

    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        self._broadcaster.close()
        super().handle_exit(sig, frame)


def _mcp(args: argparse.Namespace) -> None:
    """MCP over stdio, for clients that launch the server themselves (Claude Desktop)."""
    from rate_desk.mcp_server import build_mcp_server

    token = os.environ.get("RATE_DESK_TOKEN")
    if not token:
        sys.exit("Set RATE_DESK_TOKEN to the token of the person this assistant works for.")
    service = _service(args.db)
    try:
        user = service.authenticate(token)
    except RateDeskError as exc:
        sys.exit(f"RATE_DESK_TOKEN was not accepted: {exc.message}")
    build_mcp_server(service, stdio_user=user).run("stdio")


def _seed_demo(args: argparse.Namespace) -> None:
    from rate_desk.seed import seed_demo

    service = _service(args.db)
    tokens = seed_demo(service, only_if_empty=True)
    if not tokens:
        sys.exit(f"{args.db} already has users; not seeding. Delete the file to start again.")
    print(f"Demo data loaded into {args.db}. Tokens (public, for demos only):")
    for name, token in tokens.items():
        print(f"  {name:<14} {token}")


def _add_user(args: argparse.Namespace) -> None:
    user, token = _service(args.db).add_user(
        args.name, can_raise=args.can_raise, can_approve=args.can_approve
    )
    print(f"Created user #{user.id} {user.name}. Their token, shown only now:\n{token}")


def _add_project(args: argparse.Namespace) -> None:
    _service(args.db).add_project(args.code, args.name)


def _add_trade(args: argparse.Namespace) -> None:
    _service(args.db).add_trade(args.code, args.name)


def _opening_rate(args: argparse.Namespace) -> None:
    try:
        amount = Money.parse(args.amount)
    except MoneyError as exc:
        sys.exit(str(exc))
    _service(args.db).load_opening_rate(args.project, args.trade, amount)


def main(argv: list[str] | None = None) -> None:
    # Logs go to stderr (basicConfig's default): in `rate-desk mcp`, stdout is the protocol.
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    parser = argparse.ArgumentParser(prog="rate-desk", description=__doc__)
    parser.add_argument(
        "--db",
        type=Path,
        default=Path(os.environ.get("RATE_DESK_DB", "rate-desk.db")),
        help="SQLite file (default: $RATE_DESK_DB or ./rate-desk.db)",
    )
    commands = parser.add_subparsers(required=True, metavar="command")

    serve = commands.add_parser("serve", help="REST API, wall dashboard and MCP over HTTP")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--demo", action="store_true", help="load demo data into an empty db")
    serve.set_defaults(run=_serve)

    commands.add_parser("mcp", help="MCP over stdio (needs RATE_DESK_TOKEN)").set_defaults(run=_mcp)
    commands.add_parser("seed-demo", help="load demo data into an empty db").set_defaults(
        run=_seed_demo
    )

    add_user = commands.add_parser("add-user", help="create a user and print their token")
    add_user.add_argument("name")
    add_user.add_argument("--can-raise", action="store_true")
    add_user.add_argument("--can-approve", action="store_true")
    add_user.set_defaults(run=_add_user)

    add_project = commands.add_parser("add-project")
    add_project.add_argument("code")
    add_project.add_argument("name")
    add_project.set_defaults(run=_add_project)

    add_trade = commands.add_parser("add-trade")
    add_trade.add_argument("code")
    add_trade.add_argument("name")
    add_trade.set_defaults(run=_add_trade)

    opening = commands.add_parser(
        "opening-rate", help="set a first rate where none has ever existed"
    )
    opening.add_argument("project")
    opening.add_argument("trade")
    opening.add_argument("amount", help='pounds, e.g. "18.50"')
    opening.set_defaults(run=_opening_rate)

    args = parser.parse_args(argv)
    try:
        args.run(args)
    except RateDeskError as exc:
        sys.exit(exc.message)


if __name__ == "__main__":
    main()
