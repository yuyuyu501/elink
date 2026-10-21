"""Windows D3D11 capture and scaling with measured YUV color metadata."""
import av
import logging
import numpy as np
import time


log = logging.getLogger(__name__)


class ColorCalibrationPending(RuntimeError):
    """The desktop cannot yet distinguish the driver's matrix/range."""


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
        raise ColorCalibrationPending(f'D3D11 color calibration needs more colored samples '
                           f'(colored_blocks={int(np.count_nonzero(colored))}, required=16)')
    candidates = []
    for space, label in ((5, 'ITU601'), (1, 'ITU709')):
        for color_range in (1, 2):
            pixels = frame.to_ndarray(format='rgb24', src_colorspace=label,
                                     dst_colorspace='ITU601', src_color_range=color_range)
            error = float(np.mean(np.abs(color_blocks(pixels) - expected)[colored]))
            candidates.append((error, space, color_range))
    candidates.sort()
    if candidates[0][0] > 6:
        raise RuntimeError(f'D3D11 color conversion could not be calibrated '
                           f'(error={candidates[0][0]:.2f}, margin={candidates[1][0]-candidates[0][0]:.2f})')
    if candidates[1][0] - candidates[0][0] < .25:
        raise ColorCalibrationPending(f'D3D11 color calibration is ambiguous '
                                      f'(margin={candidates[1][0]-candidates[0][0]:.2f})')
    return candidates[0][1:]


class D3D11Capture:
    def __init__(self, width, height, draw_mouse):
        self.width, self.height, self.draw_mouse = width, height, draw_mouse
        self.calibration_pending = True
        self.next_calibration_at = 0
        self.pending_frame = None
        self.pending_notice = False
        try:
            self.stage = 'calibration_graph'
            self._build(reference=True)
            self.pending_frame = self._calibrate_frame()
        except Exception as exc:
            exc.add_note(f'D3D11 stage={self.stage} output={width}x{height} draw_mouse={draw_mouse}')
            self.close()
            raise
        except BaseException:
            self.close()
            raise

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
        # the measured matrix/range once enough desktop colors are available.
        stages = [source,
                  self.graph.add('scale_d3d11', f'width={self.width}:height={self.height}:format=nv12'),
                  self.graph.add('hwdownload'), self.graph.add('format', 'pix_fmts=nv12'),
                  self.graph.add('buffersink')]
        for left, right in zip(stages, stages[1:]):
            left.link_to(right)
        self.graph.configure()
        self.sink = stages[-1]

    def _calibrate_frame(self):
        self.stage = 'calibration_frames'
        frame, reference = self.sink.pull(), self.reference_sink.pull()
        if frame.pts != reference.pts:
            raise RuntimeError(f'D3D11 calibration frames are not synchronized '
                               f'(video_pts={frame.pts}, reference_pts={reference.pts})')
        if time.monotonic() >= self.next_calibration_at:
            self.stage = 'calibration_colors'
            try:
                self.colorspace, self.color_range = calibrated_color_space(frame, reference)
            except ColorCalibrationPending as exc:
                self.next_calibration_at = time.monotonic() + .25
                if not self.pending_notice:
                    log.info('%s; keeping D3D11 capture with temporary CPU color conversion', exc)
                    self.pending_notice = True
            else:
                self.close()
                self.stage = 'capture_graph'
                self._build()
                self.calibration_pending = False
                log.info('D3D11 color calibration ready: output=%sx%s matrix=%s range=%s',
                         self.width, self.height, self.colorspace, self.color_range)
                frame.colorspace, frame.color_range = self.colorspace, self.color_range
                return frame
        # Never guess metadata for a later colorful frame based on a gray one.
        # Keep trying in this same session instead of permanently disabling GPU.
        self.stage = 'calibration_cpu_color'
        frame = reference.reformat(width=self.width, height=self.height, format='nv12',
                                   dst_colorspace='ITU709', dst_color_range=1)
        frame.colorspace, frame.color_range = 1, 1
        return frame

    def read(self):
        try:
            self.stage = 'capture_read'
            if self.pending_frame is not None:
                frame, self.pending_frame = self.pending_frame, None
            elif self.calibration_pending:
                frame = self._calibrate_frame()
            else:
                frame = self.sink.pull()
                frame.colorspace, frame.color_range = self.colorspace, self.color_range
            if frame.format.name != 'nv12' or (frame.width, frame.height) != (self.width, self.height):
                raise RuntimeError(f'Unexpected D3D11 capture output: '
                                   f'{frame.width}x{frame.height} {frame.format.name}')
        except Exception as exc:
            exc.add_note(f'D3D11 stage={self.stage} output={self.width}x{self.height} '
                         f'draw_mouse={self.draw_mouse}')
            raise
        return frame

    def close(self):
        self.graph = None
        self.sink = self.reference_sink = None
        self.pending_frame = None
