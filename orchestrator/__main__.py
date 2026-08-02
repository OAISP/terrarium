"""Run the orchestrator API: ``terrarium`` or ``python -m orchestrator``."""

from __future__ import annotations

import os


def main() -> None:
    import uvicorn

    host = os.environ.get("TERRA_HOST", "127.0.0.1")
    port = int(os.environ.get("TERRA_PORT", "8900"))
    # import string enables reload; factory builds the app with env config
    uvicorn.run("orchestrator.api:create_app", host=host, port=port, factory=True)


if __name__ == "__main__":
    main()
