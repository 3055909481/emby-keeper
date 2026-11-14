"""本地 / 自建能力提供者.

上游把验证码求解、智能回答、OCR、题库等能力都放在作者运营的
Telegram Bot ``@embykeeper_auth_bot`` 后面. 该 Bot 既是「赞助等级」的
授权网关, 也是这些计算能力的实际提供方; 一旦停服, 签到 / 监控 / 水群
模块会在 ``if not Link(client).auth(...): return`` 处被整段跳过.

本模块把「能力」从「授权」中剥离出来, 改为可插拔的本地实现:

* 智能回答 (gpt / visual / infer / *_answer) -> 任意 OpenAI 兼容端点
  (OpenAI、DeepSeek、Ollama、one-api、LM Studio ...), 未配置则快速返回 None;
* 验证码 (captcha / captcha_content) -> 用户自建的 solver HTTP 服务,
  未配置则快速返回 None, 对应站点自行跳过;
* OCR -> 复用项目自带的 ddddocr (``embykeeper/ocr.py``), 在 link.py 中接入.

所有配置走环境变量, 不改 config.toml / schema, Docker 里直接加即可.
未配置的能力不产生任何网络请求, 避免每个站点都卡满 60s x 3 的重试.
"""

from __future__ import annotations

import base64
import os
from typing import Optional, Tuple

import httpx
from loguru import logger

log = logger.bind(scheme="capability")

# 给答题 / 话术类请求一个统一的约束, 站点代码会按 [SEND] / [CLICK] / @@@ 等
# 标记解析结果, 多余的解释会让它解析失败.
ANSWER_SYSTEM = (
    "你是 Embykeeper 的自动应答助手. 严格按用户要求输出, 不要解释、不要寒暄、"
    "不要添加 Markdown 代码块或多余文字. 若无法确定答案, 输出空字符串."
)
INFER_SYSTEM = (
    "你是一个真实的群组聊天参与者. 根据用户给出的聊天记录, 推测下一条自然、简短、"
    "口语化的发言. 只输出发言内容本身, 不要解释、不要加引号. 若不应发言, 输出 SKIP."
)


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return default


def _mime(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"GIF8"):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


class CapabilityProvider:
    """把云端能力路由到本地 / 自建服务; 未配置的能力一律快速失败."""

    # --- OpenAI 兼容端点 -------------------------------------------------
    # EK_LLM_BASE_URL   例: https://api.openai.com/v1 或 http://ollama:11434/v1
    # EK_LLM_API_KEY    自建端点可留空
    # EK_LLM_MODEL      默认 gpt-4o-mini
    # EK_LLM_VISION_MODEL  视觉模型, 留空则复用 EK_LLM_MODEL
    # EK_LLM_PROXY      可选, 访问境外 API 时显式指定; 默认不读环境代理
    # EK_LLM_TIMEOUT    默认 40 秒

    @property
    def llm_enabled(self) -> bool:
        return bool(_env("EK_LLM_BASE_URL"))

    @property
    def captcha_enabled(self) -> bool:
        return bool(_env("EK_CAPTCHA_API_URL"))

    def _client(self, *, timeout: float, proxy_var: str) -> httpx.AsyncClient:
        kwargs = {"timeout": timeout, "trust_env": False}
        proxy = _env(proxy_var)
        if proxy:
            kwargs["proxy"] = proxy
        return httpx.AsyncClient(**kwargs)

    async def chat(
        self,
        prompt: str,
        *,
        image: Optional[bytes] = None,
        vision: bool = False,
        system: str = ANSWER_SYSTEM,
    ) -> Tuple[Optional[str], Optional[str]]:
        """调用 OpenAI 兼容的 /chat/completions, 返回 (答案, 来源标识)."""
        base = _env("EK_LLM_BASE_URL").rstrip("/")
        if not base:
            return None, None

        model = (
            _env("EK_LLM_VISION_MODEL")
            if vision and _env("EK_LLM_VISION_MODEL")
            else _env("EK_LLM_MODEL", "gpt-4o-mini")
        )
        if image:
            encoded = base64.b64encode(image).decode()
            content = [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:{_mime(image)};base64,{encoded}"}},
            ]
        else:
            content = prompt

        headers = {"Content-Type": "application/json"}
        key = _env("EK_LLM_API_KEY")
        if key:
            headers["Authorization"] = f"Bearer {key}"

        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": content},
            ],
        }
        try:
            async with self._client(
                timeout=_env_float("EK_LLM_TIMEOUT", 40), proxy_var="EK_LLM_PROXY"
            ) as client:
                resp = await client.post(f"{base}/chat/completions", json=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()
            answer = (data["choices"][0]["message"].get("content") or "").strip()
            return (answer, f"local:{model}") if answer else (None, None)
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as e:
            log.warning(f"本地模型调用失败 ({model}): {e}")
            return None, None

    async def solve_captcha(self, site: str, url: Optional[str] = None, kind: str = "token") -> Optional[str]:
        """调用自建 Turnstile / hCaptcha solver.

        约定: POST ``EK_CAPTCHA_API_URL``, JSON ``{"site": ..., "url": ..., "kind": ...}``,
        响应 JSON 中的 ``token`` / ``content`` / ``cf_clearance`` 字段即为结果.
        ``EK_CAPTCHA_API_KEY`` 若设置, 会以 ``Authorization: Bearer`` 发送.
        """
        endpoint = _env("EK_CAPTCHA_API_URL")
        if not endpoint:
            return None

        headers = {"Content-Type": "application/json"}
        key = _env("EK_CAPTCHA_API_KEY")
        if key:
            headers["Authorization"] = f"Bearer {key}"

        payload = {"site": site, "url": url, "kind": kind}
        try:
            async with self._client(
                timeout=_env_float("EK_CAPTCHA_TIMEOUT", 60), proxy_var="EK_CAPTCHA_PROXY"
            ) as client:
                resp = await client.post(endpoint, json=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()
            if not isinstance(data, dict):
                return None
            return data.get(kind) or data.get("token") or data.get("answer") or None
        except (httpx.HTTPError, TypeError, ValueError) as e:
            log.warning(f"自建验证码服务调用失败 ({site}): {e}")
            return None


provider = CapabilityProvider()
