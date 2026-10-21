"""Compatibility import for the Windows D3D11 capture backend."""
import sys
from ..platforms.windows import capture as implementation

sys.modules[__name__] = implementation
