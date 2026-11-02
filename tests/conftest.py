"""Shared pytest setup: TestClient sends ``Host: testserver``, which the Host guard would reject."""
import os

os.environ.setdefault("VINYLFLOW_ALLOWED_HOSTS", "testserver")
