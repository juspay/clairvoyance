-- 085: connectivity — crm_connector_installation.door_data + door_name (T11)
--
-- door_data: the door's own non-secret settings, written by the connector's
-- onboarder: a Xyne app's {"base_url": "https://spaces.xyne.juspay.net"}
-- today. Where an account's API lives is a fact ABOUT the door, not a secret,
-- so it sits on the row rather than in the vault — readable without a
-- decrypt, and visible on the connections screen. Shape lives in code (each
-- connector's onboarder decides its own keys), so the only CHECK is on
-- FORMAT: an object, never a scalar or an array.
--
-- door_name: an optional name the merchant gives one account, so a workflow
-- action can say WHICH door it acts through when a merchant holds several on
-- one connector. NULL means unnamed — such a door is only ever reached as the
-- connector's newest usable account. A name is unique per merchant and
-- connector (merchant_id leads, as on every crm_ unique index); the CHECK is
-- on FORMAT: a plain token, because it is written in plan documents.

ALTER TABLE crm_connector_installation
    ADD COLUMN door_data jsonb NOT NULL DEFAULT '{}'::jsonb
        CONSTRAINT crm_connector_installation_door_data_object
        CHECK (jsonb_typeof(door_data) = 'object'),
    ADD COLUMN door_name text
        CONSTRAINT crm_connector_installation_door_name_format
        CHECK (door_name ~ '^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$');

CREATE UNIQUE INDEX crm_connector_installation_door_name_uq
    ON crm_connector_installation (merchant_id, connector_key, door_name)
    WHERE door_name IS NOT NULL;
