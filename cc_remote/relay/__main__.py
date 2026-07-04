"""Entry point: `python -m cc_remote.relay`."""
from __future__ import annotations

import uvicorn

from cc_remote.config import relay_config
from cc_remote.log import logger
from cc_remote.relay.server import create_app

log = logger("cc_remote.relay")


def main() -> None:
    cfg = relay_config()
    app = create_app(cfg)
    log.info("starting relay", host=cfg.host, port=cfg.port)
    uvicorn.run(app, host=cfg.host, port=cfg.port, log_level="info")


if __name__ == "__main__":
    main()
