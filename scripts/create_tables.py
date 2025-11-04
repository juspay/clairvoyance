"""
Database table creation script.
This module handles the creation of all database tables and their indexes.
"""

import asyncio

from dotenv import load_dotenv

from app.database import close_db_pool, get_db_connection, init_db_pool

load_dotenv(override=True)

# Table names
OUTBOUND_NUMBERS_TABLE = "outbound_number"
CALL_EXECUTION_CONFIG_TABLE = "call_execution_config"
LEAD_CALL_TRACKER_TABLE = "lead_call_tracker"
CONVERSATIONS_TABLE = "conversations"
CONVERSATION_MESSAGES_TABLE = "conversation_messages"


def create_lead_call_tracker_table_query() -> str:
    """
    Generate query to create lead_call_tracker table.
    """
    return f"""
        CREATE TABLE IF NOT EXISTS "{LEAD_CALL_TRACKER_TABLE}" (
            "id" VARCHAR(255) PRIMARY KEY,
            "outbound_number_id" VARCHAR(255),
            "merchant_id" VARCHAR(100) NOT NULL,
            "workflow" VARCHAR(50) CHECK ("workflow" IN ('order-confirmation')) NOT NULL,
            "attempt_count" INTEGER DEFAULT 0,
            "next_attempt_at" TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            "payload" JSONB,
            "meta_data" JSONB,
            "recording_url" VARCHAR(500),
            "status" VARCHAR(50) CHECK ("status" IN ('BACKLOG', 'PROCESSING', 'FINISHED', 'RETRY')) NOT NULL,
            "outcome" VARCHAR(50) CHECK ("outcome" IN ('NO_ANSWER', 'BUSY', 'CANCEL', 'CONFIRM', 'UNKNOWN', 'ADDRESS_UPDATED')),
            "call_id" VARCHAR(100),
            "call_initiated_time" TIMESTAMP WITH TIME ZONE,
            "call_end_time" TIMESTAMP WITH TIME ZONE,
            "cost" REAL,
            "created_at" TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
            "updated_at" TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL
        );
        CREATE INDEX IF NOT EXISTS "idx_lead_call_tracker_merchant_id" ON "{LEAD_CALL_TRACKER_TABLE}" ("merchant_id");
        CREATE INDEX IF NOT EXISTS "idx_lead_call_tracker_status" ON "{LEAD_CALL_TRACKER_TABLE}" ("status");
        CREATE INDEX IF NOT EXISTS "idx_lead_call_tracker_outcome" ON "{LEAD_CALL_TRACKER_TABLE}" ("outcome");
        CREATE INDEX IF NOT EXISTS "idx_lead_call_tracker_created_at" ON "{LEAD_CALL_TRACKER_TABLE}" ("created_at");
    """


def create_call_execution_config_table_query() -> str:
    """
    Generate query to create call_execution_configs table.
    """
    return f"""
        CREATE TABLE IF NOT EXISTS "{CALL_EXECUTION_CONFIG_TABLE}" (
            "id" VARCHAR(255) PRIMARY KEY,
            "initial_offset" INTEGER NOT NULL,
            "retry_offset" INTEGER NOT NULL,
            "call_start_time" TIME NOT NULL,
            "call_end_time" TIME NOT NULL,
            "max_retry" INTEGER NOT NULL,
            "calling_provider" VARCHAR(50) CHECK ("calling_provider" IN ('TWILIO', 'EXOTEL')) NOT NULL,
            "merchant_id" VARCHAR(255) NOT NULL,
            "workflow" VARCHAR(50) CHECK ("workflow" IN ('order-confirmation')) NOT NULL,
            "created_at" TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
            "updated_at" TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
            UNIQUE("merchant_id", "workflow")
        );
        CREATE INDEX IF NOT EXISTS "idx_call_execution_config_created_at" ON "{CALL_EXECUTION_CONFIG_TABLE}" ("created_at");
    """


def create_conversations_table_query() -> str:
    """
    Generate query to create conversations table.
    """
    return f"""
        CREATE TABLE IF NOT EXISTS "{CONVERSATIONS_TABLE}" (
            "id" UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            "session_id" VARCHAR(255) UNIQUE NOT NULL,
            "client_sid" VARCHAR(255),
            "merchant_id" VARCHAR(255),
            "user_email" VARCHAR(255),
            "user_name" VARCHAR(255),
            "shop_id" VARCHAR(255),
            "shop_url" VARCHAR(255),
            "reseller_id" VARCHAR(255),
            "mode" VARCHAR(50),
            "status" VARCHAR(50) DEFAULT 'active' NOT NULL,
            "summary" TEXT,
            "message_count" INTEGER DEFAULT 0,
            "started_at" TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
            "last_activity_at" TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            "completed_at" TIMESTAMP WITH TIME ZONE,
            "metadata" JSONB,
            "created_at" TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
            "updated_at" TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL
        );

        CREATE INDEX IF NOT EXISTS "idx_conversations_session_id" ON "{CONVERSATIONS_TABLE}" ("session_id");
        CREATE INDEX IF NOT EXISTS "idx_conversations_merchant_id" ON "{CONVERSATIONS_TABLE}" ("merchant_id");
        CREATE INDEX IF NOT EXISTS "idx_conversations_user_email" ON "{CONVERSATIONS_TABLE}" ("user_email");
        CREATE INDEX IF NOT EXISTS "idx_conversations_shop_id" ON "{CONVERSATIONS_TABLE}" ("shop_id");
        CREATE INDEX IF NOT EXISTS "idx_conversations_status" ON "{CONVERSATIONS_TABLE}" ("status");
        CREATE INDEX IF NOT EXISTS "idx_conversations_started_at" ON "{CONVERSATIONS_TABLE}" ("started_at" DESC);
        CREATE INDEX IF NOT EXISTS "idx_conversations_user_lookup" ON "{CONVERSATIONS_TABLE}" ("merchant_id", "user_email", "started_at" DESC);
        CREATE INDEX IF NOT EXISTS "idx_conversations_shop_lookup" ON "{CONVERSATIONS_TABLE}" ("merchant_id", "shop_id", "started_at" DESC);
    """


def create_conversation_messages_table_query() -> str:
    """
    Generate query to create conversation_messages table.
    """
    return f"""
        CREATE TABLE IF NOT EXISTS "{CONVERSATION_MESSAGES_TABLE}" (
            "id" UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            "conversation_id" UUID NOT NULL,
            "role" VARCHAR(20) NOT NULL,
            "content" TEXT NOT NULL,
            "sequence_number" INTEGER NOT NULL,
            "timestamp" TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
            "created_at" TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
            CONSTRAINT "fk_conversation_id" FOREIGN KEY ("conversation_id")
                REFERENCES "{CONVERSATIONS_TABLE}" ("id") ON DELETE CASCADE,
            CONSTRAINT "unique_conversation_sequence" UNIQUE ("conversation_id", "sequence_number")
        );

        CREATE INDEX IF NOT EXISTS "idx_conversation_messages_conversation_id" ON "{CONVERSATION_MESSAGES_TABLE}" ("conversation_id");
        CREATE INDEX IF NOT EXISTS "idx_conversation_messages_sequence" ON "{CONVERSATION_MESSAGES_TABLE}" ("conversation_id", "sequence_number");
        CREATE INDEX IF NOT EXISTS "idx_conversation_messages_timestamp" ON "{CONVERSATION_MESSAGES_TABLE}" ("timestamp" DESC);
    """


def create_outbound_numbers_table_query() -> str:
    """
    Generate query to create outbound_numbers table.
    """
    return f"""
        CREATE TABLE IF NOT EXISTS "{OUTBOUND_NUMBERS_TABLE}" (
            "id" VARCHAR(255) PRIMARY KEY,
            "number" VARCHAR(20) NOT NULL UNIQUE,
            "provider" VARCHAR(50) CHECK ("provider" IN ('TWILIO', 'EXOTEL')) NOT NULL,
            "status" VARCHAR(50) CHECK ("status" IN ('AVAILABLE', 'IN_USE', 'DISABLED')) NOT NULL,
            "channels" INTEGER,
            "maximum_channels" INTEGER,
            "created_at" TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
            "updated_at" TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_outbound_numbers_status ON "{OUTBOUND_NUMBERS_TABLE}" ("status");
        CREATE INDEX IF NOT EXISTS idx_outbound_numbers_provider ON "{OUTBOUND_NUMBERS_TABLE}" ("provider");
    """


async def create_outbound_numbers_table():
    """
    Create the outbound_numbers table with all constraints and indexes.
    """
    try:
        async for conn in get_db_connection():
            print("Creating outbound_numbers table...")
            await conn.execute(create_outbound_numbers_table_query())
            print("Outbound numbers table created successfully")
            return True
    except Exception as e:
        print(f"Error creating outbound_numbers table: {e}")
        return False


async def create_call_execution_config_table():
    """
    Create the call_execution_configs table with all constraints and indexes.
    """
    try:
        async for conn in get_db_connection():
            print("Creating call_execution_configs table...")
            await conn.execute(create_call_execution_config_table_query())
            print("Call execution configs table created successfully")
            return True
    except Exception as e:
        print(f"Error creating call_execution_configs table: {e}")
        return False


async def create_lead_call_tracker_table():
    """
    Create the lead_call_tracker table with all constraints and indexes.
    """
    try:
        async for conn in get_db_connection():
            print("Creating lead_call_tracker table...")
            await conn.execute(create_lead_call_tracker_table_query())
            print("Lead call tracker table created successfully")
            return True
    except Exception as e:
        print(f"Error creating lead_call_tracker table: {e}")
        return False


async def create_conversations_table():
    """
    Create the conversations table with all constraints and indexes.
    """
    try:
        async for conn in get_db_connection():
            print("Creating conversations table...")
            await conn.execute(create_conversations_table_query())
            print("Conversations table created successfully")
            return True
    except Exception as e:
        print(f"Error creating conversations table: {e}")
        return False


async def create_conversation_messages_table():
    """
    Create the conversation_messages table with all constraints and indexes.
    """
    try:
        async for conn in get_db_connection():
            print("Creating conversation_messages table...")
            await conn.execute(create_conversation_messages_table_query())
            print("Conversation messages table created successfully")
            return True
    except Exception as e:
        print(f"Error creating conversation_messages table: {e}")
        return False


async def create_all_tables():
    """
    Create all database tables.
    """
    print("Starting database table creation...")

    try:
        outbound_numbers_success = await create_outbound_numbers_table()
        call_execution_config_success = await create_call_execution_config_table()
        lead_call_tracker_success = await create_lead_call_tracker_table()
        conversations_success = await create_conversations_table()
        conversation_messages_success = await create_conversation_messages_table()

        if (
            outbound_numbers_success
            and call_execution_config_success
            and lead_call_tracker_success
            and conversations_success
            and conversation_messages_success
        ):
            print("All database tables created successfully")
            return True
        else:
            print("Failed to create some database tables")
            return False

    except Exception as e:
        print(f"Error during table creation: {e}")
        return False


async def alter_existing_tables():
    """
    Alter existing tables.
    """
    try:
        async for conn in get_db_connection():
            print("Altering tables...")
            await conn.execute(alter_tables_query())
            print("Tables altered successfully.")
            return True
    except Exception as e:
        print(f"Error altering tables: {e}")
        return False


def alter_tables_query() -> str:
    """
    Generate queries to alter existing tables.
    """
    return f"""
        ALTER TABLE "{LEAD_CALL_TRACKER_TABLE}" ADD COLUMN IF NOT EXISTS "shop_identifier" VARCHAR(255);
        ALTER TABLE "{CALL_EXECUTION_CONFIG_TABLE}" ADD COLUMN IF NOT EXISTS "shop_identifier" VARCHAR(255);
        ALTER TABLE "{CALL_EXECUTION_CONFIG_TABLE}" ADD COLUMN IF NOT EXISTS "enable_international_call" BOOLEAN DEFAULT TRUE;
        ALTER TABLE "{CALL_EXECUTION_CONFIG_TABLE}" DROP CONSTRAINT IF EXISTS "call_execution_config_merchant_id_workflow_key";
        CREATE UNIQUE INDEX IF NOT EXISTS "uq_call_execution_config_shop"
            ON "{CALL_EXECUTION_CONFIG_TABLE}" ("merchant_id", "workflow", "shop_identifier")
            WHERE "shop_identifier" IS NOT NULL;
        CREATE UNIQUE INDEX IF NOT EXISTS "uq_call_execution_config_generic"
            ON "{CALL_EXECUTION_CONFIG_TABLE}" ("merchant_id", "workflow")
            WHERE "shop_identifier" IS NULL;
    """


async def list_all_tables():
    """
    List all tables in the database.
    """
    try:
        async for conn in get_db_connection():
            query = """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'public'
                ORDER BY table_name;
            """
            rows = await conn.fetch(query)
            return [row["table_name"] for row in rows]
    except Exception as e:
        print(f"Error listing tables: {e}")
        return []


def main():
    """
    Main function to run table creation.
    """
    import sys

    if len(sys.argv) > 1:
        command = sys.argv[1].lower()

        if command == "create":
            asyncio.run(create_all_tables())
        elif command == "alter":
            asyncio.run(alter_existing_tables())
        elif command == "list":

            async def list_tables():
                await init_db_pool()
                try:
                    tables = await list_all_tables()
                    print("Database tables:")
                    for table in tables:
                        print(f"  - {table}")
                finally:
                    await close_db_pool()

            asyncio.run(list_tables())
        else:
            print("Usage: python -m scripts.create_tables [create|list|alter]")


if __name__ == "__main__":
    main()
