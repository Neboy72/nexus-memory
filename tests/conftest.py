"""Pytest configuration — set NEXUS_COLLECTION for all tests."""

import os

# Set a test collection name before any nexus module imports
os.environ.setdefault("NEXUS_COLLECTION", "test-collection")
