"""Scout LLM Client and Provider Chain.

Provides OpenAI-compatible completions with:
- Role-based configuration from settings.yaml
- Fallback chain on HTTP 429/quota errors
- Conservative token reservations before dispatch
- Structured Pydantic model parsing and bounded retries
- Token usage reporting (actual vs estimated/unknown)
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional, Tuple, Type, TypeVar

import httpx
from openai import APIStatusError, OpenAI, RateLimitError
from pydantic import BaseModel, ValidationError

from scout.config import RoleConfig, Settings, get_settings

T = TypeVar("T", bound=BaseModel)


class LLMUsage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    is_estimated: bool = False


class LLMResponse(BaseModel):
    content: str
    parsed: Optional[Any] = None
    provider: str
    model: str
    usage: LLMUsage
    latency_s: float
    retries: int = 0


class TokenBudgetExceededError(Exception):
    """Raised when token reservation exceeds available budget."""
    pass


class ProviderQuotaExhaustedError(Exception):
    """Raised when all providers in the fallback chain hit rate limits or quota walls."""
    pass


class LLMClient:
    def __init__(self, settings: Optional[Settings] = None):
        self.settings = settings or get_settings()

    def _get_api_key(self, env_key: str) -> Optional[str]:
        return os.getenv(env_key)

    def _create_client(self, provider_name: str) -> Tuple[OpenAI, str]:
        if provider_name not in self.settings.providers:
            raise ValueError(f"Unknown provider: {provider_name}")
        p_cfg = self.settings.providers[provider_name]
        api_key = self._get_api_key(p_cfg.env_key)
        if not api_key:
            # Check for dummy / mock mode if not live
            if self.settings.scout_mode in ("record", "replay"):
                api_key = "test-key"
            else:
                raise ValueError(
                    f"Missing API key for provider '{provider_name}'. "
                    f"Please set environment variable '{p_cfg.env_key}' in .env."
                )

        client = OpenAI(
            base_url=p_cfg.base_url,
            api_key=api_key,
            timeout=httpx.Timeout(60.0, connect=10.0),
        )
        return client, p_cfg.base_url

    def estimate_prompt_tokens(self, messages: List[Dict[str, str]]) -> int:
        """Conservative estimation: ~1 token per 3.5 characters + formatting overhead."""
        char_count = sum(len(m.get("content", "")) for m in messages)
        return int(char_count / 3.5) + len(messages) * 4 + 10

    def generate(
        self,
        role: str,
        messages: List[Dict[str, str]],
        response_model: Optional[Type[T]] = None,
        remaining_token_budget: Optional[int] = None,
        max_retries: int = 2,
    ) -> LLMResponse:
        """Dispatches an LLM completion for the given role with token reservation and retries."""
        role_entry = self.settings.roles.get(role)
        if not role_entry:
            raise KeyError(f"Role '{role}' not configured in settings.yaml")

        provider_chain = [role_entry.primary]
        if role_entry.fallback:
            provider_chain.append(role_entry.fallback)

        prompt_est = self.estimate_prompt_tokens(messages)

        last_error = None
        for role_cfg in provider_chain:
            # Conservative token reservation check before dispatch (PRD FR-2.2)
            reservation = prompt_est + role_cfg.max_tokens
            if remaining_token_budget is not None and reservation > remaining_token_budget:
                raise TokenBudgetExceededError(
                    f"Token reservation ({reservation}) exceeds remaining budget ({remaining_token_budget})"
                )

            try:
                return self._dispatch_with_retries(
                    role_cfg=role_cfg,
                    messages=messages,
                    response_model=response_model,
                    max_retries=max_retries,
                    prompt_est=prompt_est,
                )
            except (RateLimitError, APIStatusError) as e:
                # 429 or quota errors trigger provider chain fallback (PRD FR-3.4)
                status_code = getattr(e, "status_code", None)
                if status_code in (429, 402, 503):
                    last_error = e
                    continue
                raise
            except Exception as e:
                last_error = e
                continue

        raise ProviderQuotaExhaustedError(
            f"All providers in chain for role '{role}' failed. Last error: {last_error}"
        )

    def _dispatch_with_retries(
        self,
        role_cfg: RoleConfig,
        messages: List[Dict[str, str]],
        response_model: Optional[Type[T]],
        max_retries: int,
        prompt_est: int,
    ) -> LLMResponse:
        client, _ = self._create_client(role_cfg.provider)
        current_messages = list(messages)
        attempt = 0

        while attempt <= max_retries:
            start_time = time.time()
            completion_kwargs: Dict[str, Any] = {
                "model": role_cfg.model,
                "messages": current_messages,
                "temperature": role_cfg.temperature,
                "max_tokens": role_cfg.max_tokens,
            }

            # Optional extra headers for OpenRouter identification
            extra_headers = {
                "HTTP-Referer": "https://github.com/Git-Kapish/Scout",
                "X-Title": "Scout Multi-Agent Research Assistant",
            }
            completion_kwargs["extra_headers"] = extra_headers

            resp = client.chat.completions.create(**completion_kwargs)
            latency = time.time() - start_time

            choice = resp.choices[0]
            content = choice.message.content or ""

            # Extract usage
            if resp.usage:
                usage = LLMUsage(
                    prompt_tokens=resp.usage.prompt_tokens or prompt_est,
                    completion_tokens=resp.usage.completion_tokens or 0,
                    total_tokens=resp.usage.total_tokens or (prompt_est + (resp.usage.completion_tokens or 0)),
                    is_estimated=False,
                )
            else:
                # Conservative fallback if provider reports late or not at all (PRD FR-2.2)
                comp_est = int(len(content) / 3.5)
                usage = LLMUsage(
                    prompt_tokens=prompt_est,
                    completion_tokens=comp_est,
                    total_tokens=prompt_est + comp_est,
                    is_estimated=True,
                )

            if response_model is None:
                return LLMResponse(
                    content=content,
                    parsed=None,
                    provider=role_cfg.provider,
                    model=role_cfg.model,
                    usage=usage,
                    latency_s=latency,
                    retries=attempt,
                )

            # Validate structured model
            try:
                parsed_instance = self._parse_structured_output(content, response_model)
                return LLMResponse(
                    content=content,
                    parsed=parsed_instance,
                    provider=role_cfg.provider,
                    model=role_cfg.model,
                    usage=usage,
                    latency_s=latency,
                    retries=attempt,
                )
            except (ValidationError, json.JSONDecodeError) as ve:
                attempt += 1
                if attempt > max_retries:
                    raise ve
                # Add validation error message for bounded retry
                current_messages.append({"role": "assistant", "content": content})
                current_messages.append({
                    "role": "user",
                    "content": f"Output validation failed with error: {ve}. Please fix and return ONLY valid JSON matching the schema.",
                })

        raise RuntimeError("Unexpected loop termination in _dispatch_with_retries")

    def _parse_structured_output(self, content: str, model_cls: Type[T]) -> T:
        """Extracts and parses JSON matching the Pydantic schema."""
        raw_text = content.strip()
        # Strip markdown fences if present
        if raw_text.startswith("```"):
            lines = raw_text.splitlines()
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            raw_text = "\n".join(lines).strip()

        # Find first '{' or '[' if wrapped
        start_brace = raw_text.find("{")
        if start_brace != -1:
            end_brace = raw_text.rfind("}")
            if end_brace != -1:
                raw_text = raw_text[start_brace : end_brace + 1]

        data = json.loads(raw_text)
        return model_cls.model_validate(data)
