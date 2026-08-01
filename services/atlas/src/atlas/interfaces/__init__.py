"""Delivery mechanisms: HTTP routers and CLI entrypoints.

This layer translates transport concerns into application use cases and back. It
may import ``application``, ``domain`` and ``config``, but never ``infrastructure``
directly — adapters reach it through the composition root.
"""
