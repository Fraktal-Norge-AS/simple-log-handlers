from __future__ import annotations

import asyncio
import functools
from contextvars import ContextVar

# Single shared context var used by both DatadogHandler and OtelHandler.
# Set via @with_executable_key; read in each handler's emit().
_executable_key_var: ContextVar[str | None] = ContextVar(
    "simple_log_handlers_executable_key", default=None
)


def with_executable_key(func_or_name=None):
    """Decorator that sets executablekey on all log records emitted during a function call.

    Usage::

        @with_executable_key                    # uses func.__name__
        def my_function(): ...

        @with_executable_key("custom_name")     # explicit name
        def my_function(): ...
    """
    def decorator(func):
        key = func_or_name if isinstance(func_or_name, str) else func.__name__
        if asyncio.iscoroutinefunction(func):
            @functools.wraps(func)
            async def async_wrapper(*args, **kwargs):
                token = _executable_key_var.set(key)
                try:
                    return await func(*args, **kwargs)
                finally:
                    _executable_key_var.reset(token)
            return async_wrapper
        @functools.wraps(func)
        def sync_wrapper(*args, **kwargs):
            token = _executable_key_var.set(key)
            try:
                return func(*args, **kwargs)
            finally:
                _executable_key_var.reset(token)
        return sync_wrapper

    if callable(func_or_name):
        return decorator(func_or_name)
    return decorator
