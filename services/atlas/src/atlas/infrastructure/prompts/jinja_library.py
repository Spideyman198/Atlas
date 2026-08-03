"""Jinja2 behind the prompt port.

Two decisions worth explaining.

**Versions are content hashes, not numbers.** A hand-maintained version gets
forgotten on the one edit that mattered, and then the logs claim two different
answers came from the same prompt. A hash of the template source cannot go
stale: change a word and the version changes with it. It is not readable, but
"which exact wording produced this answer" is the question it has to answer, and
a number that might be wrong answers it worse than a hash that cannot be.

**Retrieved text cannot forge the context fence.** The system prompt tells the
model that everything between the fence markers is quoted data rather than
instructions. That is worth nothing if a retrieved document can simply close the
fence and start issuing orders, so no rendered variable is allowed to contain
the marker. Jinja does not evaluate the *contents* of a variable, so template
syntax inside a document is already inert; this covers the other half.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final

from jinja2 import Environment, FileSystemLoader, StrictUndefined, TemplateNotFound

from atlas.domain.errors import ConfigurationError
from atlas.domain.ports.prompts import RenderedPrompt

#: Where retrieved content starts and stops. Named rather than generic so the
#: marker does not collide with XML or markdown that a document legitimately
#: contains.
FENCE_OPEN: Final = "<atlas:context>"
FENCE_CLOSE: Final = "</atlas:context>"

#: What a forged marker is replaced with. Visible on purpose: a document trying
#: to close the fence is worth seeing in a log, and the model reading it in
#: place of the marker is told plainly that something was removed.
FENCE_REMOVED: Final = "[removed: context marker in retrieved text]"

#: Every template the engine ships. Listed rather than globbed so a missing file
#: fails at startup instead of at the first request that needs it.
TEMPLATES: Final = ("system", "answer", "summarise", "refusal")

_SUFFIX: Final = ".jinja"
_VERSION_CHARS: Final = 12


class JinjaPromptLibrary:
    """Renders the engine's prompt templates from disk.

    Args:
        directory: Where the templates live. Defaults to the package's own.
        names: Templates to verify at construction. Every one must exist.

    Raises:
        ConfigurationError: A declared template is missing.
    """

    def __init__(
        self,
        *,
        directory: Path | None = None,
        names: Sequence[str] = TEMPLATES,
    ) -> None:
        self._directory = directory or Path(__file__).parent / "templates"
        self._environment = Environment(
            loader=FileSystemLoader(self._directory),
            # Prompts are plain text. HTML escaping would turn every apostrophe
            # in a customer's name into `&#39;` on its way to the model.
            autoescape=False,  # noqa: S701 - text prompts, not markup
            undefined=StrictUndefined,
            trim_blocks=True,
            lstrip_blocks=True,
            keep_trailing_newline=False,
        )
        self._versions = {name: self._hash(name) for name in names}

    def render(self, name: str, /, **variables: Any) -> RenderedPrompt:
        """Render template ``name`` with ``variables``.

        Every string reaching the template is stripped of context markers first,
        so retrieved content cannot end the quoted section it sits in.

        Raises:
            ConfigurationError: No such template.
        """
        version = self.version(name)
        template = self._environment.get_template(name + _SUFFIX)
        text = template.render(**{key: _sanitise(value) for key, value in variables.items()})
        return RenderedPrompt(text=text.strip(), name=name, version=version)

    def version(self, name: str) -> str:
        """The version of template ``name``.

        Raises:
            ConfigurationError: No such template.
        """
        known = self._versions.get(name)
        if known is None:
            available = ", ".join(sorted(self._versions))
            message = f"no prompt template named {name!r}. Available: {available}"
            raise ConfigurationError(message)
        return known

    def _hash(self, name: str) -> str:
        try:
            source, _path, _uptodate = self._environment.loader.get_source(  # type: ignore[union-attr]
                self._environment, name + _SUFFIX
            )
        except TemplateNotFound as error:
            message = f"prompt template {name!r} is missing from {self._directory}"
            raise ConfigurationError(message) from error
        digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
        return digest[:_VERSION_CHARS]


def _sanitise(value: Any) -> Any:
    """Strip context markers from anything rendered into a prompt.

    Recurses through the containers a template variable is actually built from.
    Anything else is passed through: an object whose attributes a template
    reaches into is the caller's responsibility, which is why the orchestrator
    hands this library strings and mappings rather than domain objects.
    """
    if isinstance(value, str):
        return value.replace(FENCE_OPEN, FENCE_REMOVED).replace(FENCE_CLOSE, FENCE_REMOVED)
    if isinstance(value, Mapping):
        return {key: _sanitise(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitise(item) for item in value]
    return value
