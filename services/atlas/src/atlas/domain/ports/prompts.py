"""The prompt library port.

Prompts are versioned and rendered outside the application layer for one
reason: when an answer is wrong, "which prompt produced it" has to be answerable
from the logs. A template edited in place makes every past answer
unreproducible.

The port is deliberately narrow — render a named template with a mapping. It
does not expose Jinja's environment, filters or inheritance, so the application
never grows a dependency on the templating engine (ADR-0003's rule applied to a
second library).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class RenderedPrompt:
    """A prompt, and the identity of the template that produced it.

    Attributes:
        text: What goes to the model.
        name: Template name, such as ``answer``.
        version: The template's version. Recorded on the answer so a bad one can
            be traced to the wording that caused it.
    """

    text: str
    name: str
    version: str

    @property
    def identity(self) -> str:
        """``name@version``, for logs and for the answer record."""
        return f"{self.name}@{self.version}"


@runtime_checkable
class PromptLibrary(Protocol):
    """Renders named, versioned prompt templates.

    Implementations must:

    - Verify every template they declare when they are constructed, so a
      missing one is a startup failure rather than a request that quietly goes
      out with an empty system prompt — an assistant with no instructions and
      no error.
    - Raise :class:`~atlas.domain.errors.ConfigurationError` for an unknown
      name rather than returning an empty string.
    - Treat every value in ``variables`` as data. A retrieved document that
      happens to contain template syntax must not be evaluated.
    """

    # Template variables are by nature untyped: each template declares its own.
    def render(self, name: str, /, **variables: Any) -> RenderedPrompt:  # noqa: ANN401
        """Render template ``name``.

        Raises:
            ConfigurationError: No such template.
        """
        ...

    def version(self, name: str) -> str:
        """The version of template ``name``, without rendering it.

        Raises:
            ConfigurationError: No such template.
        """
        ...
