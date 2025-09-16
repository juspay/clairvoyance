"""
Database table creation script.
This module handles the creation of all database tables and their indexes.
"""

from dotenv import load_dotenv
import asyncio
from app.database import init_db_pool, close_db_pool, get_db_connection

load_dotenv(override=True)

# Table names
OUTBOUND_NUMBERS_TABLE = "outbound_number"
CALL_EXECUTION_CONFIG_TABLE = "call_execution_config"
LEAD_CALL_TRACKER_TABLE = "lead_call_tracker"


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


async def create_all_tables():
    """
    Create all database tables.
    """
    print("Starting database table creation...")

    try:
        outbound_numbers_success = await create_outbound_numbers_table()
        call_execution_config_success = await create_call_execution_config_table()
        lead_call_tracker_success = await create_lead_call_tracker_table()

        if (
            outbound_numbers_success
            and call_execution_config_success
            and lead_call_tracker_success
        ):
            print("All database tables created successfully")
            return True
        else:
            print("Failed to create some database tables")
            return False

    except Exception as e:
        print(f"Error during table creation: {e}")
        return False


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
            print("Usage: python -m scripts.create_tables [create|list]")


if __name__ == "__main__":
    main()
