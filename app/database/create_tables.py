"""
Database table creation script.
This module handles the creation of all database tables and their indexes.
"""
from dotenv import load_dotenv
import asyncio
from app.database import init_db_pool, close_db_pool, get_db_connection

load_dotenv(override=True)

# Table names
CALL_DATA_TABLE = "call_data"

def create_call_data_table_query() -> str:
    """
    Generate query to create call_data table.
    """
    return f"""
        CREATE TABLE IF NOT EXISTS "{CALL_DATA_TABLE}" (
            "id" VARCHAR(255) PRIMARY KEY,
            "outcome" VARCHAR(50) CHECK ("outcome" IN ('CONFIRM', 'BUSY', 'CANCEL')),
            "transcription" JSONB,
            "call_start_time" TIMESTAMP WITH TIME ZONE NOT NULL,
            "call_end_time" TIMESTAMP WITH TIME ZONE,
            "call_id" VARCHAR(255),
            "provider" VARCHAR(255) NOT NULL,
            "status" VARCHAR(50) CHECK ("status" IN ('backlog', 'picked', 'finished', 'ongoing', 'error')) DEFAULT 'backlog',
            "requested_by" VARCHAR(50) CHECK ("requested_by" IN ('breeze', 'shopify')) NOT NULL,
            "call_payload" JSONB,
            "assigned_number" VARCHAR(50),
            "created_at" TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
            "updated_at" TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
        );
        
        CREATE INDEX IF NOT EXISTS idx_call_data_status ON "{CALL_DATA_TABLE}" ("status");
        CREATE INDEX IF NOT EXISTS idx_call_data_provider ON "{CALL_DATA_TABLE}" ("provider");
        CREATE INDEX IF NOT EXISTS idx_call_data_requested_by ON "{CALL_DATA_TABLE}" ("requested_by");
        CREATE INDEX IF NOT EXISTS idx_call_data_call_id ON "{CALL_DATA_TABLE}" ("call_id");
        CREATE INDEX IF NOT EXISTS idx_call_data_created_at ON "{CALL_DATA_TABLE}" ("created_at");
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

async def create_all_tables():
    """
    Create all database tables.
    """
    print("Starting database table creation...")
    
    try:
        # Create call_data table
        call_data_success = await create_call_data_table()
        
        if call_data_success:
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
            print("Usage: python -m app.database.create_tables [create|recreate|drop|check|list]")

if __name__ == "__main__":
    main()
