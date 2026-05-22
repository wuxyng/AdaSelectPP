# -*- coding: utf-8 -*-
"""util/logging_utils.py

Centralized logging setup for reproducible experiments.

Goal:
- Default runs are clean: INFO to console and file.
- When debugging: fully visible: DEBUG with file/line context.

This configures the *root* logger, so all module loggers
(logging.getLogger(__name__)) inherit the same handlers.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Iterable, Optional, Union


def setup_logging(
    log_path: Union[str, Path],
    *,
    debug: bool = False,
    console_level: Optional[int] = None,
    file_level: Optional[int] = None,
    quiet_libs: bool = True,
    clear_root_handlers: bool = True,
) -> None:
    """Configure root logging.

    Args:
        log_path: Path to the log file.
        debug: If True, enable verbose DEBUG logging.
        console_level: Override console handler level.
        file_level: Override file handler level.
        quiet_libs: If True, suppress noisy third-party loggers.
        clear_root_handlers: If True, remove existing root handlers.
    """

    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    base_level = logging.DEBUG if debug else logging.INFO
    ch_level = console_level if console_level is not None else base_level
    fh_level = file_level if file_level is not None else base_level

    # Root logger must be at least as permissive as the most-verbose handler.
    root_level = min(ch_level, fh_level)

    root = logging.getLogger()
    root.setLevel(root_level)

    if clear_root_handlers:
        for h in list(root.handlers):
            root.removeHandler(h)

    if debug:
        fmt = logging.Formatter(
            '%(asctime)s [%(levelname)s] %(name)s:%(lineno)d %(message)s'
        )
    else:
        # Keep it readable for long benchmark runs.
        fmt = logging.Formatter('%(asctime)s %(levelname)s %(name)s %(message)s')

    fh = logging.FileHandler(log_path, mode='a', encoding='utf-8')
    fh.setLevel(fh_level)
    fh.setFormatter(fmt)
    root.addHandler(fh)

    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(ch_level)
    sh.setFormatter(fmt)
    root.addHandler(sh)

    # Avoid double-print if a library configures its own handler.
    root.propagate = False

    if quiet_libs:
        _quiet_logger_names: Iterable[str] = (
            'psycopg2',
            'urllib3',
            'matplotlib',
            'PIL',
            'asyncio',
            'concurrent',
        )
        for name in _quiet_logger_names:
            logging.getLogger(name).setLevel(logging.WARNING)

