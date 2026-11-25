"""
AP IGRS Configuration
"""
import os
from pathlib import Path
from dotenv import load_dotenv

# Project paths
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
DATA_DIR = PROJECT_ROOT / "data"
SEEDS_DIR = DATA_DIR / "verified_seeds"

# Load environment configuration
env_file = PROJECT_ROOT / ".env"
if env_file.exists():
    load_dotenv(env_file)
else:
    load_dotenv()

# Database
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./igrs_ap_local.db")

# Gemini Configuration
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")

# Runtime mode and secrets. Defaults are intentionally local-development only.
APP_ENV = os.getenv("APP_ENV", "development").strip().lower()
IS_PRODUCTION = APP_ENV == "production"
CARD_HMAC_SECRET = os.getenv("CARD_HMAC_SECRET", "")
ADMIN_API_TOKEN = os.getenv("ADMIN_API_TOKEN", "")
ALLOW_EXTERNAL_LLM = os.getenv("ALLOW_EXTERNAL_LLM", "false").strip().lower() == "true"
PRODUCTION_RATE_DATA_APPROVED = (
    os.getenv("PRODUCTION_RATE_DATA_APPROVED", "false").strip().lower() == "true"
)
SRO_OFFICE_CODE = os.getenv("SRO_OFFICE_CODE", "LOCAL-DEMO")
CORS_ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.getenv(
        "CORS_ALLOWED_ORIGINS",
        "http://localhost:3000,http://127.0.0.1:3000",
    ).split(",")
    if origin.strip()
]


def validate_runtime_config() -> None:
    """Fail closed when a production deployment lacks required secrets."""
    if IS_PRODUCTION:
        missing = [
            name for name, value in (
                ("CARD_HMAC_SECRET", CARD_HMAC_SECRET),
                ("ADMIN_API_TOKEN", ADMIN_API_TOKEN),
            ) if not value
        ]
        if missing:
            raise RuntimeError(
                "Production startup requires secrets: " + ", ".join(missing)
            )
        if len(CARD_HMAC_SECRET) < 32 or len(ADMIN_API_TOKEN) < 32:
            raise RuntimeError(
                "Production CARD_HMAC_SECRET and ADMIN_API_TOKEN must each be at least 32 characters"
            )

