-- Schema version 3: the database also guards the tester columns added in version 2.
--
-- Version 2 added columns with ALTER TABLE, which cannot extend the table's
-- existing CHECK constraints, so these rules are triggers instead.

-- Only the demo tester can be recorded as having acted for someone else.
CREATE TRIGGER raised_by_proxy_is_a_tester
BEFORE INSERT ON rate_change_requests
WHEN NEW.raised_by_proxy IS NOT NULL
 AND NOT EXISTS (SELECT 1 FROM users WHERE id = NEW.raised_by_proxy AND can_act_as_others = 1)
BEGIN
    SELECT RAISE(ABORT, 'only the demo tester can raise a request for someone else');
END;

-- A decision made by the tester is recorded on a decided request, and only for the tester.
CREATE TRIGGER decided_by_proxy_is_a_tester
BEFORE UPDATE OF decided_by_proxy ON rate_change_requests
WHEN NEW.decided_by_proxy IS NOT NULL
 AND (NEW.status = 'pending'
      OR NOT EXISTS (SELECT 1 FROM users WHERE id = NEW.decided_by_proxy AND can_act_as_others = 1))
BEGIN
    SELECT RAISE(ABORT, 'only the demo tester can decide for someone else, and only as a decision');
END;

-- Whether a user can act as others is fixed when the user is created.
CREATE TRIGGER tester_capability_is_fixed
BEFORE UPDATE OF can_act_as_others ON users
WHEN NEW.can_act_as_others IS NOT OLD.can_act_as_others
BEGIN
    SELECT RAISE(ABORT, 'whether a user can act as others is fixed when the user is created');
END;
