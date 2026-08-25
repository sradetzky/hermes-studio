"""Compatibility alias for dependency-neutral identifiers."""

import sys

from studio_core import identifiers as _implementation

sys.modules[__name__] = _implementation
