"""Matched 50% NVENC/D3D11VA quality comparison and real WebRTC presentation runs.

Only generated test images are saved. No desktop screenshots or user data.
Readbacks/quality scoring are excluded from the separate throughput measurements.
"""
import argparse
import fractions
import json
import os
from pathlib import Path
import subprocess
import sys

import av
from av.codec.hwaccel import HWAccel
import cv2
import numpy as np
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen
from PySide6.QtWidgets import QApplication

from elink.core.codecs import CodecPolicy, make_encoder
from elink.ui.rtx_video import RtxProcessor
from scripts.check_gpu_video import player, settle


def pixels(image):
    image = image.convertToFormat(QImage.Format.Format_RGB888)
    return np.frombuffer(image.constBits(), dtype=np.uint8).reshape(
        image.height(), image.bytesPerLine())[:, :image.width()*3].reshape(image.height(), image.width(), 3).copy()


def test_scene(kind):
    width, height = 1920, 1080
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(QColor('#202329'))
    painter = QPainter(image)
    if kind == 'texture':
        rng = np.random.default_rng(420)
        field = np.zeros((height, width), dtype=np.float32)
        for spacing, gain in ((8, 50), (24, 70), (80, 100)):
            noise = rng.random((height//spacing+1, width//spacing+1)).astype(np.float32)
            field += gain * cv2.resize(noise, (width, height), interpolation=cv2.INTER_CUBIC)
        rgb = np.stack((field*.7, field, field*.8), axis=2).clip(0,255).astype(np.uint8)
        painter.drawImage(0, 0, QImage(rgb.data, width, height, rgb.strides[0], QImage.Format.Format_RGB888))
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    for index, size in enumerate((12, 16, 20, 28, 40)):
        font = QFont('Microsoft YaHei')
        font.setPixelSize(size)
        painter.setFont(font)
        painter.setPen(QColor('#f1f3f4'))
        painter.drawText(40, 50+index*65, 'Elink 90 FPS | 1920 x 1080 | ABCxyz 0123456789')
        painter.setPen(QColor('#61d8ad'))
        painter.drawText(1040, 50+index*65, 'Health 100 / Ammo 30 / Target 128m')
    if kind == 'desktop':
        painter.fillRect(30, 390, 1860, 650, QColor('#f8fafb'))
        for row in range(18):
            painter.setPen(QColor('#263238'))
            font.setPixelSize(18)
            painter.setFont(font)
            painter.drawText(50, 425+row*32, f'{row+1:02d}  const frame = decoder.receive();  // sample text 1920 x 1080')
            painter.setPen(QPen(QColor('#c6ccd0'), 1))
            painter.drawLine(1000, 425+row*32, 1850, 425+row*32)
    else:
        for index in range(70):
            painter.setPen(QPen(QColor('#f7da72' if index%2 else '#d8e5ef'), 1+index%4))
            painter.drawLine(40+index*25, 450, 1900-index*23, 1000)
    painter.end()
    return pixels(image)


def compressed_frame(rgb, scale, fps, bitrate):
    height, width = rgb.shape[:2]
    width, height = width*scale//100, height*scale//100
    source = av.VideoFrame.from_ndarray(rgb, format='rgb24').reformat(
        width=width, height=height, format='yuv420p', dst_colorspace='ITU709', dst_color_range=1)
    source.colorspace, source.color_range = 1, 1
    codec = make_encoder('h264_nvenc', width, height, CodecPolicy(fps=fps, bitrate=bitrate*1_000_000),
                         colorspace=1, color_range=1)
    decoder = av.CodecContext.create('h264', 'r', hwaccel=HWAccel('d3d11va', allow_software_fallback=False))
    decoded = None
    # Compare a settled inter-frame stream, rather than differently budgeted IDRs.
    for index in range(30):
        source.pts, source.time_base = index*90000//fps, fractions.Fraction(1,90000)
        for packet in codec.encode(source):
            for decoded in decoder.decode(packet):
                pass
    if decoded is None:
        raise RuntimeError('NVENC/D3D11VA did not produce a comparison frame')
    return decoded


def quality(reference, actual):
    mse = float(np.mean((reference.astype(np.float32)-actual.astype(np.float32))**2))
    a = cv2.cvtColor(reference, cv2.COLOR_RGB2GRAY).astype(np.float32)
    b = cv2.cvtColor(actual, cv2.COLOR_RGB2GRAY).astype(np.float32)
    mu_a, mu_b = (cv2.GaussianBlur(value, (11,11), 1.5) for value in (a,b))
    var_a = cv2.GaussianBlur(a*a, (11,11), 1.5)-mu_a*mu_a
    var_b = cv2.GaussianBlur(b*b, (11,11), 1.5)-mu_b*mu_b
    cov = cv2.GaussianBlur(a*b, (11,11), 1.5)-mu_a*mu_b
    ssim = ((2*mu_a*mu_b+6.5025)*(2*cov+58.5225))/((mu_a*mu_a+mu_b*mu_b+6.5025)*(var_a+var_b+58.5225))
    return dict(psnr_db=round(float(10*np.log10(255**2/max(mse,1e-10))),4), ssim_y=round(float(ssim[5:-5,5:-5].mean()),6))


def render(app, frame, upscaler, scale):
    result = None
    view = player(app, 'gpu', statistics=False, render_scale=scale, upscaler=upscaler)
    view.preferences['resolution'] = [1920,1080]
    # The helper initially creates an empty surface; select output dimensions
    # before its GL context initialization by rebuilding it when necessary.
    surface = view.gpu_surface
    surface.makeCurrent()
    if surface.upscaler:
        surface.upscaler.cleanup()
    from elink.ui.super_resolution import FsrUpscaler
    from elink.ui.rtx_video import RtxUpscaler
    surface.upscaler = (RtxUpscaler if upscaler=='rtx' else FsrUpscaler)(surface,(1920,1080)) if scale!=100 else None
    surface.doneCurrent()
    try:
        for _ in range(8):
            surface.set_frame(frame)
            app.processEvents()
        metadata = view.client.profile.snapshot()['metadata']
        expected = 'RTX-VSR' if upscaler=='rtx' else 'FSR1-EASU-RCAS'
        if scale!=100 and metadata.get('upscaler')!=expected:
            raise RuntimeError('Comparison upscaler fell back: '+view.error_label.text())
        surface.makeCurrent()
        if scale==100:
            result = frame.to_ndarray(format='rgb24')
        elif upscaler=='fsr':
            result = pixels(surface.upscaler.buffers[2].toImage())
        surface.doneCurrent()
    finally:
        view.close()
        view.deleteLater()
        app.processEvents()
    if scale != 100 and upscaler == 'rtx':
        # Do not keep a displayed VSR processor alive alongside the validation
        # processor: the driver's automatic policy can disable the second one.
        processor = RtxProcessor(frame.width, frame.height, (1920,1080))
        try:
            for _ in range(8):
                processor.render(frame)
            result = np.empty((1080,1920,4), dtype=np.uint8)
            processor.download(result)
            result = result[:,:,:3][:,:,::-1].copy()
        finally:
            processor.close()
    return result


def compare(output, app, fps=90, bitrate=20):
    output.mkdir(parents=True, exist_ok=True)
    report = dict(quality=[], scale_percent=50, input=[960,540], output=[1920,1080], fps=fps, bitrate_mbps=bitrate,
                  limitations='Generated stills, not subjective game quality. Same decoded 540p frame for both upscalers. '
                              'Quality readbacks excluded from separate loopback throughput runs. No input-to-photon measurement.')
    for kind in ('desktop','texture','diagonals'):
        reference = test_scene(kind)
        low = compressed_frame(reference,50,fps,bitrate)
        native = compressed_frame(reference,100,fps,bitrate)
        images = {'reference':reference, 'native':render(app,native,'fsr',100),
                  'fsr50':render(app,low,'fsr',50), 'rtx50':render(app,low,'rtx',50)}
        control = RtxProcessor(low.width,low.height,(1920,1080),enabled=False)
        try:
            control.render(low)
            disabled = np.empty((1080,1920,4),dtype=np.uint8)
            control.download(disabled)
            difference = float(np.mean(np.abs(images['rtx50'].astype(float)-disabled[:,:,:3][:,:,::-1].astype(float))))
            if difference < .01:
                raise RuntimeError('RTX extension accepted, but enabled/disabled output is indistinguishable')
        finally:
            control.close()
        row = dict(scene=kind, rtx_enabled_disabled_mae=round(difference,4),
                   scores={name:quality(reference,value) for name,value in images.items() if name!='reference'})
        report['quality'].append(row)
        # Full-size outputs and a pixel-for-pixel crop comparison, all synthetic.
        crops=[]
        for name,rgb in images.items():
            cv2.imwrite(str(output/f'{kind}-{name}.png'),cv2.cvtColor(rgb,cv2.COLOR_RGB2BGR))
            crop=rgb[0:350,20:740].copy()
            crop=cv2.copyMakeBorder(crop,35,0,0,0,cv2.BORDER_CONSTANT,value=(20,20,20))
            cv2.putText(crop,name,(10,25),cv2.FONT_HERSHEY_SIMPLEX,.65,(255,255,255),1,cv2.LINE_AA)
            crops.append(crop)
        montage=np.concatenate((np.concatenate(crops[:2],axis=1),np.concatenate(crops[2:],axis=1)),axis=0)
        cv2.imwrite(str(output/f'{kind}-comparison.png'),cv2.cvtColor(montage,cv2.COLOR_RGB2BGR))
        print(json.dumps(row),flush=True)
    return report


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=Path('.artifacts/upscaler-comparison'))
    parser.add_argument('--seconds',type=float,default=15)
    parser.add_argument('--quality-only',action='store_true')
    args=parser.parse_args(argv)
    os.environ['QT_QPA_PLATFORM']='windows'
    app=QApplication.instance() or QApplication([])
    report=compare(args.output,app)
    (args.output/'comparison.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    if not args.quality_only:
        report['throughput']={}
        for name,scale,algorithm in (('native',100,'fsr'),('fsr50',50,'fsr'),('rtx50',50,'rtx')):
            target=args.output/f'{name}-stream.json'
            subprocess.run([sys.executable,'-m','scripts.benchmark_stream','--source','motion','--present','--separate-host',
                            '--render-scale',str(scale),'--upscaler',algorithm,'--fps','90','--seconds',str(args.seconds),
                            '--warmup','3','--output',str(target)],check=True)
            data=json.loads(target.read_text(encoding='utf-8'))
            from scripts.benchmark_matrix import assess
            if not assess(data)['within_five_percent']:
                raise RuntimeError(f'{name}: loopback did not meet 95% of requested FPS')
            if scale==50 and data['client']['metadata'].get('upscaler')!=('RTX-VSR' if algorithm=='rtx' else 'FSR1-EASU-RCAS'):
                raise RuntimeError(f'{name}: upscaler unexpectedly fell back')
            report['throughput'][name]=dict(rates=data['rates'],stages=data['client']['stages'])
    (args.output/'comparison.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(args.output/'comparison.json')


if __name__=='__main__':
    main()
