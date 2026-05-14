"""pytest configuration for api_app/."""
import sys
from pathlib import Path

# Make api_app/ importable as `api_app` and api/ importable as plain `services.*`
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "api"))
