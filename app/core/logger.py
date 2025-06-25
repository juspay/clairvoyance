import sys
from loguru import logger

# Remove the default sink to have full control over logging.
logger.remove()

# Define a format string with Loguru's color tags for stdout.
color_fmt = (
    "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
    "<level>{level: <8}</level> | "
    "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
    "<level>{message}</level>"
)

# Add a sink for standard output (stdout) with the specified color format.
logger.add(
    sys.stdout,
    level="DEBUG",
    format=color_fmt,
    enqueue=True,
    backtrace=False,
    colorize=True,
)

# Add a sink for standard error (stderr) with a modified color format for errors.
logger.add(
    sys.stderr,
    level="WARNING",
    format=color_fmt.replace("<green>", "<red>").replace("</green>", "</red>"),
    enqueue=True,
    backtrace=True,
    colorize=True,
)

# Export the configured logger for use throughout the application.
__all__ = ["logger"]