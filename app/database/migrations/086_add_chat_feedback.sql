-- 086: buddy — customer feedback on widget chats.
--
-- chat_message.feedback: the customer's 👍/👎 on one bot reply, e.g.
--   {"rating": "down", "at": "2026-10-27T10:00:00Z"}
--   One rating per reply; a new tap replaces it, clearing it sets NULL.
--   This is the first column on chat_message that is written after insert
--   (027 made the log insert-only); the write is a single-row UPDATE by the
--   (session_id, idx) primary key.
--
-- chat_session.feedback: the end-of-chat card, asked once per chat, e.g.
--   {"pills": ["helped"], "comment": "...", "trigger": "event",
--    "at": "2026-10-27T10:05:00Z"}
--   or a skip: {"skipped": true, "trigger": "new_chat", "at": "..."}
--
-- Both nullable with no default: adding them is a metadata-only change, no
-- table rewrite. Shape is validated in code (no CHECK — the vocabulary of
-- pills is per-template data, not schema).
ALTER TABLE chat_message
    ADD COLUMN IF NOT EXISTS feedback jsonb;

ALTER TABLE chat_session
    ADD COLUMN IF NOT EXISTS feedback jsonb;
