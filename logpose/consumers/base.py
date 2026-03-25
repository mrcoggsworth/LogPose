from __future__ import annotations

import abc
from collections.abc import Callable

from logpose.models.alert import Alert


class BaseConsumer(abc.ABC):
    """Abstract base class for all ingestion source consumers.

    Subclasses implement connect/consume/disconnect for each source
    (Kafka, SNS, Pub/Sub) while sharing the same Alert normalization interface.
    """

    @abc.abstractmethod
    def connect(self) -> None:
        """Establish connection to the upstream event source."""

    @abc.abstractmethod
    def consume(self, callback: Callable[[Alert], None]) -> None:
        """Start blocking poll loop; invoke callback for each received Alert."""

    @abc.abstractmethod
    def disconnect(self) -> None:
        """Cleanly close the connection to the upstream event source."""

    def __enter__(self) -> "BaseConsumer":
        self.connect()
        return self

    def __exit__(self, *_: object) -> None:
        self.disconnect()
