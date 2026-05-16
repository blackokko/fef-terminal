"""Load QuantData credentials + tool IDs from ~/.quantdata-mcp/config.json.

The QuantData MCP package writes this file during its `setup` flow. We read
it directly so the sidecar doesn't need its own onboarding.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from pydantic import BaseModel, Field

from fef_sidecar.quantdata.errors import QuantDataNotConfiguredError

DEFAULT_CONFIG_DIR = Path(os.environ.get("QUANTDATA_MCP_CONFIG_DIR", Path.home() / ".quantdata-mcp"))
DEFAULT_CONFIG_PATH = DEFAULT_CONFIG_DIR / "config.json"


class QuantDataConfig(BaseModel):
    auth_token: str
    instance_id: str
    page_id: str = ""
    tools: dict[str, str] = Field(default_factory=dict)

    @property
    def base_url(self) -> str:
        return "https://core-lb-prod.quantdata.us/api"


def load_config(path: Path | None = None) -> QuantDataConfig:
    """Load QuantData config from disk.

    Raises:
        QuantDataNotConfiguredError: if config file missing or malformed.
    """
    config_path = path or DEFAULT_CONFIG_PATH
    if not config_path.exists():
        raise QuantDataNotConfiguredError(
            f"QuantData config not found at {config_path}. "
            "Run `quantdata-mcp setup --auth-token <TOKEN> --instance-id <ID>` "
            "to provision credentials."
        )
    try:
        raw = json.loads(config_path.read_text())
        return QuantDataConfig.model_validate(raw)
    except (json.JSONDecodeError, ValueError) as e:
        raise QuantDataNotConfiguredError(
            f"Failed to parse QuantData config at {config_path}: {e}"
        ) from e
