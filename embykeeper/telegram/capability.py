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

配置来源 (优先级从高到低):
  1. 环境变量 ``EK_LLM_*`` / ``EK_CAPTCHA_*`` (纯 Docker 部署用);
  2. 面板「AI 与验证码服务」页保存到 ``/app/capability.json``;
  3. 都没有 -> 能力关闭, 不产生任何网络请求, 对应站点快速跳过.

面板保存后按文件 mtime 自动重载, 无需重启进程.
"""

from __future__ import annotations

import base64
from typing import Any, Optional, Tuple

import httpx
from loguru import logger

from embykeeper import capability_settings as settings

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


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value) if value not in (None, "") else default
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


def _http_error_detail(e: BaseException) -> str:
    """带出网关响应正文.

    聚合网关在「无可用渠道 / 上游全挂 / 额度不足 / 限流」时通常只回一个 503,
    真正的原因写在 body 里; 而 httpx 的异常字符串不含正文, 会让日志失去线索.
    """
    if isinstance(e, httpx.HTTPStatusError):
        status = e.response.status_code
        try:
            body = " ".join((e.response.text or "").split())[:300]
        except Exception:
            body = ""
        hint = ""
        if status in (429, 500, 502, 503, 504):
            hint = " (多为中转上游不可用/无可用渠道/限流)"
        return f"HTTP {status}{hint}: {body or str(e).splitlines()[0]}"
    return str(e)


class CapabilityProvider:
    """把云端能力路由到本地 / 自建服务; 未配置的能力一律快速失败."""

    @property
    def llm_enabled(self) -> bool:
        return bool(settings.get("llm_base_url"))

    @property
    def captcha_enabled(self) -> bool:
        return bool(settings.get("captcha_api_url"))

    @staticmethod
    def _client(*, timeout: float, proxy: str = "") -> httpx.AsyncClient:
        kwargs = {"timeout": timeout, "trust_env": False}
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
        base = str(settings.get("llm_base_url") or "").rstrip("/")
        if not base:
            return None, None

        vision_model = str(settings.get("llm_vision_model") or "")
        model = vision_model if vision and vision_model else str(settings.get("llm_model") or "gpt-4o-mini")
        if image:
            encoded = base64.b64encode(image).decode()
            content = [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:{_mime(image)};base64,{encoded}"}},
            ]
        else:
            content = prompt

        headers = {"Content-Type": "application/json"}
        key = str(settings.get("llm_api_key") or "")
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
                timeout=_as_float(settings.get("llm_timeout"), 40),
                proxy=str(settings.get("llm_proxy") or ""),
            ) as client:
                resp = await client.post(f"{base}/chat/completions", json=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()
            answer = (data["choices"][0]["message"].get("content") or "").strip()
            return (answer, f"local:{model}") if answer else (None, None)
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as e:
            log.warning(f"本地模型调用失败 ({model}): {_http_error_detail(e)}")
            return None, None

    async def solve_captcha(self, site: str, url: Optional[str] = None, kind: str = "token") -> Optional[str]:
        """调用自建 Turnstile / hCaptcha solver.

        约定: POST ``captcha_api_url``, JSON ``{"site": ..., "url": ..., "kind": ...}``,
        响应 JSON 中的 ``token`` / ``content`` / ``cf_clearance`` 字段即为结果.
        ``captcha_api_key`` 若设置, 会以 ``Authorization: Bearer`` 发送.
        """
        endpoint = str(settings.get("captcha_api_url") or "")
        if not endpoint:
            return None

        headers = {"Content-Type": "application/json"}
        key = str(settings.get("captcha_api_key") or "")
        if key:
            headers["Authorization"] = f"Bearer {key}"

        payload = {"site": site, "url": url, "kind": kind}
        try:
            async with self._client(
                timeout=_as_float(settings.get("captcha_timeout"), 60),
                proxy=str(settings.get("captcha_proxy") or ""),
            ) as client:
                resp = await client.post(endpoint, json=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()
            if not isinstance(data, dict):
                return None
            return data.get(kind) or data.get("token") or data.get("answer") or None
        except (httpx.HTTPError, TypeError, ValueError) as e:
            log.warning(f"自建验证码服务调用失败 ({site}): {_http_error_detail(e)}")
            return None


provider = CapabilityProvider()
