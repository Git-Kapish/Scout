"""Admission Control for Research Requests (PRD §5.2, §8, FR-5.1).

Rejects requests with HTTP 429 daily_allowance if projected calls exceed
remaining daily allowance based on measured provider rate limits and headroom.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

import redis

from scout.api.schemas import AdmissionErrorResponse, ResearchOptions
from scout.config import get_settings
from scout.store.ledgers import LedgerStore

logger = logging.getLogger(__name__)


class AdmissionController:
    def __init__(self, r: redis.Redis):
        self.r = r
        self.ledgers = LedgerStore(r)

    def check_admission(
        self,
        options: Optional[ResearchOptions] = None,
        provider_override: Optional[str] = None,
    ) -> Tuple[bool, Optional[AdmissionErrorResponse]]:
        """Evaluates whether the research run can be admitted under today's quota.

        Returns:
            (is_admitted, error_response_if_rejected)
        """
        settings = get_settings()
        if not settings.admission_control.enabled:
            return True, None
        provider = provider_override or settings.get_role_primary("planner").provider

        prov_cfg = settings.providers.get(provider)
        if prov_cfg:
            measured = prov_cfg.measured_r_day or 50
            headroom = prov_cfg.headroom_factor or 0.8
            daily_limit = int(measured * headroom)
        else:
            daily_limit = 40

        # Read today's provider usage
        used_requests, _ = self.ledgers.get_provider_daily_usage(provider)
        remaining_calls = max(0, daily_limit - used_requests)

        # Estimate projected calls for this run
        if options and options.max_subquestions:
            projected_calls = min(options.max_subquestions * 6 + 5, settings.budgets["default"].total_llm_calls)
        else:
            projected_calls = min(
                settings.admission_control.conservative_reserve_per_run, settings.budgets["default"].total_llm_calls
            )

        if projected_calls > remaining_calls:
            # Calculate seconds until next UTC midnight
            now = datetime.now(timezone.utc)
            next_midnight = (now + timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            retry_after_s = max(1, int((next_midnight - now).total_seconds()))

            logger.warning(
                "Admission REJECTED for provider '%s': projected %d calls > %d remaining (limit: %d)",
                provider,
                projected_calls,
                remaining_calls,
                daily_limit,
            )
            return False, AdmissionErrorResponse(
                code="daily_allowance",
                retry_after_s=retry_after_s,
                projected_calls=projected_calls,
                remaining_calls=remaining_calls,
            )

        return True, None
