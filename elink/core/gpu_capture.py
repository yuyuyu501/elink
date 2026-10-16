"""D3D11 capture and scaling with measured YUV color metadata."""
import av
import numpy as np


def color_blocks(pixels):
    size = max(16, min(pixels.shape[:2]) // 48)
    height, width = pixels.shape[0] // size, pixels.shape[1] // size
    return pixels[:height*size, :width*size].reshape(height, size, width, size, 3).mean(axis=(1, 3))


def calibrated_color_space(frame, reference):
    reference = reference.reformat(width=frame.width, height=frame.height, format='rgb24').to_ndarray()
    # Compare block averages: GPU/CPU resize kernels and 4:2:0 differ at text
    # edges. Sparse individual pixels mistake those differences for a matrix.
    expected = color_blocks(reference)
    colored = np.ptp(expected, axis=2) > 8
    if np.count_nonzero(colored) < 16:
        raise RuntimeError('D3D11 color calibration needs more colored samples')
    candidates = []
    for space, label in ((5, 'ITU601'), (1, 'ITU709')):
        for color_range in (1, 2):
            pixels = frame.to_ndarray(format='rgb24', src_colorspace=label,
                                     dst_colorspace='ITU601', src_color_range=color_range)
            error = float(np.mean(np.abs(color_blocks(pixels) - expected)[colored]))
            candidates.append((error, space, color_range))
    candidates.sort()
    if candidates[0][0] > 6 or candidates[1][0] - candidates[0][0] < .25:
        raise RuntimeError(f'D3D11 color conversion could not be calibrated '
                           f'(error={candidates[0][0]:.2f}, margin={candidates[1][0]-candidates[0][0]:.2f})')
    return candidates[0][1:]


class D3D11Capture:
    def __init__(self, width, height, draw_mouse):
        self.width, self.height, self.draw_mouse = width, height, draw_mouse
        self._build(reference=True)
        try:
            frame, reference = self.sink.pull(), self.reference_sink.pull()
            if frame.pts != reference.pts:
                raise RuntimeError('D3D11 calibration frames are not synchronized')
            self.colorspace, self.color_range = calibrated_color_space(frame, reference)
        finally:
            self.close()
        self._build()

    def _build(self, reference=False):
        self.graph = av.filter.Graph()
        # Elink's worker FrameClock owns the cadence. A high source ceiling
        # avoids ddagrab's av_usleep/Sleep timer throttling 90 FPS to ~64 FPS.
        source = self.graph.add('ddagrab', f'framerate=1000:dup_frames=1:draw_mouse={int(self.draw_mouse)}')
        if reference:
            splitter = self.graph.add('split', 'outputs=2')
            source.link_to(splitter)
            source = splitter
            download = self.graph.add('hwdownload')
            splitter.link_to(download, 1)
            fmt = self.graph.add('format', 'pix_fmts=bgra')
            download.link_to(fmt)
            self.reference_sink = self.graph.add('buffersink')
            fmt.link_to(self.reference_sink)
        # FFmpeg 8 only supports NV12/P010 here and copies RGB metadata. Use
        # the measured matrix/range; fail back to DXCam if it is ambiguous.
        stages = [source,
                  self.graph.add('scale_d3d11', f'width={self.width}:height={self.height}:format=nv12'),
                  self.graph.add('hwdownload'), self.graph.add('format', 'pix_fmts=nv12'),
                  self.graph.add('buffersink')]
        for left, right in zip(stages, stages[1:]):
            left.link_to(right)
        self.graph.configure()
        self.sink = stages[-1]

    def read(self):
        frame = self.sink.pull()
        if frame.format.name != 'nv12' or (frame.width, frame.height) != (self.width, self.height):
            raise RuntimeError('Unexpected D3D11 capture output')
        frame.colorspace, frame.color_range = self.colorspace, self.color_range
        return frame

    def close(self):
        self.graph = None
        self.sink = self.reference_sink = None
