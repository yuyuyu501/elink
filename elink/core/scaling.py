"""Separate presentation dimensions from the encoded pixel dimensions."""
RATIOS = {100: (1, 1), 75: (3, 4), 67: (2, 3)}


def encoding_size(config):
    numerator, denominator = RATIOS[config.get('render_scale', 100)]
    return tuple(max(2, (config[key] * numerator // denominator) // 2 * 2) for key in ('width', 'height'))
