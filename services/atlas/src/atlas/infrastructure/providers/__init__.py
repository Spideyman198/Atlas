"""Model provider adapters.

Vendor SDK adapters (Anthropic, OpenAI, Voyage) land in M3b. This package
currently holds the pieces that need no network: deterministic fakes, the pricing
table, and the resilience decorators that wrap any provider.
"""
