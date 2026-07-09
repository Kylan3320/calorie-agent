"""Compatibility shim for fake write tool adapters."""

from .write_tools import FakeWriteLegacy, WRITE_ALLOWED_TOOLS, execute_fake_write_tool

__all__ = ["FakeWriteLegacy", "WRITE_ALLOWED_TOOLS", "execute_fake_write_tool"]
