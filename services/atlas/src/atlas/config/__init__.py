"""Typed configuration and the composition root.

This is the only package permitted to know which concrete adapter satisfies which
domain port. Everything else receives its collaborators by injection, which is
what makes the test suite runnable with no network and no API key.

The composition root itself arrives in M2; M1 ships only :mod:`atlas.config.settings`.
"""
