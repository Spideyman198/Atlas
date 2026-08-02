"""Ports: the protocols the application layer depends on.

Every port is a :class:`typing.Protocol` expressed in domain types. Adapters live
in ``atlas.infrastructure`` and are bound to ports by the composition root, so no
use case ever names a concrete implementation.

``ChatProvider`` and ``EmbeddingProvider`` are separate protocols rather than one
``AIProvider`` because Anthropic ships no embedding API. A combined interface
would force half its implementations to raise ``NotImplementedError`` — the
classic sign of an interface that has not been segregated.
"""

from atlas.domain.ports.chat import ChatProvider
from atlas.domain.ports.embedding import EmbeddingProvider

__all__ = ["ChatProvider", "EmbeddingProvider"]
