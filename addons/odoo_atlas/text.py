"""Text helpers shared by the Atlas models.

Deliberately free of any Odoo import, so it can be reasoned about (and read) on
its own.
"""


def summarise(text, max_length):
    """Collapse a block of text into a single line of at most ``max_length`` characters.

    Runs of whitespace — including the newlines an LLM answer is full of — become
    single spaces. Truncated results end in an ellipsis, which counts towards the
    limit.
    """
    collapsed = " ".join((text or "").split())
    if len(collapsed) <= max_length:
        return collapsed
    return collapsed[: max_length - 1].rstrip() + "\N{HORIZONTAL ELLIPSIS}"
