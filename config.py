"""Central config. Loads .env so every other file shares one source of truth.

To move to Azure later, you only change values here / in .env -- no logic changes.
"""
import os

from dotenv import load_dotenv

load_dotenv()

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
DATABASE_URL = os.environ.get("DATABASE_URL", "")            # sales/use tax (CDTFA) DB
INCOME_DATABASE_URL = os.environ.get("INCOME_DATABASE_URL", "")  # income tax (FTB) DB -- Ring 2 database split
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "models/gemini-embedding-001")

# text-embedding-004 returns 768-dim vectors; the pgvector column must match.
EMBED_DIM = 768


def require(name: str, value: str) -> str:
    """Fail with a clear message instead of a confusing stack trace."""
    if not value:
        raise SystemExit(f"Missing {name}. Set it in .env (see .env.example).")
    return value
