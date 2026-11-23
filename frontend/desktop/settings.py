"""界面设置持久化（JSON）。

用 JSON 而不是 QSettings 的原因：设置项要和后端 ``AppConfig`` 对齐、还要能被
命令行/脚本读取，INI/注册表都不方便；文件放在用户数据目录（见 ``paths``），
打包后依然可写。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace

from backend.logging.logger import get_logger

from .paths import data_dir, settings_file
from .service import HostSettings, ViewerSettings
from backend.input_capture.mouse import mouse_mode, sensitivity_value
from backend.media.video.codec.probe import codec_options

logger = get_logger(__name__)

DEFAULT_HOST = HostSettings()
DEFAULT_VIEWER = ViewerSettings()
PICTURE_KEYS = ("width", "height", "fps", "bitrate_kbps")
DEVICE_PICTURE_KEYS = PICTURE_KEYS + (
    "sr_enabled", "sr_mode", "sr_percent", "sr_sharpen", "sr_sharpness",
)


@dataclass
class UiSettings:
    """窗口行为（与后端参数分开保存）。"""

    start_on_login: bool = False
    close_to_tray: bool = False
    admin_startup: bool = False
    update_previews: bool = True


def _read() -> dict:
    path = settings_file()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("设置文件损坏，已忽略: %s", path)
        return {}
    return data if isinstance(data, dict) else {}


def load() -> tuple[HostSettings, ViewerSettings]:
    data = _read()
    host = _merge(HostSettings, data.get("host"))
    viewer = _merge(ViewerSettings, data.get("viewer"))
    host.clipboard_enabled = host.gamepad_enabled = True
    host.gamepad_backend = host.gamepad_backend if host.gamepad_backend in {"vigem", "elinkpad"} else "vigem"
    viewer.clipboard_enabled = viewer.gamepad_enabled = True
    viewer.decoder = "h264" if viewer.decoder == "h264_pyav" else viewer.decoder
    protocols = viewer.protocol_preferences
    viewer.protocol_preferences = {
        peer: protocol for peer, protocol in protocols.items()
        if isinstance(peer, str) and len(peer) <= 128 and protocol in {"webrtc", "srt"}
    } if isinstance(protocols, dict) else {}
    viewer.mouse_mode = mouse_mode(viewer.mouse_mode)
    viewer.mouse_sensitivity = sensitivity_value(viewer.mouse_sensitivity)
    viewer.stats_visible = viewer.stats_visible is True
    viewer.sr_enabled = viewer.sr_enabled is True
    viewer.sr_sharpen = viewer.sr_sharpen is True
    viewer.sr_mode = viewer.sr_mode if viewer.sr_mode in {"fsr", "rtx"} else "fsr"
    for name, default, low, high in (("sr_percent", 100, 33, 100), ("sr_sharpness", 50, 0, 100)):
        value = getattr(viewer, name)
        setattr(viewer, name, max(low, min(high, value)) if type(value) is int else default)
    preferences = viewer.codec_preferences if isinstance(viewer.codec_preferences, dict) else {}
    viewer.codec_preferences = {}
    for peer, options in preferences.items():
        if not isinstance(peer, str) or len(peer) > 128:
            continue
        try:
            codec = codec_options({"codec": options.get("codec", "h264")})["codec"]
            viewer.codec_preferences[peer] = {"codec": "h264" if codec == "auto" else codec}
        except (ValueError, AttributeError):
            continue
    viewer.picture_preferences = _load_picture_preferences(
        viewer.picture_preferences, viewer
    )
    return host, viewer


def picture_settings(values: dict) -> dict:
    """校验会话画面参数，供连接层和配置层共同使用。"""
    if not isinstance(values, dict):
        raise ValueError("画面参数格式错误")
    result = {}
    for name, low, high, cast in (
        ("width", 320, 7680, int), ("height", 240, 4320, int),
        ("fps", 5, 120, float), ("bitrate_kbps", 300, 50000, int),
    ):
        value = values.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"画面参数无效：{name}")
        if name in {"width", "height"} and "upscale" in values:
            low = 2
        if not low <= value <= high:
            raise ValueError(f"画面参数超出范围：{name}")
        result[name] = cast(value)
    if result["width"] % 2 or result["height"] % 2:
        raise ValueError("分辨率必须为偶数")
    if "upscale" in values:
        sr = values["upscale"]
        if not isinstance(sr, dict) or set(sr) != {"width", "height", "percent", "mode", "sharpen", "sharpness"}:
            raise ValueError("超分参数格式错误")
        for key, low, high in (("width", 320, 7680), ("height", 240, 4320),
                               ("percent", 33, 100), ("sharpness", 0, 100)):
            if type(sr[key]) is not int or not low <= sr[key] <= high:
                raise ValueError(f"超分参数无效：{key}")
        if sr["mode"] not in {"fsr", "rtx"} or type(sr["sharpen"]) is not bool:
            raise ValueError("超分算法或锐化参数无效")
        for axis in ("width", "height"):
            if sr[axis] % 2 or result[axis] > sr[axis]:
                raise ValueError("超分尺寸无效")
        result["upscale"] = dict(sr)
    return result


def for_device(viewer: ViewerSettings, peer_id: str) -> ViewerSettings:
    """返回设备专属画面设置，缺少时回退到全局默认值。"""
    values = viewer.picture_preferences.get(str(peer_id))
    if not isinstance(values, dict):
        return replace(viewer)
    try:
        picture = picture_settings({key: values[key] for key in PICTURE_KEYS})
    except (KeyError, ValueError, TypeError):
        return replace(viewer)
    result = replace(viewer, **{
        f"stream_{key}": picture[key] for key in PICTURE_KEYS
    })
    for key in DEVICE_PICTURE_KEYS:
        if key in values and hasattr(result, key):
            setattr(result, key, values[key])
    return result


def remember_device_picture(viewer: ViewerSettings, peer_id: str,
                            settings: ViewerSettings) -> None:
    """把当前设备的画面设置写回设备映射。"""
    values = {
        key: getattr(settings, f"stream_{key}") for key in PICTURE_KEYS
    }
    values.update({
        key: getattr(settings, key) for key in DEVICE_PICTURE_KEYS
        if key not in PICTURE_KEYS
    })
    try:
        values = _validated_device_picture(values)
    except (ValueError, TypeError):
        return
    viewer.picture_preferences[str(peer_id)] = values


def load_ui() -> UiSettings:
    data = _read()
    return _merge(UiSettings, data.get("ui"))


def save(host: HostSettings, viewer: ViewerSettings, ui: UiSettings | None = None) -> None:
    path = settings_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "host": asdict(host),
        "viewer": asdict(viewer),
        "ui": asdict(ui or UiSettings()),
    }
    try:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        logger.warning("设置保存失败: %s", path, exc_info=True)


def _merge(cls, values) -> object:
    """只接受已知字段，避免旧版本残留字段导致构造失败。"""
    if not isinstance(values, dict):
        return cls()
    allowed = {field for field in cls.__dataclass_fields__}
    return cls(**{key: value for key, value in values.items() if key in allowed})


def _load_picture_preferences(values, viewer: ViewerSettings) -> dict:
    if not isinstance(values, dict):
        return {}
    result = {}
    for peer, options in values.items():
        if not isinstance(peer, str) or len(peer) > 128:
            continue
        try:
            result[peer] = _validated_device_picture(options)
        except (ValueError, TypeError):
            continue
    return result


def _validated_device_picture(values: dict) -> dict:
    if not isinstance(values, dict):
        raise ValueError("设备画面设置格式错误")
    picture = picture_settings({key: values[key] for key in PICTURE_KEYS})
    result = dict(picture)
    result["sr_enabled"] = values.get("sr_enabled") is True
    result["sr_mode"] = values.get("sr_mode", "fsr")
    result["sr_percent"] = values.get("sr_percent", 100)
    result["sr_sharpen"] = values.get("sr_sharpen") is True
    result["sr_sharpness"] = values.get("sr_sharpness", 50)
    if result["sr_mode"] not in {"fsr", "rtx"}:
        raise ValueError("超分算法无效")
    if type(result["sr_percent"]) is not int or not 33 <= result["sr_percent"] <= 100:
        raise ValueError("超分压缩比无效")
    if type(result["sr_sharpness"]) is not int or not 0 <= result["sr_sharpness"] <= 100:
        raise ValueError("超分锐化参数无效")
    return result


def captures_dir():
    path = data_dir() / "captures"
    path.mkdir(parents=True, exist_ok=True)
    return path
