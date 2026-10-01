"""Startpunkt: python -m bot

Startet die Bot-Engine (NUR SPIELGELD). Beenden mit Strg + C.
"""

from __future__ import annotations

import sys


def main() -> int:
    from bot.config import ConfigError, load_config
    from bot.logging_setup import setup_logging

    try:
        cfg = load_config()
    except ConfigError as exc:
        print(str(exc))
        print("\nBitte config.yaml korrigieren und den Bot erneut starten.")
        return 1
    setup_logging(cfg.general.log_path, cfg.general.display_tz)

    from bot.engine import AlreadyRunningError, Engine
    from bot.learning.manager import LearningManager

    engine = Engine(cfg, learning=LearningManager(cfg))
    try:
        engine.run_forever()
    except AlreadyRunningError as exc:
        print(str(exc))
        return 2
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
