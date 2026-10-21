"""Compatibility import for integrations using the pre-0.5 Windows input API."""
import sys
from ..platforms.windows import input as implementation

sys.modules[__name__] = implementation
