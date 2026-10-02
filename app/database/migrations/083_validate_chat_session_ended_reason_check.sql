-- 083: buddy — validate chat_session_ended_reason_check against the rows
-- that existed before 082 widened it.
--
-- 082 re-added the constraint NOT VALID so its own transaction never scanned
-- the table under ACCESS EXCLUSIVE. VALIDATE CONSTRAINT takes SHARE UPDATE
-- EXCLUSIVE, which lets widget chat keep reading and writing while every
-- existing row is checked. Every value 027 allowed is still allowed, so this
-- cannot fail on data.
ALTER TABLE chat_session
    VALIDATE CONSTRAINT chat_session_ended_reason_check;
