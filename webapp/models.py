"""Compatibility alias for dependency-neutral runtime models."""

import sys

from studio_core import models as _implementation

sys.modules[__name__] = _implementation
