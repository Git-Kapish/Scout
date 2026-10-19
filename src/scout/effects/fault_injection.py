"""Fault Injection Subsystem (PRD FR-4.6).

Exposes named crash points that invoke os._exit(137) to test recovery
at critical execution boundaries without relying on imprecise external SIGKILL.
Compiled out in production (only active when SCOUT_FAULTS_ENABLED=1).
"""

from __future__ import annotations

import logging
import os
from typing import Dict

logger = logging.getLogger(__name__)

# Counter for crash points that can fire on nth invocation
_HIT_COUNTS: Dict[str, int] = {}


def maybe_crash(point: str) -> None:
    """Checks if fault injection is enabled and if the specified crash point matches.

    Usage:
        SCOUT_FAULTS_ENABLED=1
        SCOUT_CRASH_AT=after_intent_before_dispatch
        or
        SCOUT_CRASH_AT=mid_extraction:2  (crashes on 2nd hit of mid_extraction)
    """
    if os.getenv("SCOUT_FAULTS_ENABLED") != "1":
        return

    target = os.getenv("SCOUT_CRASH_AT")
    if not target:
        return

    # Parse point and target hit count
    target_count = 1
    if ":" in target:
        target_point, count_str = target.split(":", 1)
        try:
            target_count = int(count_str)
        except ValueError:
            target_count = 1
    else:
        target_point = target

    if point != target_point:
        return

    current_count = _HIT_COUNTS.get(point, 0) + 1
    _HIT_COUNTS[point] = current_count

    if current_count >= target_count:
        logger.critical(
            "*** FAULT INJECTION CRASH TRIGGERED: point='%s' (hit %d/%d) -> os._exit(137) ***",
            point,
            current_count,
            target_count,
        )
        # Flush stdout/stderr before abrupt exit
        try:
            import sys
            sys.stdout.flush()
            sys.stderr.flush()
        except Exception:
            pass
        os._exit(137)


def reset_fault_counters() -> None:
    """Resets hit counters for test harness."""
    _HIT_COUNTS.clear()
