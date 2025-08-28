"""
Database table creation script.
This module handles the creation of all database tables and their indexes.
"""
from dotenv import load_dotenv
import asyncio
from app.database import init_db_pool, close_db_pool, get_db_connection
from app.schemas import DailyRoomStatus

load_dotenv(override=True)

# Table names
CALL_DATA_TABLE = "call_data"
HOTLINE_ROOMS_TABLE = "daily_hotline_rooms"

def create_call_data_table_query() -> str:
    """
    Generate query to create call_data table.
    """
    return f"""
        CREATE TABLE IF NOT EXISTS "{CALL_DATA_TABLE}" (
            "id" VARCHAR(255) PRIMARY KEY,
            "outcome" VARCHAR(50) CHECK ("outcome" IN ('CONFIRM', 'BUSY', 'CANCEL', 'NO_ANSWER')),
            "transcription" JSONB,
            "call_start_time" TIMESTAMP WITH TIME ZONE NOT NULL,
            "call_end_time" TIMESTAMP WITH TIME ZONE,
            "call_id" VARCHAR(255),
            "provider" VARCHAR(255) NOT NULL,
            "status" VARCHAR(50) CHECK ("status" IN ('backlog', 'finished', 'ongoing', 'error')) DEFAULT 'backlog',
            "requested_by" VARCHAR(50) CHECK ("requested_by" IN ('breeze', 'shopify')) NOT NULL,
            "workflow" VARCHAR(50) NOT NULL,
            "call_payload" JSONB,
            "assigned_number" VARCHAR(50),
            "created_at" TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
            "updated_at" TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL
        );
        
        CREATE INDEX IF NOT EXISTS idx_call_data_status ON "{CALL_DATA_TABLE}" ("status");
        CREATE INDEX IF NOT EXISTS idx_call_data_provider ON "{CALL_DATA_TABLE}" ("provider");
        CREATE INDEX IF NOT EXISTS idx_call_data_requested_by ON "{CALL_DATA_TABLE}" ("requested_by");
        CREATE INDEX IF NOT EXISTS idx_call_data_workflow ON "{CALL_DATA_TABLE}" ("workflow");
        CREATE INDEX IF NOT EXISTS idx_call_data_call_id ON "{CALL_DATA_TABLE}" ("call_id");
        CREATE INDEX IF NOT EXISTS idx_call_data_created_at ON "{CALL_DATA_TABLE}" ("created_at");
    """

def create_daily_hotline_rooms_table_query() -> str:
    """
    Generate query to create daily_hotline_rooms table with optimized indexes.
    """
    return f"""
        CREATE TABLE IF NOT EXISTS "{HOTLINE_ROOMS_TABLE}" (
            "id" UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            "daily_room_url" TEXT NOT NULL,
            "daily_token" TEXT NOT NULL,
            "status" TEXT NOT NULL CHECK ("status" IN ('{DailyRoomStatus.AVAILABLE.value}', '{DailyRoomStatus.RESERVED.value}', '{DailyRoomStatus.IN_USE.value}')),
            "agent_pid" INTEGER,
            "session_id" TEXT,
            "created_at" TIMESTAMP DEFAULT NOW(),
            "expires_at" TIMESTAMP NOT NULL,
            "isactive" BOOLEAN NOT NULL DEFAULT true
        );
        
        -- Optimized indexes for daily hotline operations with soft delete support
        CREATE INDEX IF NOT EXISTS idx_daily_hotline_rooms_active_status_expires ON "{HOTLINE_ROOMS_TABLE}"("isactive", "status", "expires_at") WHERE "isactive" = true AND "expires_at" > NOW();
        CREATE INDEX IF NOT EXISTS idx_daily_hotline_rooms_active_expires ON "{HOTLINE_ROOMS_TABLE}"("isactive", "expires_at") WHERE "isactive" = true;
        CREATE INDEX IF NOT EXISTS idx_daily_hotline_rooms_active_agent_pid ON "{HOTLINE_ROOMS_TABLE}"("isactive", "agent_pid") WHERE "isactive" = true AND "agent_pid" IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_daily_hotline_rooms_isactive ON "{HOTLINE_ROOMS_TABLE}"("isactive");
    """

async def create_call_data_table():
    """
    Create the call_data table with all constraints and indexes.
    """
    try:
        async for conn in get_db_connection():
            print("Creating call_data table...")
            await conn.execute(create_call_data_table_query())
            print("Call data table created successfully")
            return True
    except Exception as e:
        print(f"Error creating call_data table: {e}")
        return False

async def create_daily_hotline_rooms_table():
    """
    Create the daily_hotline_rooms table with all constraints and indexes.
    """
    try:
        async for conn in get_db_connection():
            print("Creating daily_hotline_rooms table...")
            await conn.execute(create_daily_hotline_rooms_table_query())
            print("Daily hotline rooms table created successfully")
            
            # Log table structure for verification
            result = await conn.fetch("""
                SELECT column_name, data_type, is_nullable
                FROM information_schema.columns
                WHERE table_name = 'daily_hotline_rooms'
                ORDER BY ordinal_position;
            """)
            
            print("Daily hotline rooms table structure:")
            for row in result:
                print(f"  {row['column_name']}: {row['data_type']} ({'NULL' if row['is_nullable'] == 'YES' else 'NOT NULL'})")
            
            return True
    except Exception as e:
        print(f"Error creating daily_hotline_rooms table: {e}")
        return False

async def create_all_tables():
    """
    Create all database tables.
    """
    print("Starting database table creation...")
    
    try:
        # Create call_data table
        call_data_success = await create_call_data_table()
        
        # Create daily_hotline_rooms table
        hotline_success = await create_daily_hotline_rooms_table()
        
        if call_data_success and hotline_success:
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
            return [row['table_name'] for row in rows]
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
            async def run_create():
                await init_db_pool()
                try:
                    await create_all_tables()
                finally:
                    await close_db_pool()
            asyncio.run(run_create())
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
            print("Usage: python -m app.scripts.create_tables [create|list]")
    else:
        print("Usage: python -m app.scripts.create_tables [create|list]")

if __name__ == "__main__":
    main()
