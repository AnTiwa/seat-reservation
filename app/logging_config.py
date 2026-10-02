"""
Logging configuration for the Seat Reservation Service.

On every startup a new timestamped log file is created inside the
``logs/`` directory so that each Docker run has its own file.
Structured JSON is written to both stdout and the file so that
console and file tails are identical.
"""
import logging
import logging.handlers
import os
import sys
from datetime import datetime, timezone


LOG_DIR = os.environ.get("LOG_DIR", "logs")


def _ensure_log_dir() -> str:
    os.makedirs(LOG_DIR, exist_ok=True)
    return LOG_DIR


class _JsonFormatter(logging.Formatter):
    """Format every log record as a single-line JSON object."""

    import json as _json

    def format(self, record: logging.LogRecord) -> str:  # type: ignore[override]
        import json

        # If the message is already valid JSON (as emitted by the
        # middleware), just pass it through.  Otherwise wrap it.
        try:
            json.loads(record.getMessage())
            return record.getMessage()
        except (ValueError, TypeError):
            return json.dumps(
                {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "level": record.levelname,
                    "logger": record.name,
                    "msg": record.getMessage(),
                    **(
                        {"exc_info": self.formatException(record.exc_info)}
                        if record.exc_info
                        else {}
                    ),
                },
                ensure_ascii=False,
            )


def configure_logging() -> None:
    """
    Call once at application startup.

    Sets up:
    - A StreamHandler (stdout) with JSON formatting
    - A RotatingFileHandler targeting logs/<timestamp>.log
    """
    log_dir = _ensure_log_dir()

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_file = os.path.join(log_dir, f"app-{ts}.log")

    formatter = _JsonFormatter()

    # --- stdout handler -------------------------------------------------
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(formatter)

    # --- file handler (10 MB × 5 backups, though we start fresh each run)
    file_handler = logging.handlers.RotatingFileHandler(
        log_file,
        maxBytes=10 * 1024 * 1024,  # 10 MB
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(logging.INFO)

    # Remove any handlers added by basicConfig earlier.
    root.handlers.clear()
    root.addHandler(stdout_handler)
    root.addHandler(file_handler)

    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

    logging.getLogger("seat-service").info(
        __import__("json").dumps(
            {
                "event": "logging_initialized",
                "log_file": log_file,
            }
        )
    )
