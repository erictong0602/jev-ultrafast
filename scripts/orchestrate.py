"""Shim for in-checkout use; the implementation lives in jev_ultrafast.orchestrate.

  uv run --env-file .env python scripts/orchestrate.py --tasks tasks.jsonl --jobs 3
"""

from jev_ultrafast.orchestrate import main

if __name__ == "__main__":
    main()
