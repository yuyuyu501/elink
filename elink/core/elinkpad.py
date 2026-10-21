"""Compatibility import for the Windows experimental gamepad backend."""
import sys
from ..platforms.windows import elinkpad as implementation

sys.modules[__name__] = implementation
