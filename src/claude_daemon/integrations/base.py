"""Base types for messaging integrations."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable


@dataclass
class NormalizedMessage:
    """Platform-agnostic message representation."""

    platform: str
    user_id: str
    user_name: str
    content: str
    message_id: str
    channel_id: str | None = None
    reply_to: str | None = None
    timestamp: datetime = field(default_factory=datetime.now)
    metadata: dict[str, Any] = field(default_factory=dict)


# Type alias for the message handler callback
MessageHandler = Callable[[NormalizedMessage], Awaitable[str]]


class BaseIntegration(ABC):
    """Abstract base class for all messaging integrations."""

    def __init__(self) -> None:
        self._handler: MessageHandler | None = None

    def set_message_handler(self, handler: MessageHandler) -> None:
        """Set the callback that processes incoming messages."""
        self._handler = handler

    @abstractmethod
    async def start(self) -> None:
        """Start the integration (connect, begin polling/listening)."""
        ...

    @abstractmethod
    async def stop(self) -> None:
        """Stop the integration gracefully."""
        ...

    @abstractmethod
    async def send_response(self, channel_id: str, content: str, **kwargs: Any) -> None:
        """Send a response message to the specified channel/chat."""
        ...
