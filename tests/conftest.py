"""Project-root conftest. Runs *before* any test module is imported.

Used here to disable LLM observability during tests — pytest discovers
``tests/conftest.py`` first, so this module-level code runs before any
``app.*`` import. We clobber the Langfuse env vars to empty so
``app.observability`` decides Langfuse is disabled at its import time,
which keeps the user's Langfuse dashboard free of test traces.

If you ever want a test that *does* exercise the real Langfuse client,
do it from a script outside the pytest tree, not from inside.
"""

from __future__ import annotations

import os

# Empty values short-circuit ``_enabled()`` in app.observability (it
# requires both keys non-empty). We set them explicitly here rather than
# popping so pydantic-settings doesn't fall back to .env values.
os.environ["LANGFUSE_PUBLIC_KEY"] = ""
os.environ["LANGFUSE_SECRET_KEY"] = ""
