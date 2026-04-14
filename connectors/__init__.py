"""
Alfred connector framework.

Connectors are the single source of truth for communicating with
external services (WhatsApp, Gmail, etc.).  Each connector owns:
  - all HTTP / protocol calls to its service
  - retry and error normalisation
  - state persistence (via integrations.py)
  - both sync and async variants where needed

Usage
-----
    from connectors.whatsapp import whatsapp

    # sync (alfred_core / background tasks)
    ok, error = whatsapp.send_message(user_id, session_id, "Alice", "hey!")

    # async (FastAPI routes)
    status = await whatsapp.ensure_ready_async(session_id)
"""

from connectors.base import BaseConnector

__all__ = ["BaseConnector"]
