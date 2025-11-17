"""Health check endpoints."""

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app import __version__
from app.database import get_db_connection

router = APIRouter()


@router.get("")
async def health_check():
    """General health check endpoint."""
    return JSONResponse({"status": "healthy"})


@router.get("/database")
async def database_health_check():
    """Check database connectivity and health."""
    try:
        async for conn in get_db_connection():
            result = await conn.fetchval("SELECT 1")
            if result == 1:
                return JSONResponse(
                    {
                        "status": "healthy",
                        "database": "connected",
                        "message": "Database connection is healthy",
                    }
                )
            else:
                return JSONResponse(
                    status_code=400,
                    content={
                        "status": "unhealthy",
                        "database": "error",
                        "message": "Database query returned unexpected result",
                    },
                )
    except Exception as e:
        return JSONResponse(
            status_code=400,
            content={
                "status": "unhealthy",
                "database": "disconnected",
                "message": f"Database connection failed: {str(e)}",
            },
        )
