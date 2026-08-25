"""Compatibility alias for dependency-neutral generation contracts."""

import sys

from studio_core import generation_archive as _implementation

sys.modules[__name__] = _implementation
