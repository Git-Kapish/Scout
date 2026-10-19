"""Effect Recorder and Replay Engine (PRD §5.3 #1, FR-6.2a).

Manages caching and deterministic replay of external effects (LLM, search, fetch).
Enforces fail-closed semantics in replay mode to guarantee benchmark reproducibility.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Callable, Dict, Optional, TypeVar

from pydantic import BaseModel

from scout.config import get_settings

logger = logging.getLogger(__name__)
T = TypeVar("T")


class ReplayMissError(RuntimeError):
    """Raised when replay mode cannot find a cached effect result (PRD FR-6.2a)."""
    pass


def compute_hash(data: Any) -> str:
    """Computes a deterministic SHA256 hash of arbitrary data."""
    if isinstance(data, BaseModel):
        serialized = data.model_dump_json()
    elif isinstance(data, (dict, list)):
        serialized = json.dumps(data, sort_keys=True, default=str)
    else:
        serialized = str(data)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def compute_effect_key(
    effect_type: str,
    node: str,
    inputs: Any,
    provider: str = "",
    model: str = "",
    prompt_version: str = "v1",
    schema_version: str = "v1",
) -> str:
    """Computes a stable effect_key per PRD FR-4.2."""
    input_hash = compute_hash(inputs)
    components = [
        effect_type,
        node,
        input_hash,
        provider,
        model,
        prompt_version,
        schema_version,
    ]
    raw = ":".join(components)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class EffectRecorder:
    def __init__(self, snapshot_dir: Optional[Path] = None):
        if snapshot_dir is None:
            from scout.config import find_project_root
            snapshot_dir = find_project_root() / "data" / "snapshots"
        self.snapshot_dir = snapshot_dir
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        self.settings = get_settings()

    def _get_path(self, effect_key: str) -> Path:
        # 2-level directory sharding by prefix
        prefix = effect_key[:2]
        shard_dir = self.snapshot_dir / prefix
        shard_dir.mkdir(parents=True, exist_ok=True)
        return shard_dir / f"{effect_key}.json"

    def execute_or_replay(
        self,
        effect_key: str,
        dispatch_fn: Callable[[], T],
        serialize_fn: Optional[Callable[[T], Dict[str, Any]]] = None,
        deserialize_fn: Optional[Callable[[Dict[str, Any]], T]] = None,
    ) -> T:
        """Handles live, record, or replay execution under configured SCOUT_MODE."""
        mode = self.settings.scout_mode
        file_path = self._get_path(effect_key)

        if mode == "replay":
            if file_path.exists():
                with open(file_path, "r", encoding="utf-8") as f:
                    cached_data = json.load(f)
                if deserialize_fn:
                    return deserialize_fn(cached_data)
                return cached_data  # type: ignore

            # Cache miss in replay mode
            if not self.settings.replay_live_fallback:
                raise ReplayMissError(
                    f"Replay cache miss for effect_key='{effect_key}'. "
                    f"Fail-closed: live fallback is prohibited in replay mode (PRD §5.3, FR-6.2a)."
                )
            logger.warning("Replay miss for key %s, falling back to live call.", effect_key)

        # Live or Record mode (or replay miss with fallback)
        result = dispatch_fn()

        if mode == "record":
            data_to_store = serialize_fn(result) if serialize_fn else (
                result.model_dump() if isinstance(result, BaseModel) else result
            )
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(data_to_store, f, indent=2, default=str)

        return result
