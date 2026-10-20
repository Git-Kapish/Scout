"""Scout Configuration Loader.

Loads settings.yaml and merges environment variables.
Enforces that models and budgets are never hard-coded.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, List, Literal, Optional

import yaml
from pydantic import BaseModel, Field


class RoleConfig(BaseModel):
    provider: str
    model: str
    max_tokens: int = 2048
    temperature: float = 0.0


class RoleEntry(BaseModel):
    primary: RoleConfig
    fallback: Optional[RoleConfig] = None


class ProviderConfig(BaseModel):
    base_url: str
    env_key: str
    default_rpm: int = 20
    measured_r_day: int = 50
    measured_date: str = "2026-10-19"
    headroom_factor: float = 0.8


class BudgetConfig(BaseModel):
    max_subquestions: int = 5
    queries_per_subquestion: int = 3
    pages_per_subquestion: int = 4
    total_llm_calls: int = 40
    total_tokens: int = 150000
    wall_clock_s: int = 600
    reworks_per_subquestion: int = 1
    reworks_per_run: int = 3
    writer_regenerations: int = 2


class MinimumEvidenceConfig(BaseModel):
    min_accepted_subquestions: int = 2
    min_verified_findings: int = 4
    min_distinct_domains: int = 2


class SearchConfig(BaseModel):
    provider_order: List[str] = Field(default_factory=lambda: ["tavily", "ddgs"])
    max_results_per_query: int = 5
    max_results_per_domain: int = 2
    cache_ttl_s: int = 86400


class FetchConfig(BaseModel):
    timeout_s: int = 10
    max_size_bytes: int = 2097152
    max_redirects: int = 3
    user_agent: str
    bm25_top_paragraphs: int = 5
    bm25_max_tokens_per_page: int = 1500
    rate_limit_per_domain_s: float = 1.0


class ReviewConfig(BaseModel):
    mode: Literal["deterministic_first", "llm"] = "deterministic_first"
    runtime_support_check: bool = False


class RedisConfig(BaseModel):
    lease_ttl_s: int = 30
    heartbeat_interval_s: int = 10
    run_ttl_s: int = 604800
    max_stream_len: int = 2000
    url: str = "redis://localhost:6379/0"


class EffectsConfig(BaseModel):
    read_only_ambiguity_policy: str = "retry_at_least_once"
    mutating_ambiguity_policy: str = "stop_ambiguous"


class AdmissionControlConfig(BaseModel):
    enabled: bool = True
    conservative_reserve_per_run: int = 35


class FaultsConfig(BaseModel):
    enabled: bool = False
    crash_at: Optional[str] = None


class EvaluationConfig(BaseModel):
    min_questions_for_publishable_results: int = 12


class Settings(BaseModel):
    version: str = "1.0"
    scout_mode: Literal["live", "record", "replay"] = "live"
    replay_live_fallback: bool = False
    roles: Dict[str, RoleEntry]
    providers: Dict[str, ProviderConfig]
    budgets: Dict[str, BudgetConfig]
    minimum_evidence: MinimumEvidenceConfig
    search: SearchConfig
    fetch: FetchConfig
    review: ReviewConfig
    redis: RedisConfig
    effects: EffectsConfig
    admission_control: AdmissionControlConfig
    faults: FaultsConfig
    evaluation: EvaluationConfig

    def get_role_primary(self, role: str) -> RoleConfig:
        if role not in self.roles:
            raise KeyError(f"Role '{role}' not configured in settings.yaml")
        return self.roles[role].primary

    def get_role_fallback(self, role: str) -> Optional[RoleConfig]:
        if role not in self.roles:
            return None
        return self.roles[role].fallback


_SETTINGS_INSTANCE: Optional[Settings] = None


def find_project_root() -> Path:
    current = Path(__file__).resolve().parent
    while current.parent != current:
        if (current / "config" / "settings.yaml").exists():
            return current
        if (current / "pyproject.toml").exists():
            return current
        current = current.parent
    return Path.cwd()


def load_settings(config_path: Optional[Path] = None) -> Settings:
    global _SETTINGS_INSTANCE
    root = find_project_root()
    env_file = root / ".env"
    if env_file.exists():
        from dotenv import load_dotenv
        load_dotenv(env_file)

    if config_path is None:
        config_path = root / "config" / "settings.yaml"

    if not config_path.exists():
        raise FileNotFoundError(f"Settings file not found: {config_path}")

    with open(config_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    # Merge environment variable overrides
    scout_mode = os.getenv("SCOUT_MODE")
    if scout_mode in ("live", "record", "replay"):
        data["scout_mode"] = scout_mode

    redis_url = os.getenv("REDIS_URL")
    if redis_url:
        if "redis" not in data:
            data["redis"] = {}
        data["redis"]["url"] = redis_url

    faults_enabled = os.getenv("SCOUT_FAULTS_ENABLED") == "1"
    crash_at = os.getenv("SCOUT_CRASH_AT")
    if "faults" not in data:
        data["faults"] = {}
    data["faults"]["enabled"] = faults_enabled
    data["faults"]["crash_at"] = crash_at

    settings = Settings.model_validate(data)
    _SETTINGS_INSTANCE = settings
    return settings


def get_settings() -> Settings:
    global _SETTINGS_INSTANCE
    if _SETTINGS_INSTANCE is None:
        _SETTINGS_INSTANCE = load_settings()
    return _SETTINGS_INSTANCE
