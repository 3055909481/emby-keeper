import asyncio
import os
import random
import time
from typing import Callable, Coroutine, List, Optional, Tuple, Union
import uuid
from io import BytesIO

import tomli
from loguru import logger
from pyrogram import filters
from pyrogram.handlers import MessageHandler
from pyrogram.enums import ParseMode
from pyrogram.types import Message
from pyrogram.errors.exceptions.bad_request_400 import YouBlockedUser
from pyrogram.errors import FloodWait

from embykeeper.utils import async_partial, truncate_str

from .lock import super_ad_shown, super_ad_shown_lock, authed_services, authed_services_lock
from .pyrogram import Client

# 云端认证等待; 上游默认 60s x 3, Auth Bot 失效时会把登录/签到流程拖死
try:
    _AUTH_TIMEOUT = float(os.environ.get("EK_LINK_AUTH_TIMEOUT", "15"))
    _AUTH_RETRIES = max(1, int(os.environ.get("EK_LINK_AUTH_RETRIES", "2")))
except ValueError:
    _AUTH_TIMEOUT, _AUTH_RETRIES = 15.0, 2
# 设为 1 恢复上游严格行为 (认证失败即跳过模块)
_STRICT_AUTH = os.environ.get("EK_LINK_STRICT_AUTH", "").strip().lower() in ("1", "true", "yes")


class LinkError(Exception):
    pass


class Link:
    """云服务类, 用于认证和高级权限任务通讯."""

    bot = "embykeeper_auth_bot"
    post_count = 0

    # 确认 Auth Bot 无响应后进入熔断窗口, 期间云端请求直接跳过, 避免每个站点都卡满超时
    _offline_until = 0.0
    _offline_ttl = 300.0

    @classmethod
    def is_offline(cls):
        return time.monotonic() < cls._offline_until

    @classmethod
    def _mark_offline(cls):
        cls._offline_until = time.monotonic() + cls._offline_ttl

    def __init__(self, client: Client):
        self.client = client
        self.log = logger.bind(scheme="telelink", username=client.me.full_name)

    @property
    def instance(self):
        """当前设备识别码."""
        rd = random.Random()
        rd.seed(uuid.getnode())
        return uuid.UUID(int=rd.getrandbits(128))

    async def delete_messages(self, messages: List[Message]):
        """删除一系列消息."""

        async def delete(m: Message):
            try:
                await asyncio.wait_for(m.delete(revoke=True), 3)
                text = m.text or m.caption or "图片或其他内容"
                text = truncate_str(text.replace("\n", ""), 30)
                self.log.debug(f"[gray50]删除了 API 消息记录: {text}[/]")
            except asyncio.TimeoutError:
                pass

        return await asyncio.gather(*[delete(m) for m in messages])

    async def post(self, *args, stop_grace: float = 0, **kw):
        async def stop(task: asyncio.Task):
            if stop_grace and not task.done():
                await asyncio.sleep(stop_grace)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        task = asyncio.create_task(self._post(*args, **kw))
        stop_handler = async_partial(stop, task=task)
        self.client.stop_handlers.append(stop_handler)
        try:
            return await task
        finally:
            self.client.stop_handlers.remove(stop_handler)

    async def _post(
        self,
        cmd,
        photo=None,
        file=None,
        condition: Callable = None,
        timeout: int = 60,
        retries=3,
        name: str = None,
        fail: bool = False,
    ) -> Tuple[Optional[str], Optional[str]]:
        """
        向机器人发送请求.
        参数:
            cmd: 命令字符串
            condition: 布尔或函数, 参数为响应 toml 的字典形式, 决定该响应是否为有效响应.
            timeout: 超时 (s)
            retries: 最大重试次数
            name: 请求名称, 用于用户提示
            fail: 当出现错误时抛出错误, 而非发送日志
        """
        Link.post_count += 1
        try:
            if Link.is_offline():
                self.log.debug(f"[gray50]账号服务无响应, 跳过云端请求: {name}[/]")
                return None
            self.log.info(f"正在进行服务请求: {name}")

            if photo and file:
                raise ValueError("can not use both photo and file")

            for r in range(retries):
                try:
                    await self.client.mute_chat(self.bot)
                except FloodWait:
                    self.log.debug(f"[gray50]设置禁用提醒因访问超限而失败: {self.bot}[/]")
                future = asyncio.Future()
                handler = MessageHandler(
                    async_partial(self._handler, cmd=cmd, future=future, condition=condition),
                    filters.text & filters.bot & filters.user(self.bot),
                )
                await self.client.add_handler(handler, group=1)
                try:
                    messages = []
                    if photo:
                        messages.append(
                            await self.client.send_photo(
                                self.bot, photo, caption=cmd, parse_mode=ParseMode.DISABLED
                            )
                        )
                    elif file:
                        messages.append(
                            await self.client.send_document(
                                self.bot, file, caption=cmd, parse_mode=ParseMode.DISABLED
                            )
                        )
                    else:
                        messages.append(
                            await self.client.send_message(self.bot, cmd, parse_mode=ParseMode.DISABLED)
                        )
                    self.log.debug(f"[gray50]-> {cmd}[/]")
                    results = await asyncio.wait_for(future, timeout=timeout)
                except asyncio.CancelledError:
                    try:
                        await asyncio.wait_for(self.delete_messages(messages), 3)
                    except asyncio.TimeoutError:
                        pass
                    finally:
                        raise
                except asyncio.TimeoutError:
                    await self.delete_messages(messages)
                    if r + 1 < retries:
                        self.log.info(f"{name}超时 ({r + 1}/{retries}), 将在 3 秒后重试.")
                        await asyncio.sleep(3)
                        continue
                    else:
                        msg = f"{name}超时 ({r + 1}/{retries})."
                        Link._mark_offline()
                        if fail:
                            raise LinkError(msg)
                        else:
                            self.log.warning(msg)
                            return None
                except YouBlockedUser:
                    msg = "您在账户中禁用了用于 API 信息传递的 Bot: @embykeeper_auth_bot, 这将导致 embykeeper 无法运行, 请尝试取消禁用."
                    if fail:
                        raise LinkError(msg)
                    else:
                        self.log.error(msg)
                        return None
                else:
                    await self.delete_messages(messages)
                    status, errmsg = [results.get(p, None) for p in ("status", "errmsg")]
                    if status == "error":
                        if fail:
                            raise LinkError(f"{errmsg}.")
                        else:
                            self.log.warning(f"{name}错误: {errmsg}.")
                            return False
                    elif status == "ok":
                        self.log.info(f"服务请求完成: {name}")
                        return results
                    else:
                        if fail:
                            raise LinkError("出现未知错误.")
                        else:
                            self.log.warning(f"{name}出现未知错误.")
                            return False
                finally:
                    try:
                        await self.client.remove_handler(handler, group=1)
                    except:
                        pass

        finally:
            Link.post_count -= 1

    async def _handler(
        self,
        client: Client,
        message: Message,
        cmd: str,
        future: asyncio.Future,
        condition: Union[bool, Callable[..., Coroutine], Callable] = None,
    ):
        try:
            toml = tomli.loads(message.text)
        except tomli.TOMLDecodeError:
            await self.delete_messages([message])
        else:
            try:
                if toml.get("command", None) == cmd:
                    if condition is None:
                        cond = True
                    elif asyncio.iscoroutinefunction(condition):
                        cond = await condition(toml)
                    elif callable(condition):
                        cond = condition(toml)
                    if cond:
                        if not future.done():
                            future.set_result(toml)
                        await asyncio.sleep(0.5)
                        await self.delete_messages([message])
                        return
            except asyncio.CancelledError as e:
                try:
                    await asyncio.wait_for(self.delete_messages([message]), 3)
                except asyncio.TimeoutError:
                    pass
                finally:
                    if not future.done():
                        future.set_exception(e)
                    raise
            else:
                message.continue_propagation()

    async def auth(self, service: str, log_func=None):
        """向机器人发送授权请求.

        Auth Bot 是作者运营的云端服务, 不可达时上游会让整个签到/监控模块直接
        return, 连本地本不需要云端能力的功能也被阻断. 这里改为:
        - 缩短认证等待, 超时后进入熔断窗口, 后续云端请求快速失败;
        - 区分 "超时" (服务不可达) 与 "权限不足" (明确拒绝), 前者按离线模式放行;
        - EK_LINK_STRICT_AUTH=1 可恢复上游的严格行为.
        """
        async with authed_services_lock:
            user_auth_cache = authed_services.get(self.client.me.id, {}).get(service, None)
            if user_auth_cache is not None:
                return user_auth_cache

            def finish(allowed: bool, message: str = None):
                authed_services.setdefault(self.client.me.id, {})[service] = allowed
                if message:
                    (log_func or self.log.warning)(message)
                return allowed

            try:
                result = await self.post(
                    f"/auth {service} {self.instance}",
                    name=f"服务 {service.upper()} 认证",
                    timeout=_AUTH_TIMEOUT,
                    retries=_AUTH_RETRIES,
                    fail=True,
                )
            except LinkError as e:
                errmsg = str(e)
                if "超时" in errmsg and not _STRICT_AUTH:
                    return finish(
                        True,
                        f"账号服务 {service.upper()} 认证无响应, 已切换离线模式: "
                        f"本地功能继续运行, 依赖云端解析的站点将自动跳过.",
                    )
                if "权限不足" in errmsg:
                    await self._show_super_ad()
                return finish(False, f"初始化错误: 使用 {service.upper()} 服务, 但{errmsg}")
            else:
                if not result and Link.is_offline() and not _STRICT_AUTH:
                    return finish(True, f"账号服务不可用, 服务 {service.upper()} 认证已跳过 (离线模式).")
                return finish(bool(result))

    async def _show_super_ad(self):
        async with super_ad_shown_lock:
            user_super_ad_shown = super_ad_shown.get(self.client.me.id, False)
            if not user_super_ad_shown:
                self.log.info("请访问 https://go.zetx.tech/eksuper 赞助项目以升级为高级用户, 尊享更多功能.")
                super_ad_shown[self.client.me.id] = True
                return True
            else:
                return False

    async def captcha(self, site: str, url: str = None) -> Optional[str]:
        """向机器人发送验证码解析请求."""
        cmd = f"/captcha {self.instance} {site}"
        if url:
            cmd += f" {url}"
        results = await self.post(cmd, timeout=120, name="请求跳过验证码")
        if results:
            return results.get("token", None)
        else:
            return None

    async def captcha_content(self, site: str, url: str = None) -> Optional[str]:
        """向机器人发送带验证码的远程网页解析请求."""
        cmd = f"/captcha {self.instance} {site}"
        if url:
            cmd += f" {url}"
        results = await self.post(cmd, timeout=120, name="请求跳过验证码")
        if results:
            return results.get("content", None)
        else:
            return None

    async def wssocks(self) -> Tuple[Optional[str], Optional[str]]:
        """向机器人发送逆向 Socks 代理隧道监听请求."""
        cmd = f"/wssocks {self.instance}"
        results = await self.post(cmd, timeout=20, name="请求新建代理隧道以跳过验证码")
        if results:
            return results.get("url", None), results.get("token", None)
        else:
            return None, None

    async def captcha_wssocks(
        self, token: str, url: str, user_agent: Optional[str] = None
    ) -> Tuple[Optional[str], Optional[str]]:
        """向机器人发送通过代理隧道进行验证码解析请求."""
        cmd = f"/captcha_wssocks {self.instance} {token} {url}"
        if user_agent:
            cmd += f" {user_agent}"
        results = await self.post(cmd, timeout=120, name="请求跳过验证码")
        if results:
            return results.get("cf_clearance", None), results.get("useragent", None)
        else:
            return None, None

    async def pornemby_answer(self, question: str) -> Tuple[Optional[str], Optional[str]]:
        """向机器人发送问题回答请求."""
        results = await self.post(
            f"/pornemby_answer {self.instance} {question}", timeout=20, name="请求问题回答"
        )
        if results:
            return results.get("answer", None), results.get("by", None)
        else:
            return None, None

    async def terminus_answer(self, question: str) -> Tuple[Optional[str], Optional[str]]:
        """向机器人发送问题回答请求."""
        results = await self.post(
            f"/terminus_answer {self.instance} {question}", timeout=20, name="请求问题回答"
        )
        if results:
            return results.get("answer", None), results.get("by", None)
        else:
            return None, None

    async def gpt(self, prompt: str) -> Tuple[Optional[str], Optional[str]]:
        """向机器人发送智能回答请求."""
        results = await self.post(f"/gpt {self.instance} {prompt}", timeout=40, name="请求智能回答")
        if results:
            return results.get("answer", None), results.get("by", None)
        else:
            return None, None

    async def visual(self, photo, options: List[str], question=None) -> Tuple[Optional[str], Optional[str]]:
        """向机器人发送视觉问题解答请求."""
        cmd = f"/visual {self.instance} {'/'.join(options)}"
        if question:
            cmd += f" {question}"
        results = await self.post(cmd, photo=photo, timeout=20, name="请求视觉问题解答")
        if results:
            return results.get("answer", None), results.get("by", None)
        else:
            return None, None

    async def ocr(self, photo) -> Optional[str]:
        """向机器人发送 OCR 解答请求."""
        cmd = f"/ocr {self.instance}"
        results = await self.post(cmd, photo=photo, timeout=20, name="请求验证码解答")
        if results:
            return results.get("answer", None)
        else:
            return None

    async def send_log(self, message):
        """向机器人发送日志记录请求."""
        results = await self.post(f"/log {self.instance} {message}", name="发送日志到 Telegram ")
        return bool(results)

    async def send_msg(self, message):
        """向机器人发送即时日志记录请求."""
        results = await self.post(f"/msg {self.instance} {message}", name="发送即时日志到 Telegram ")
        return bool(results)

    async def infer(self, prompt: str) -> Tuple[Optional[str], Optional[str]]:
        """向机器人发送话术推测记录请求."""
        bio = BytesIO()
        bio.write(prompt.encode("utf-8"))
        bio.seek(0)
        bio.name = "data.txt"

        results = await self.post(f"/infer {self.instance}", timeout=120, file=bio, name="发送话术推测请求")
        if results:
            return results.get("answer", None), results.get("by", None)
        else:
            return None, None
