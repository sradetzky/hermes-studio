"""Compatibility alias for the dependency-neutral runtime schema."""

import sys

from studio_core import runtime_schema as _implementation

sys.modules[__name__] = _implementation
