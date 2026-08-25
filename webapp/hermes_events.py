"""Compatibility alias for the dependency-neutral Hermes event bridge."""

import sys

from studio_core import hermes_events as _implementation

sys.modules[__name__] = _implementation
