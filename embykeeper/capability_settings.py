"""面板可配置的「本地 / 自建能力」设置存储.

配置保存在 ``/app/capability.json`` (与 config.toml 同目录, 随 Docker 数据卷
持久化). 不写进 config.toml, 是为了避免 API 密钥出现在面板的「配置导出」里,
也避免改动 CoreConfig schema 影响现有设置页.

优先级: 环境变量 > 面板设置 > 未配置(能力关闭).
每次读取按文件 mtime 判断是否需要重载, 因此面板保存后无需重启进程即可生效.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

SETTINGS_FILENAME = "capability.json"
SETTINGS_PATH_ENV = "EK_CAPABILITY_SETTINGS_FILE"
MASK = "********"

# JSON 字段 -> 兼容的环境变量名 (环境变量优先, 便于纯 Docker 部署)
ENV_KEYS = {
    "llm_base_url": "EK_LLM_BASE_URL",
    "llm_api_key": "EK_LLM_API_KEY",
    "llm_model": "EK_LLM_MODEL",
    "llm_vision_model": "EK_LLM_VISION_MODEL",
    "llm_proxy": "EK_LLM_PROXY",
    "llm_timeout": "EK_LLM_TIMEOUT",
    "captcha_api_url": "EK_CAPTCHA_API_URL",
    "captcha_api_key": "EK_CAPTCHA_API_KEY",
    "captcha_proxy": "EK_CAPTCHA_PROXY",
    "captcha_timeout": "EK_CAPTCHA_TIMEOUT",
}
SECRET_FIELDS = ("llm_api_key", "captcha_api_key")


class CapabilitySettings(BaseModel):
    """面板「AI 与验证码服务」页的可编辑字段."""

    llm_enabled: bool = True
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = "gpt-4o-mini"
    llm_vision_model: str = ""
    llm_proxy: str = ""
    llm_timeout: float = Field(40.0, gt=0)
    captcha_enabled: bool = True
    captcha_api_url: str = ""
    captcha_api_key: str = ""
    captcha_proxy: str = ""
    captcha_timeout: float = Field(60.0, gt=0)


def settings_path() -> Path:
    override = os.environ.get(SETTINGS_PATH_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    base = os.environ.get("EMBYKEEPERAPI_BASEDIR") or os.environ.get("HOME") or "/app"
    return Path(base) / SETTINGS_FILENAME


_cache: Dict[str, Any] = {"mtime": None, "data": {}}


def _read() -> Dict[str, Any]:
    """按 mtime 缓存读取, 面板保存后下一次调用自动生效."""
    path = settings_path()
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        _cache["mtime"], _cache["data"] = None, {}
        return {}
    if _cache["mtime"] != mtime:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            data = CapabilitySettings(**raw).model_dump()
        except Exception:
            data = CapabilitySettings().model_dump()
        _cache["mtime"], _cache["data"] = mtime, data
    return _cache["data"]


def load() -> CapabilitySettings:
    return CapabilitySettings(**_read())


def save(settings: CapabilitySettings) -> None:
    """原子写入, 避免面板并发保存时读到半截文件."""
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(settings.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    _cache["mtime"], _cache["data"] = None, {}


def get(name: str, default: Any = None) -> Any:
    """取有效配置: 环境变量优先, 其次面板设置 (受启用开关控制), 最后默认值."""
    env_key = ENV_KEYS.get(name)
    if env_key:
        value = os.environ.get(env_key)
        if value:
            return value.strip()
    data = _read()
    if name.startswith("llm_") and name != "llm_enabled" and not data.get("llm_enabled", True):
        return default
    if name.startswith("captcha_") and name != "captcha_enabled" and not data.get("captcha_enabled", True):
        return default
    value = data.get(name, default)
    return default if value is None else value


def env_overrides() -> List[str]:
    """当前被环境变量锁定的字段, 用于提示用户 Web 配置不会生效."""
    return [env for env in ENV_KEYS.values() if os.environ.get(env)]


def public_view() -> Dict[str, Any]:
    """供给前端: 密钥脱敏, 并附带环境变量覆盖列表."""
    data = load().model_dump()
    for key in SECRET_FIELDS:
        data[key] = MASK if data.get(key) else ""
    data["env_overrides"] = env_overrides()
    return data


def merge_secret_update(payload: Dict[str, Any]) -> CapabilitySettings:
    """面板回传 MASK 表示保留原密钥; 传空字符串表示清除.

    以当前配置为底合并, 因此只提交部分字段也不会把其余设置重置为默认值.
    """
    current = load().model_dump()
    update = dict(payload)
    update.pop("env_overrides", None)
    for key in SECRET_FIELDS:
        if update.get(key) == MASK:
            update[key] = current.get(key, "")
    return CapabilitySettings(**{**current, **update})
