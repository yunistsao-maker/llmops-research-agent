"""Make the agent's own modules (app/llmops_research/research) importable from scripts/ without installing anything."""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP_DIR = PROJECT_ROOT / "app" / "llmops_research"

if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))
