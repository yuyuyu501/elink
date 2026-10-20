"""Compare identical H.264 packets through software and real D3D11VA decode.

Generated SDR colors only; no desktop capture or saved personal pixels.
"""
import argparse
import fractions
import json
from pathlib import Path
from types import SimpleNamespace

import av
import numpy as np

from elink.core.codecs import CodecPolicy, DesktopDecoder, make_encoder
from elink.core.media import bgr_pixels


COLORS = [(v, v, v) for v in (0, 8, 16, 32, 128, 220, 235, 245, 250, 255)] + [
    (180, 60, 30), (30, 90, 190), (40, 180, 70), (236, 240, 248), (32, 38, 45), (170, 50, 180)]
POINTS = tuple(((index % 4 + .5) / 4, (index // 4 + .5) / 4) for index in range(16))
CASES = {(w, h, space, color_range, fmt) for w, h in ((1920, 1080), (2560, 1440))
         for space in (1, 5) for color_range in (1, 2) for fmt in ('yuv420p', 'nv12')}


def samples(pixels):
    height, width = pixels.shape[:2]
    return np.array([pixels[int(ny * height), int(nx * width)] for nx, ny in POINTS], dtype=int)


def verify(present=None):
    results = []
    for width, height, space, color_range, fmt in sorted(CASES):
        rgb = np.zeros((height, width, 3), np.uint8)
        for index, color in enumerate(COLORS):
            x, y = index % 4, index // 4
            rgb[y * height // 4:(y + 1) * height // 4, x * width // 4:(x + 1) * width // 4] = color
        source = av.VideoFrame.from_ndarray(rgb, format='rgb24').reformat(
            format=fmt, dst_colorspace='ITU709' if space == 1 else 'ITU601', dst_color_range=color_range)
        source.colorspace, source.color_range = space, color_range
        encoder = make_encoder('h264_nvenc', width, height, CodecPolicy(fps=90), fmt, space, color_range)
        decoders = [DesktopDecoder(CodecPolicy(decoder=mode)) for mode in ('software', 'hardware')]
        decoded = [[], []]
        for index in range(8):
            source.pts, source.time_base = index * 1000, fractions.Fraction(1, 90000)
            data = b''.join(bytes(packet) for packet in encoder.encode(source))
            if data:
                for output, decoder in zip(decoded, decoders):
                    output.extend(decoder.decode(SimpleNamespace(data=data, timestamp=index * 1000)))
            if all(decoded):
                break
        if not all(decoded) or not decoders[1].hardware:
            raise AssertionError('Color validation did not decode both software H.264 and D3D11VA')
        frames = [output[-1] for output in decoded]
        if any((int(frame.colorspace), int(frame.color_range)) != (space, color_range) for frame in frames):
            raise AssertionError(f'Decoded color metadata lost: {space}/{color_range}/{fmt}: '
                                 + str([(int(f.colorspace), int(f.color_range)) for f in frames]))
        pixels = [bgr_pixels(frame)[:, :, ::-1] for frame in frames]
        errors = [int(np.max(np.abs(samples(p) - COLORS))) for p in pixels]
        difference = int(np.max(np.abs(samples(pixels[0]) - samples(pixels[1]))))
        if max(errors) > 6 or difference > 3:
            raise AssertionError(f'Software/hardware color mismatch: {errors}, difference={difference}')
        result = dict(width=width, height=height, colorspace=space, color_range=color_range,
                      input_format=fmt, hardware_format=frames[1].format.name,
                      software_colorspace=int(frames[0].colorspace), hardware_colorspace=int(frames[1].colorspace),
                      software_color_range=int(frames[0].color_range), hardware_color_range=int(frames[1].color_range),
                      software_rgb_error=errors[0], hardware_rgb_error=errors[1],
                      decoder_rgb_difference=difference,
                      software_samples=samples(pixels[0]).tolist(), hardware_samples=samples(pixels[1]).tolist())
        if present is not None:
            result.update(present(frames[1], pixels[0]))
        results.append(result)
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('.artifacts/decoder-colors.json'))
    args = parser.parse_args()
    result = verify()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
