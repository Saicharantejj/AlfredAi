"""
Base connector class for all Alfred integrations.

Every connector gets a name (used as the key inside integrations.json)
and inherits helpers for loading / saving its slice of state via the
existing integrations storage layer.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class BaseConnector(ABC):
    """Abstract base for all Alfred connectors."""

    #: Unique slug that identifies this connector inside integrations.json.
    #: Subclasses must set this as a class attribute.
    name: str

    # ------------------------------------------------------------------
    # State helpers — read/write only the connector's own key so that
    # connectors never accidentally overwrite each other's state.
    # ------------------------------------------------------------------

    def load_state(self, user_id: str) -> dict:
        """Return this connector's state dict for *user_id*."""
        # Lazy import to avoid circular imports at module load time.
        from integrations import load_integrations  # noqa: PLC0415
        state = load_integrations(user_id)
        return dict(state.get(self.name) or {})

    def save_state(self, user_id: str, connector_state: dict) -> None:
        """Persist *connector_state* for *user_id*, leaving other connectors untouched."""
        from integrations import load_integrations, save_integrations  # noqa: PLC0415
        state = load_integrations(user_id)
        state[self.name] = connector_state
        save_integrations(user_id, state)

    def patch_state(self, user_id: str, **kwargs: Any) -> None:
        """Shallow-merge *kwargs* into this connector's persisted state."""
        current = self.load_state(user_id)
        current.update({k: v for k, v in kwargs.items() if v is not None})
        self.save_state(user_id, current)

    # ------------------------------------------------------------------
    # Interface that subclasses should implement
    # ------------------------------------------------------------------

    @abstractmethod
    def get_status(self, user_id: str) -> dict:
        """Return a dict describing the current connection status for *user_id*."""
        ...
