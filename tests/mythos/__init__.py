"""Mythos integration tests.

These tests run the full happy path with no Discord, no CLIs installed,
no API keys. Stub agents return canned text; the in-memory Discord IO
records every message and exposes it for assertion.
"""
