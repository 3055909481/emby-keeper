import asyncio
import random
import re
from typing import List, Optional, Union

from loguru import logger
from pydantic import BaseModel, ValidationError
from pyrogram.types import InlineKeyboardMarkup, Message
from pyrogram.errors import MessageIdInvalid
from pyrogram.raw.types.messages import BotCallbackAnswer

from embykeeper.utils import to_iterable

from . import BotCheckin
from ._base import default_keywords

__ignore__ = True

# 「签到验证」类选择题的默认识别关键词, 必须同时带有 inline 按钮才会被判定为验证题.
DEFAULT_QUIZ_KEYWORDS = (
    "签到验证",
    "请选择正确答案",
    "请选择正确",
    "人机验证",
    "答对后完成签到",
    "只能选择一次",
    "计算出",
    "题目",
)

# 兼容 Unicode 数学符号 (U+2212 减号 / 全角) 以及 = ? 之间的空白.
_QUIZ_MATH_RE = re.compile(
    r"(-?\d+(?:\.\d+)?)\s*([+\-−–—﹣－＋×✕✖xX*÷/])\s*(-?\d+(?:\.\d+)?)\s*[=＝]\s*[?？]"
)

_OPS = {
    "+": "add",
    "＋": "add",
    "-": "sub",
    "−": "sub",
    "–": "sub",
    "—": "sub",
    "﹣": "sub",
    "－": "sub",
    "*": "mul",
    "×": "mul",
    "✕": "mul",
    "✖": "mul",
    "x": "mul",
    "X": "mul",
    "/": "div",
    "÷": "div",
}


def _solve_math(text: str) -> Optional[str]:
    """从题面解析 a op b = ? 并以字符串返回答案, 无法解析或结果异常时返回 None."""
    match = _QUIZ_MATH_RE.search(text)
    if not match:
        return None
    op = _OPS.get(match.group(2))
    if not op:
        return None
    try:
        a, b = float(match.group(1)), float(match.group(3))
    except ValueError:
        return None
    if abs(a) > 1e6 or abs(b) > 1e6:
        return None
    if op == "add":
        value = a + b
    elif op == "sub":
        value = a - b
    elif op == "mul":
        value = a * b
    else:
        if b == 0:
            return None
        value = a / b
    if abs(value) > 1e9:
        return None
    if value == int(value):
        return str(int(value))
    return f"{value:.6f}".rstrip("0").rstrip(".")


def _match_button(answer: str, buttons: List[str]) -> Optional[str]:
    """把答案匹配到按钮文字, 支持 "A. 1" 这类带前缀的选项."""
    for button in buttons:
        if button and button.strip() == answer:
            return button
    for button in buttons:
        if not button:
            continue
        numbers = re.findall(r"-?\d+(?:\.\d+)?", button)
        if len(numbers) == 1 and numbers[0] == answer:
            return button
    return None


def _looks_like_quiz_prompt(text: str) -> bool:
    """判断点击签到按钮后的 callback answer 是「去做验证」的中间提示, 而非签到结果.

    基类把「完成」也列为成功关键词, 像「请先完成签到验证」这样的 toast 会被
    误判为签到成功并提前结束, 导致随后发来的验证题消息再也收不到.
    """
    hints = ("验证", "请选择", "请先", "答对", "答题", "题目")
    results = ("成功", "获得", "猫币", "积分", "已签到", "签到日期", "已完成签到")
    return any(h in text for h in hints) and not any(r in text for r in results)


class TemplateACheckinConfig(BaseModel):
    # fmt: off
    name: Optional[str] = None  # 签到器的名称
    use_button_answer: bool = None  # 点击按钮后等待并识别响应
    bot_text_ignore_answer: Union[str, List[str]] = None  # 忽略的响应文本
    bot_fail_keywords: Union[str, List[str]] = None  # 签到错误将重试时检测的关键词 (暂不支持regex), 置空使用内置关键词表
    bot_success_keywords: Union[str, List[str]] = None  # 成功时检测的关键词 (暂不支持regex), 置空使用内置关键词表
    bot_success_pat: Optional[str] = None  # 当接收到成功消息后, 从消息中提取数字的模式
    bot_captcha_len: Optional[int] = None  # 验证码长度的可能范围
    bot_text_ignore: Union[str, List[str]] = None  # 当含有列表中的关键词, 即忽略该消息, 置空不限制
    bot_checkin_caption_pat: Optional[str] = None  # 当 Bot 返回图片时, 仅当符合该 regex 才识别为验证码, 置空不限制
    bot_checkin_cmd: Optional[str] = None  # Bot 依次执行的签到命令
    bot_use_captcha: Optional[bool] = None  # 当 Bot 返回图片时, 识别验证码并调用 on_captcha
    bot_checkin_button: Union[str, List[str]] = None  # 签到按钮文本
    templ_panel_keywords: Union[str, List[str]] = None  # 面板关键词
    templ_quiz_enabled: Optional[bool] = None  # 自动回答「签到验证」选择题, 置空默认开启
    templ_quiz_keywords: Union[str, List[str]] = None  # 验证题识别关键词, 置空使用内置关键词表
    templ_quiz_local_math: Optional[bool] = None  # 纯算术题本地计算, 不消耗模型额度, 置空默认开启
    templ_quiz_use_gpt: Optional[bool] = None  # 本地算不出时调用智能模型, 置空默认开启
    # fmt: on


class TemplateACheckin(BotCheckin):
    bot_text_ignore_answer = ["Done"]
    use_button_answer = True
    bot_checkin_cmd = "/start"
    bot_checkin_button = ["签到", "簽到"]
    templ_panel_keywords = None
    templ_quiz_enabled = True
    templ_quiz_keywords = None
    templ_quiz_local_math = True
    templ_quiz_use_gpt = True

    async def init(self):
        try:
            self.t_config = TemplateACheckinConfig.model_validate(self.config)
        except ValidationError as e:
            self.log.warning(f"初始化失败: 签到自定义模板 A 的配置错误:\n{e}")
            return False

        self.name = self.t_config.name or self.name
        self.use_button_answer = (
            self.t_config.use_button_answer
            if self.t_config.use_button_answer is not None
            else self.use_button_answer
        )
        self.bot_text_ignore_answer = self.t_config.bot_text_ignore_answer or self.bot_text_ignore_answer
        self.bot_fail_keywords = self.t_config.bot_fail_keywords or self.bot_fail_keywords
        self.bot_success_keywords = self.t_config.bot_success_keywords or self.bot_success_keywords
        self.bot_success_pat = self.t_config.bot_success_pat or self.bot_success_pat
        self.bot_captcha_len = self.t_config.bot_captcha_len or self.bot_captcha_len
        self.bot_text_ignore = self.t_config.bot_text_ignore or self.bot_text_ignore
        self.bot_checkin_caption_pat = self.t_config.bot_checkin_caption_pat or self.bot_checkin_caption_pat
        self.bot_checkin_cmd = self.t_config.bot_checkin_cmd or self.bot_checkin_cmd
        self.bot_checkin_button = self.t_config.bot_checkin_button or self.bot_checkin_button
        self.templ_panel_keywords = self.t_config.templ_panel_keywords or self.templ_panel_keywords
        self.bot_use_captcha = (
            self.t_config.bot_use_captcha
            if self.t_config.bot_use_captcha is not None
            else self.bot_use_captcha
        )
        self.templ_quiz_enabled = (
            self.t_config.templ_quiz_enabled
            if self.t_config.templ_quiz_enabled is not None
            else self.templ_quiz_enabled
        )
        self.templ_quiz_keywords = self.t_config.templ_quiz_keywords or self.templ_quiz_keywords
        self.templ_quiz_local_math = (
            self.t_config.templ_quiz_local_math
            if self.t_config.templ_quiz_local_math is not None
            else self.templ_quiz_local_math
        )
        self.templ_quiz_use_gpt = (
            self.t_config.templ_quiz_use_gpt
            if self.t_config.templ_quiz_use_gpt is not None
            else self.templ_quiz_use_gpt
        )
        self._quiz_handled = set()

        self.log = logger.bind(scheme="telechecker", name=self.name, username=self.client.me.full_name)
        return True

    async def _handle_quiz(self, message: Message) -> bool:
        """处理「签到验证」选择题, 返回 True 表示该消息已被消费, 不再交给基类."""
        text = message.text or message.caption
        if not text:
            return False
        markup = getattr(message, "reply_markup", None)
        if not isinstance(markup, InlineKeyboardMarkup) or not markup.inline_keyboard:
            return False
        keywords = to_iterable(self.templ_quiz_keywords) or DEFAULT_QUIZ_KEYWORDS
        if not any(keyword in text for keyword in keywords):
            return False
        # 「完成」等成功关键词也会出现在题面里 (例如「答对后完成签到」),
        # 因此仅在不含问号时才把消息视为机器人给出的结果.
        result_keywords = to_iterable(self.bot_success_keywords) or default_keywords["success"]
        if not any(mark in text for mark in ("?", "？")) and any(k in text for k in result_keywords):
            return False

        handled = getattr(self, "_quiz_handled", None)
        if handled is None:
            handled = self._quiz_handled = set()
        key = (getattr(message, "id", None), text)
        if key in handled:
            return True
        handled.add(key)
        if len(handled) > 128:
            handled.clear()
            handled.add(key)

        buttons = [k.text for r in markup.inline_keyboard for k in r if k and k.text]
        if not buttons:
            return False

        answer = _solve_math(text) if self.templ_quiz_local_math else None
        if answer:
            button = _match_button(answer, buttons)
            if button:
                self.log.info(f'签到验证: 本地计算答案 "{answer}", 点击按钮 "{button}".')
                try:
                    await message.click(button)
                except (TimeoutError, MessageIdInvalid):
                    pass
                return True
            self.log.debug(f'签到验证: 本地计算答案 "{answer}" 未在按钮中找到对应选项.')

        if self.templ_quiz_use_gpt:
            self.log.info("签到验证: 正在使用智能接口回答问题.")
            if not await self.gpt_handle_message(message, unexpected=False):
                self.log.warning("签到验证: 智能回答未成功, 签到器将继续等待机器人响应.")
            return True

        self.log.warning("签到验证: 未启用智能回答, 该题目将被跳过.")
        return True

    async def message_handler(self, client, message: Message):
        if self.templ_quiz_enabled and await self._handle_quiz(message):
            return
        text = message.caption or message.text
        # 首条响应可能是无 caption 的图片 (面板按钮才是关键), 不要求必须有文本;
        # templ_panel_keywords 分支仍需要文本才能匹配.
        if message.reply_markup and (
            (
                text
                and self.templ_panel_keywords
                and any(keyword in text for keyword in to_iterable(self.templ_panel_keywords))
            )
            or (getattr(message, "is_first_response", False) and not message.edit_date)
        ):
            keys = [k.text for r in message.reply_markup.inline_keyboard for k in r]
            for k in keys:
                if any(btn in k for btn in self.bot_checkin_button):
                    await asyncio.sleep(random.uniform(0.5, 1.5))
                    try:
                        answer: BotCallbackAnswer = await message.click(k)
                    except TimeoutError:
                        self.log.debug(f"点击签到按钮无响应, 可能按钮未正确处理点击回复. 一般来说不影响签到.")
                    except MessageIdInvalid:
                        pass
                    else:
                        await self.on_button_answer(answer)
                    return
            else:
                self.log.warning(f"签到失败: 账户错误.")
                return await self.fail()

        if message.text and "请先点击下面加入我们的" in message.text:
            self.log.warning(f"签到失败: 账户错误.")
            return await self.fail()

        await super().message_handler(client, message)

    async def on_text(self, message: Message, text: str):
        # 基类把「只能」视作「今日已签到」, 而验证题常写「每题只能选择一次」;
        # 这类题目已由 _handle_quiz 消费, 这里再兜住 callback answer 构造的伪消息.
        if (
            self.templ_quiz_enabled
            and text
            and any(k in text for k in ("只能选择一次", "请选择正确答案", "答对后完成签到"))
        ):
            return
        await super().on_text(message, text)

    async def on_button_answer(self, answer: BotCallbackAnswer):
        if self.use_button_answer:
            if not isinstance(answer, BotCallbackAnswer):
                self.log.warning(f"签到失败: 签到按钮指向 URL, 不受支持.")
                return await self.fail()
            if answer.message and not any(ignore in answer.message for ignore in self.bot_text_ignore_answer):
                if self.templ_quiz_enabled and _looks_like_quiz_prompt(answer.message):
                    self.log.debug(f"忽略签到按钮的中间提示: {answer.message}")
                    return
                await self.on_text(Message(id=0, text=answer.message), answer.message)


def use(**kw):
    return type("TemplatedClass", (TemplateACheckin,), kw)
