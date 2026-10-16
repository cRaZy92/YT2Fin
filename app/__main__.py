from __future__ import annotations

import sys

import uvicorn

from app.config import load_config
from app.main import create_app


def main() -> None:
    config = load_config()
    app = create_app(config)
    server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=config.port, log_config=None))
    app.state.server = server
    server.run()
    sys.exit(app.state.exit_code)


if __name__ == "__main__":
    main()
