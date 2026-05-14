"""pytest configuration for api/."""
import sys
from pathlib import Path

# Make api/ importable as `api`.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
