"""Compatibility alias for the dependency-neutral project domain."""

import sys

from studio_core import projects as _implementation

sys.modules[__name__] = _implementation
