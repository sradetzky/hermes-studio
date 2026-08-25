"""Compatibility alias for the dependency-neutral job store."""

import sys

from studio_core import job_store as _implementation

sys.modules[__name__] = _implementation
