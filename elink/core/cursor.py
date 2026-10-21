"""Compatibility import for the Windows cursor backend."""
import sys
from ..platforms.windows import cursor as implementation

sys.modules[__name__] = implementation
