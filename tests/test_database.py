"""The database enforces the rules too, so a bug or a person with a SQL prompt cannot break them."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from rate_desk.db import SCHEMA_VERSION, Database
from rate_desk.domain import Channel
from tests.conftest import World


@pytest.fixture
def sql(world: World) -> Iterator[sqlite3.Connection]:
    """A plain SQL connection, going around the service entirely."""
    conn = Database(world.db_path).connect()
    yield conn
    conn.close()


def test_rates_cannot_be_edited_or_deleted(world: World, sql: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        sql.execute("UPDATE rates SET amount_pence = 9999")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        sql.execute("DELETE FROM rates")


def test_a_superseded_rate_cannot_be_revived_or_re_dated(
    world: World, sql: sqlite3.Connection
) -> None:
    world.service.approve(world.raise_ganger().id, by=world.priya, via=Channel.API)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        sql.execute("UPDATE rates SET superseded_at = NULL WHERE superseded_at IS NOT NULL")


def test_only_one_rate_can_be_in_force_per_project_and_trade(
    world: World, sql: sqlite3.Connection
) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        sql.execute(
            "INSERT INTO rates (project_code, trade_code, amount_pence, effective_from)"
            " VALUES ('viaduct', 'ganger', 9999, '2026-10-14T12:00:00.000000+00:00')"
        )


def test_one_request_can_create_only_one_rate(world: World, sql: sqlite3.Connection) -> None:
    request = world.raise_ganger()
    world.service.approve(request.id, by=world.priya, via=Channel.API)
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        sql.execute(
            "INSERT INTO rates"
            " (project_code, trade_code, amount_pence, effective_from, superseded_at,"
            "  source_request_id)"
            " VALUES ('viaduct', 'steel-fixer', 9999, '2026-10-14T12:00:00.000000+00:00', NULL, ?)",
            (request.id,),
        )


def test_a_decided_request_is_final(world: World, sql: sqlite3.Connection) -> None:
    request = world.raise_ganger()
    world.service.approve(request.id, by=world.priya, via=Channel.API)
    with pytest.raises(sqlite3.IntegrityError, match="cannot change"):
        sql.execute(
            "UPDATE rate_change_requests SET status = 'pending' WHERE id = ?", (request.id,)
        )
    with pytest.raises(sqlite3.IntegrityError, match="never deleted"):
        sql.execute("DELETE FROM rate_change_requests")


def test_a_request_cannot_be_edited_while_it_waits(world: World, sql: sqlite3.Connection) -> None:
    request = world.raise_ganger()
    with pytest.raises(sqlite3.IntegrityError, match="cannot be edited"):
        sql.execute(
            "UPDATE rate_change_requests SET requested_pence = 99999 WHERE id = ?", (request.id,)
        )


def test_the_database_refuses_self_approval(world: World, sql: sqlite3.Connection) -> None:
    request = world.raise_ganger(by=world.priya)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        sql.execute(
            "UPDATE rate_change_requests SET status = 'declined', decided_by = requested_by,"
            " decided_at = requested_at, decided_via = 'api', decline_reason = 'x' WHERE id = ?",
            (request.id,),
        )


def test_an_approved_request_must_say_what_it_created(
    world: World, sql: sqlite3.Connection
) -> None:
    request = world.raise_ganger()
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        sql.execute(
            "UPDATE rate_change_requests SET status = 'approved', decided_by = ?,"
            " decided_at = requested_at, decided_via = 'api' WHERE id = ?",
            (world.tom.id, request.id),
        )


def test_money_columns_hold_integers_only(world: World, sql: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="cannot store REAL"):
        sql.execute(
            "INSERT INTO rates (project_code, trade_code, amount_pence, effective_from)"
            " VALUES ('viaduct', 'steel-fixer', 2550.5, '2026-10-14T12:00:00.000000+00:00')"
        )


def test_events_are_append_only(world: World, sql: sqlite3.Connection) -> None:
    world.raise_ganger()
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        sql.execute("DELETE FROM events")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        sql.execute("UPDATE events SET type = 'request.approved'")


def test_migrating_twice_is_harmless(world: World) -> None:
    Database(world.db_path).migrate()
    assert world.service.get_request(world.raise_ganger().id)


def test_several_processes_starting_on_a_new_file_create_the_schema_once(
    tmp_path: Path,
) -> None:
    path = tmp_path / "new.db"
    with ThreadPoolExecutor(8) as pool:
        list(pool.map(lambda _: Database(path).migrate(), range(8)))
    conn = Database(path).connect()
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    finally:
        conn.close()
