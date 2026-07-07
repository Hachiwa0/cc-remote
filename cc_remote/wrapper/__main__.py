"""Entry point: `python -m cc_remote.wrapper`."""
from __future__ import annotations

import asyncio

from cc_remote.config import wrapper_config
from cc_remote.log import logger
from cc_remote.wrapper.machine import WrapperMachine
from cc_remote.wrapper.sdk import SdkHandle
from cc_remote.wrapper.transport import WrapperTransport

log = logger("cc_remote.wrapper")


async def main() -> None:
    cfg = wrapper_config()
    SdkHandle.preflight()
    log.info("starting wrapper", relay=cfg.relay_url, cwd=cfg.cc_cwd,
             resume=bool(cfg.resume_session_id))
    transport = WrapperTransport(cfg.relay_url, cfg.wrapper_token)
    machine = WrapperMachine(cfg, transport)
    try:
        await machine.run()
    except KeyboardInterrupt:
        log.info("shutting down (keyboard interrupt)")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
