"""Prompt rendering.

Jinja2 lives here and nowhere else. The application layer asks the
:class:`~atlas.domain.ports.prompts.PromptLibrary` port for rendered text and
never learns what rendered it.
"""

from atlas.infrastructure.prompts.jinja_library import (
    FENCE_CLOSE,
    FENCE_OPEN,
    TEMPLATES,
    JinjaPromptLibrary,
)

__all__ = [
    "FENCE_CLOSE",
    "FENCE_OPEN",
    "TEMPLATES",
    "JinjaPromptLibrary",
]
