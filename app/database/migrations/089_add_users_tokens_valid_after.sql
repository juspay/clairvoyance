-- 089: users.tokens_valid_after — the login-token cut-off (token registry 3/4).
--
-- Login, launch and signup tokens are short-lived and never registered, so
-- they can't be revoked one by one. Instead every verifier refuses a
-- short-lived token whose subject is this user and whose `iat` is before
-- this stamp. Written by POST /user/{id}/revoke-sessions and whenever the
-- user is deactivated (so re-activating them doesn't bring old logins back).
--
-- Long-lived (registered) tokens are NOT cut off by this: they are revoked
-- or rotated one by one, so kicking a shared admin's dashboard sessions
-- doesn't kill the service tokens that admin minted. NULL = no cut-off.

ALTER TABLE users ADD COLUMN IF NOT EXISTS tokens_valid_after timestamptz;
