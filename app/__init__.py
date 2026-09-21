"""Intraday notification system for contact center operations."""

import sys

# Checked here, before any submodule imports, so an unsupported interpreter
# reports itself plainly instead of failing deep in a dataclass definition.
if sys.version_info < (3, 11):
    raise RuntimeError(
        "This project needs Python 3.11 or newer, but is running on "
        f"{sys.version.split()[0]} ({sys.executable}).\n"
        "Rebuild the virtualenv against a newer interpreter:\n"
        "    rm -rf .venv && python3.11 -m venv .venv\n"
        "    source .venv/bin/activate && pip install -r requirements.txt"
    )
