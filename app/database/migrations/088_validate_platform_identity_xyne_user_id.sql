-- 088: validate 087's two CHECKs (owner: platform).
--
-- Its own file, so its own transaction: VALIDATE CONSTRAINT takes SHARE
-- UPDATE EXCLUSIVE, which blocks neither the gate's probe nor a write, but
-- only once 087's ACCESS EXCLUSIVE lock has been released by its commit.
-- Both checks hold for every existing row by construction — the kind list
-- only grew, and no row has the new kind yet — so this cannot fail; it
-- makes the constraints count as proven for the planner and for pg_dump.

ALTER TABLE platform_identity VALIDATE CONSTRAINT platform_identity_kind_check;
ALTER TABLE platform_identity VALIDATE CONSTRAINT platform_identity_xyne_user_id_shape;
