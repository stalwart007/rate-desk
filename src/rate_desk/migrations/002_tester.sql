-- Schema version 2: a demo-only tester who can act as the other demo people.
--
-- The tester exists so that interviewers can try every role from the wall
-- dashboard with a single token. Every action they take records two people:
-- whose authority was used (requested_by / decided_by, as before) and who
-- actually pressed the button (the new *_by_proxy columns).

ALTER TABLE users ADD COLUMN can_act_as_others INTEGER NOT NULL DEFAULT 0
    CHECK (can_act_as_others IN (0, 1));

ALTER TABLE rate_change_requests ADD COLUMN raised_by_proxy INTEGER REFERENCES users (id);
ALTER TABLE rate_change_requests ADD COLUMN decided_by_proxy INTEGER REFERENCES users (id);

-- Who entered a request is one of its fixed facts, like who asked.
DROP TRIGGER requests_are_not_edited;

CREATE TRIGGER requests_are_not_edited
BEFORE UPDATE ON rate_change_requests
WHEN NEW.project_code    IS NOT OLD.project_code
  OR NEW.trade_code      IS NOT OLD.trade_code
  OR NEW.current_pence   IS NOT OLD.current_pence
  OR NEW.requested_pence IS NOT OLD.requested_pence
  OR NEW.reason          IS NOT OLD.reason
  OR NEW.requested_by    IS NOT OLD.requested_by
  OR NEW.requested_at    IS NOT OLD.requested_at
  OR NEW.raised_by_proxy IS NOT OLD.raised_by_proxy
BEGIN
    SELECT RAISE(ABORT, 'a request cannot be edited after it is raised');
END;
