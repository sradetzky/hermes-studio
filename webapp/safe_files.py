"""Compatibility alias for the dependency-neutral safe-file implementation."""

import sys

from studio_core import safe_files as _implementation

sys.modules[__name__] = _implementation
