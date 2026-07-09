"""Compatibility shim for read-only tool adapters."""

from .read_tools import READONLY_ALLOWED_TOOLS, execute_readonly_tool

__all__ = ["READONLY_ALLOWED_TOOLS", "execute_readonly_tool"]
