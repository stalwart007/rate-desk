-- Rate Desk schema, version 1.
--
-- The service layer enforces every rule. This file enforces them again, so
-- that a bug in the service, or someone with a SQL prompt, still cannot
-- break them. STRICT tables reject values of the wrong type outright.
--
-- Times are UTC ISO-8601 text with fixed-width microseconds
-- ("2026-10-14T09:12:03.123456+00:00"), so text order is time order.

CREATE TABLE projects (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL
) STRICT;

CREATE TABLE trades (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL
) STRICT;

CREATE TABLE users (
    id           INTEGER PRIMARY KEY,
    name         TEXT    NOT NULL,
    token_sha256 TEXT    NOT NULL UNIQUE,
    can_raise    INTEGER NOT NULL CHECK (can_raise IN (0, 1)),
    can_approve  INTEGER NOT NULL CHECK (can_approve IN (0, 1))
) STRICT;

-- Rate history. A rate is never edited: a change appends a new row and
-- stamps the old one with superseded_at. Amounts are integer pence (Rule 4).
CREATE TABLE rates (
    id                INTEGER PRIMARY KEY,
    project_code      TEXT    NOT NULL REFERENCES projects (code),
    trade_code        TEXT    NOT NULL REFERENCES trades (code),
    amount_pence      INTEGER NOT NULL CHECK (amount_pence > 0),
    effective_from    TEXT    NOT NULL,
    superseded_at     TEXT    CHECK (superseded_at IS NULL OR superseded_at >= effective_from),
    -- Rule 3, physically: one request can produce at most one rate.
    source_request_id INTEGER UNIQUE REFERENCES rate_change_requests (id)
) STRICT;

-- At most one rate in force per project and trade.
CREATE UNIQUE INDEX rates_one_in_force
    ON rates (project_code, trade_code) WHERE superseded_at IS NULL;

CREATE INDEX rates_history ON rates (project_code, trade_code, effective_from);

CREATE TABLE rate_change_requests (
    id                INTEGER PRIMARY KEY,
    project_code      TEXT    NOT NULL REFERENCES projects (code),
    trade_code        TEXT    NOT NULL REFERENCES trades (code),
    current_pence     INTEGER CHECK (current_pence IS NULL OR current_pence > 0),
    requested_pence   INTEGER NOT NULL CHECK (requested_pence > 0),
    reason            TEXT    NOT NULL CHECK (length(trim(reason)) > 0),
    requested_by      INTEGER NOT NULL REFERENCES users (id),
    requested_at      TEXT    NOT NULL,
    status            TEXT    NOT NULL DEFAULT 'pending'
                              CHECK (status IN ('pending', 'approved', 'declined')),
    decided_by        INTEGER REFERENCES users (id),
    decided_at        TEXT,
    decided_via       TEXT    CHECK (decided_via IN ('api', 'mcp')),
    decline_reason    TEXT,
    -- Rule 2: what an approval did. A rate can be replaced only once, and
    -- created only once, hence UNIQUE on both.
    previous_rate_id  INTEGER UNIQUE REFERENCES rates (id),
    resulting_rate_id INTEGER UNIQUE REFERENCES rates (id),

    CHECK (current_pence IS NULL OR current_pence <> requested_pence),
    -- Four eyes: whoever decides is not whoever asked.
    CHECK (decided_by IS NULL OR decided_by <> requested_by),
    -- Each status carries exactly the fields that belong to it.
    CHECK (
        (status = 'pending'
            AND decided_by IS NULL AND decided_at IS NULL AND decided_via IS NULL
            AND decline_reason IS NULL
            AND previous_rate_id IS NULL AND resulting_rate_id IS NULL)
     OR (status = 'approved'
            AND decided_by IS NOT NULL AND decided_at IS NOT NULL AND decided_via IS NOT NULL
            AND decline_reason IS NULL
            AND resulting_rate_id IS NOT NULL
            AND ((current_pence IS NULL) = (previous_rate_id IS NULL)))
     OR (status = 'declined'
            AND decided_by IS NOT NULL AND decided_at IS NOT NULL AND decided_via IS NOT NULL
            AND decline_reason IS NOT NULL AND length(trim(decline_reason)) > 0
            AND previous_rate_id IS NULL AND resulting_rate_id IS NULL)
    )
) STRICT;

CREATE INDEX requests_by_status ON rate_change_requests (status, id);
CREATE INDEX requests_by_requester ON rate_change_requests (requested_by, id);

-- Everything the wall dashboard needs to know, in commit order. Written in
-- the same transaction as the change it describes (a transactional outbox),
-- so an event exists if and only if its change committed. AUTOINCREMENT
-- means an id is never reused, even after the newest row is gone.
CREATE TABLE events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    type        TEXT    NOT NULL
                        CHECK (type IN ('request.raised', 'request.approved', 'request.declined',
                                        'request.updated')),
    request_id  INTEGER NOT NULL REFERENCES rate_change_requests (id),
    occurred_at TEXT    NOT NULL,
    payload     TEXT    NOT NULL CHECK (json_valid(payload))
) STRICT;

-- Append-only, enforced by the database itself.

CREATE TRIGGER rates_are_append_only
BEFORE UPDATE ON rates
WHEN OLD.superseded_at IS NOT NULL
  OR NEW.project_code      IS NOT OLD.project_code
  OR NEW.trade_code        IS NOT OLD.trade_code
  OR NEW.amount_pence      IS NOT OLD.amount_pence
  OR NEW.effective_from    IS NOT OLD.effective_from
  OR NEW.source_request_id IS NOT OLD.source_request_id
BEGIN
    SELECT RAISE(ABORT, 'rates are append-only: only superseded_at may be set, and only once');
END;

CREATE TRIGGER rates_are_never_deleted
BEFORE DELETE ON rates
BEGIN
    SELECT RAISE(ABORT, 'rates are append-only');
END;

CREATE TRIGGER decided_requests_are_final
BEFORE UPDATE ON rate_change_requests
WHEN OLD.status <> 'pending'
BEGIN
    SELECT RAISE(ABORT, 'a decided request cannot change');
END;

CREATE TRIGGER requests_are_not_edited
BEFORE UPDATE ON rate_change_requests
WHEN NEW.project_code    IS NOT OLD.project_code
  OR NEW.trade_code      IS NOT OLD.trade_code
  OR NEW.current_pence   IS NOT OLD.current_pence
  OR NEW.requested_pence IS NOT OLD.requested_pence
  OR NEW.reason          IS NOT OLD.reason
  OR NEW.requested_by    IS NOT OLD.requested_by
  OR NEW.requested_at    IS NOT OLD.requested_at
BEGIN
    SELECT RAISE(ABORT, 'a request cannot be edited after it is raised');
END;

CREATE TRIGGER requests_are_never_deleted
BEFORE DELETE ON rate_change_requests
BEGIN
    SELECT RAISE(ABORT, 'requests are never deleted');
END;

CREATE TRIGGER events_are_never_updated
BEFORE UPDATE ON events
BEGIN
    SELECT RAISE(ABORT, 'events are append-only');
END;

CREATE TRIGGER events_are_never_deleted
BEFORE DELETE ON events
BEGIN
    SELECT RAISE(ABORT, 'events are append-only');
END;
