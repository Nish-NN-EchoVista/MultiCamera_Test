"""Entrypoint for the Echovista Multi-Camera Controller.

    python main.py

Startup order matters: logging first so config problems are recorded, then
config, then the UI. Nothing connects automatically -- every device starts
disconnected, as specified.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import customtkinter as ctk

from emcc import fonts
from emcc.app import App
from emcc.backend.config_manager import ConfigManager
from emcc.backend.logging_setup import setup_logging, shutdown_logging

logger = logging.getLogger("emcc")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Echovista Multi-Camera Controller (EMCC)."
    )
    parser.add_argument(
        "--config", type=Path, default=Path("config.json"),
        help="path to config.json (default: ./config.json)",
    )
    parser.add_argument(
        "--log-dir", type=Path, default=Path("logs"),
        help="directory for rotating logs (default: ./logs)",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="log at DEBUG level",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    level = logging.DEBUG if args.verbose else logging.INFO
    log_path = setup_logging(level=level, log_dir=args.log_dir)
    logger.info("=" * 62)
    logger.info("EMCC starting (python %s)", sys.version.split()[0])
    if log_path:
        logger.info("logging to %s", log_path)

    config = ConfigManager(args.config)
    config.load()

    # The design is dark-only; there is no light palette to switch to.
    ctk.set_appearance_mode("dark")

    app: App | None = None
    try:
        app = App(config=config)
        fonts.init()
        logger.info("fonts resolved:\n%s", fonts.report())
        app.mainloop()
    except Exception:
        logger.exception("fatal error")
        return 1
    finally:
        # mainloop() returns after destroy(), but an exception could arrive
        # first -- make sure workers and config are dealt with either way.
        if app is not None:
            try:
                app.shutdown()
            except Exception:
                logger.exception("shutdown during teardown failed")
        else:
            config.close()
        shutdown_logging()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
