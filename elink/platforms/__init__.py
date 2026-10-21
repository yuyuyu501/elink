"""Platform boundary for capture, input and desktop integration."""
import sys


def backend():
    if sys.platform == 'win32':
        from .windows import backend as implementation
    elif sys.platform.startswith('linux'):
        from .linux import backend as implementation
    else:
        raise RuntimeError(f'Unsupported desktop platform: {sys.platform}')
    return implementation


def capabilities():
    return backend().capabilities()
