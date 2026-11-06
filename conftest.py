"""Tests must never start the contribution service's background workers."""
import os

os.environ.setdefault("OSSCOUT_DISABLE_AUTO_REPAIR", "1")
