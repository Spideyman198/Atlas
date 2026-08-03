"""What a tool is.

Separate from the registry so the handlers can import it without the registry
importing them back.
"""

import dataclasses


@dataclasses.dataclass(frozen=True)
class Tool:
    """One callable tool, and what the model is told about it.

    Attributes:
        description: What it does *and when to reach for it*. The trigger
            condition matters as much as the capability: current models call
            tools conservatively, and a description that only states a
            capability measurably under-triggers it.
        parameters: JSON Schema, strict. The engine hands this to the model
            verbatim, so this is the single definition of the tool's shape —
            there is no second copy on the engine's side to drift from it.
        models: Odoo models the tool reads. Used to leave a tool out of the
            catalogue on a database where its module is not installed, so the
            model is never told about a capability that could only ever fail.
    """

    name: str
    description: str
    parameters: dict
    handler: object
    models: tuple = ()

    def run(self, env, arguments):
        """Execute the tool in ``env``, which is already the acting user's."""
        return self.handler(env, arguments)

    def available_in(self, env):
        """Whether this database can serve the tool to the acting user."""
        return all(model in env and env[model].has_access("read") for model in self.models)
