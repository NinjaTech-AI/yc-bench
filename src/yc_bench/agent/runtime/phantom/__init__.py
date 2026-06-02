"""Phantom adapter — drives NinjaTech Phantom (Claude Code) as a YC-Bench runtime.

Activated when settings.model starts with "phantom/" (see runtime/factory.py).
The suffix after the slash is passed to claude-code as --model.
"""

from .runtime import PhantomRuntime

__all__ = ["PhantomRuntime"]
