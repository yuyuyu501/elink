"""界面设置持久化（JSON）。

用 JSON 而不是 QSettings 的原因：设置项要和后端 ``AppConfig`` 对齐、还要能被
命令行/脚本读取，INI/注册表都不方便；文件放在用户数据目录（见 ``paths``），
打包后依然可写。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace

from backend.logging.logger import get_logger
from backend.config.schema import WINDOWS_CAPTURE_BACKENDS

from .paths import data_dir, settings_file
from .service import HostSettings, ViewerSettings
from backend.input_capture.mouse import mouse_mode, sensitivity_value
from backend.media.video.codec.probe import CODEC_DEVICE_KEYS, codec_options

logger = get_logger(__name__)

DEFAULT_HOST = HostSettings()
DEFAULT_VIEWER = ViewerSettings()
PICTURE_KEYS = ("width", "height", "fps", "bitrate_kbps")


@dataclass
class UiSettings:
    """窗口行为（与后端参数分开保存）。"""

    start_on_login: bool = False
    close_to_tray: bool = False
    admin_startup: bool = False
    update_previews: bool = True
    device_name: str = ""


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
    raw_viewer = data.get("viewer")
    if isinstance(raw_viewer, dict) and "volume" not in raw_viewer and raw_viewer.get("muted") is True:
        viewer.volume = 0
    viewer.volume = max(0, min(100, viewer.volume)) if type(viewer.volume) is int else 100
    host.clipboard_enabled = host.gamepad_enabled = True
    host.gamepad_backend = host.gamepad_backend if host.gamepad_backend in {"vigem", "elinkpad"} else "vigem"
    if host.capture_backend not in ("auto", *WINDOWS_CAPTURE_BACKENDS):
        host.capture_backend = "auto"
    viewer.clipboard_enabled = viewer.gamepad_enabled = True
    viewer.decoder = "h264" if viewer.decoder == "h264_pyav" else viewer.decoder
    host.transport = "file"
    viewer.transport = "webrtc"
    viewer.mouse_mode = mouse_mode(viewer.mouse_mode)
    viewer.mouse_sensitivity = sensitivity_value(viewer.mouse_sensitivity)
    viewer.stats_visible = viewer.stats_visible is True
    preferences = viewer.codec_preferences if isinstance(viewer.codec_preferences, dict) else {}
    viewer.codec_preferences = {}
    for peer, options in preferences.items():
        if not isinstance(peer, str) or len(peer) > 128:
            continue
        try:
            preference = codec_options({
                key: options.get(key, "h264" if key == "codec" else "auto")
                for key in ("codec", "encoder", "decoder", *CODEC_DEVICE_KEYS)
            })
            viewer.codec_preferences[peer] = codec_preference(preference)
        except (ValueError, AttributeError):
            continue
    viewer.picture_preferences = _load_picture_preferences(viewer.picture_preferences)
    return host, viewer


def codec_preference(options: dict) -> dict:
    return {
        key: options[key]
        for key in ("codec", "encoder", "decoder", *CODEC_DEVICE_KEYS)
        if key in options and (key == "codec" or options[key] != "auto")
    }


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
        if not low <= value <= high:
            raise ValueError(f"画面参数超出范围：{name}")
        result[name] = cast(value)
    if result["width"] % 2 or result["height"] % 2:
        raise ValueError("分辨率必须为偶数")
    return result


def for_device(viewer: ViewerSettings, peer_id: str) -> ViewerSettings:
    """返回设备专属画面设置，缺少时回退到全局默认值。"""
    values = viewer.picture_preferences.get(str(peer_id))
    if not isinstance(values, dict):
        return replace(viewer)
    try:
        picture = picture_settings(values)
    except ValueError:
        return replace(viewer)
    return replace(viewer, **{
        f"stream_{key}": picture[key] for key in PICTURE_KEYS
    })


def remember_device_picture(viewer: ViewerSettings, peer_id: str,
                            settings: ViewerSettings) -> None:
    """把当前设备的画面设置写回设备映射。"""
    values = {
        key: getattr(settings, f"stream_{key}") for key in PICTURE_KEYS
    }
    try:
        values = picture_settings(values)
    except ValueError:
        return
    viewer.picture_preferences[str(peer_id)] = values


def remember_device_preferences(viewer: ViewerSettings, peer_id: str, picture: dict,
                                options: dict) -> None:
    viewer.picture_preferences = {**viewer.picture_preferences, str(peer_id): picture_settings(picture)}
    viewer.codec_preferences = {**viewer.codec_preferences,
                                str(peer_id): codec_preference(codec_options(options))}


def normalize_device_name(value: object) -> str:
    name = value.strip() if isinstance(value, str) else ""
    return name if 1 <= len(name) <= 80 else ""


def load_ui() -> UiSettings:
    data = _read()
    ui = _merge(UiSettings, data.get("ui"))
    ui.device_name = normalize_device_name(ui.device_name)
    return ui


def save(host: HostSettings, viewer: ViewerSettings, ui: UiSettings | None = None) -> bool:
    path = settings_file()
    payload = {
        "host": asdict(host),
        "viewer": asdict(viewer),
        "ui": asdict(ui or UiSettings()),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        logger.warning("设置保存失败: %s", path, exc_info=True)
        return False
    return True


def _merge(cls, values) -> object:
    """只接受已知字段，避免旧版本残留字段导致构造失败。"""
    if not isinstance(values, dict):
        return cls()
    allowed = {field for field in cls.__dataclass_fields__}
    return cls(**{key: value for key, value in values.items() if key in allowed})


def _load_picture_preferences(values) -> dict:
    if not isinstance(values, dict):
        return {}
    result = {}
    for peer, options in values.items():
        if not isinstance(peer, str) or len(peer) > 128:
            continue
        try:
            result[peer] = picture_settings(options)
        except ValueError:
            continue
    return result


def captures_dir():
    path = data_dir() / "captures"
    path.mkdir(parents=True, exist_ok=True)
    return path
