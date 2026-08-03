"""Questions to show somebody who has not asked one yet.

An empty chat panel is a hard thing to start using. The panel cannot say "ask me
anything" and mean it either: what Atlas can answer depends on which modules are
installed and what the person asking is allowed to read.

So the suggestions are derived rather than written down. Each one names the model
it needs, and a suggestion whose model is missing from this database — or that
this user cannot read — is not shown. Nobody is offered a question that would
come back as "you do not have access to that".

Ordered by how much of the database supports them: a suggestion resting on a
model with no records in it is a demonstration that ends in "there are none".
"""

import logging

logger = logging.getLogger(__name__)

#: How many to show. Enough to suggest the range of what is possible, few enough
#: to read without scrolling.
MAX_SUGGESTIONS = 6

#: ``(model, question)``. The model is what the question needs in order to have
#: an answer, not necessarily the only one the tools will read.
CANDIDATES = (
    ("sale.order", "Which sales orders are still waiting to be confirmed?"),
    ("account.move", "Which invoices are overdue, and who owes the most?"),
    ("stock.quant", "Which products are running low on stock?"),
    ("sale.order.line", "Which products generated the most revenue?"),
    ("crm.lead", "What opportunities are open, and what are they worth?"),
    ("purchase.order", "What have we ordered from suppliers recently?"),
    ("res.partner", "Which customers were added most recently?"),
    ("product.template", "What products do we sell?"),
)

#: Shown when nothing else survives the filter. A database with only `base`
#: installed can still answer this, and an empty panel with no starting point at
#: all is worse than one obvious suggestion.
FALLBACK = "What can you tell me about our contacts?"


def for_user(env):
    """Suggestions this database can answer for this user.

    Args:
        env: The acting user's environment. Access is checked as them, so two
            users on the same database can be offered different questions.

    Returns:
        A list of question strings, at most :data:`MAX_SUGGESTIONS` long.
    """
    scored = []
    for model_name, question in CANDIDATES:
        count = _count(env, model_name)
        if count is None:
            continue
        scored.append((count, question))

    # Populated models first, and the declaration order kept within a tie so the
    # list does not reshuffle itself between page loads.
    scored.sort(key=lambda entry: entry[0] > 0, reverse=True)
    questions = [question for _count_, question in scored[:MAX_SUGGESTIONS]]
    return questions or [FALLBACK]


def _count(env, model_name):
    """How many records of ``model_name`` this user can see, or ``None``.

    ``None`` means the question cannot be offered at all: the module is not
    installed, or the user has no access to the model. Both are the same answer
    from the panel's point of view.
    """
    if model_name not in env:
        return None
    model = env[model_name]
    if not model.has_access("read"):
        return None
    try:
        # Bounded: the number is only used to sort populated ahead of empty, and
        # counting a million rows to decide that would be an odd way to render a
        # placeholder.
        return model.search_count([], limit=1)
    except Exception:
        # A model that refuses to be counted is a model whose question would
        # have failed anyway.
        logger.debug("could not count %s for suggestions", model_name, exc_info=True)
        return None
