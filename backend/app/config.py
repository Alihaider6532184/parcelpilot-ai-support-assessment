from pathlib import Path
import os

ROOT = Path(__file__).resolve().parents[2]
RAW_DIR = Path(os.getenv("PARCELPILOT_DATA_DIR", ROOT / "data" / "raw"))
RUNTIME_DIR = Path(os.getenv("PARCELPILOT_RUNTIME_DIR", ROOT / "runtime"))
CHROMA_DIR = RUNTIME_DIR / "chroma"
SQLITE_PATH = RUNTIME_DIR / "parcelpilot.sqlite3"
ACTION_PATH = RUNTIME_DIR / "actions.sqlite3"
SESSION_SECRET = os.getenv("SESSION_SECRET", "local-demo-only-change-me-32-byte-key")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
FRONTEND_ORIGIN = os.getenv("FRONTEND_ORIGIN", "http://localhost:3000")
MAX_TOOL_STEPS = int(os.getenv("MAX_TOOL_STEPS", "6"))
MODEL_DAILY_LIMIT = int(os.getenv("MODEL_DAILY_LIMIT", "40"))
