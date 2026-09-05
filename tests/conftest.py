"""Keep import-time gateway state initialization away from user data."""
import os
from pathlib import Path
import sys
import tempfile


def pytest_sessionstart(session):
    repository = Path(__file__).resolve().parents[1]
    if str(repository) not in sys.path:
        sys.path.insert(0, str(repository))
    session._bridge_original_directory = Path.cwd()
    # Retain the temporary directory for inspection. Some imported log writers
    # own open SQLite handles until process exit, especially on Windows.
    session._bridge_runtime_directory = Path(tempfile.mkdtemp(prefix='llmbridge-tests-'))
    os.chdir(session._bridge_runtime_directory)


def pytest_sessionfinish(session, exitstatus):
    original = getattr(session, '_bridge_original_directory', None)
    if original is not None:
        os.chdir(original)
