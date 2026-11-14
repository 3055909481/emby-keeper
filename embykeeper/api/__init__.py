"""Embykeeper API module.

=============================================================================
panel-scheduler patch (for embykeeper 8.1.1)
=============================================================================
上游 ``embykeeper/cli.py`` 的 ``--api`` 分支是::

    server = uvicorn.Server(config_obj)
    await server.serve()
    return            # <-- 直接返回, 跳过了后面全部 schedule_all() 调度逻辑

因此面板模式拉起的业务后端只提供 REST 接口, 永远不会自己签到 / 保活.
镜像 Dockerfile 的注释明确写着 "单容器提供 界面 + API + 计划任务", 属于实现回归.

本文件只增强 ``_lifespan``: 在 API 服务器启动时, 复用与命令行模式完全相同的
Manager 拉起全部计划任务; 在服务器关闭时一并取消. 其余代码保持上游原样.

串联链路 (全部是上游已有能力, 无需额外进程):
    面板保存账号 / 计划设置
      -> embykeeper/api/config.py::_write_config_dict() 写 config.toml
      -> loop.create_task(config.reload_conf(...)) / watchfiles 监视器
      -> ConfigManager._process_changes() 触发 on_change / on_list_change
      -> 各 Manager 自动 reconcile 调度器 (新增/删除账号即时生效)
      -> 任务运行状态写入 RunContext, 面板「运行状态」页与日志可直接查看

可选环境变量 (都是可选项, 不配置即使用安全默认值):
    EK_PANEL_TASKS    逗号分隔, 默认 "checkiner,emby,subsonic"
                      可加 registrar; 设为 "none" 则退化为纯 API 模式
    EK_PANEL_INSTANT  1/true/yes 时后端启动后立刻跑一次签到与保活 (等价 CLI -i)
    EK_PANEL_NOTIFIER 1/true/yes 时同时启用通知推送 (需要可用的 Telegram 账号)
"""

import asyncio
import importlib
import os
import sys
from contextlib import asynccontextmanager
from typing import List

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger

from .. import __version__


_DEFAULT_TASKS = "checkiner,emby,subsonic"

# (名称, 相对模块, 管理器类, 常驻入口方法) —— 与 cli.py 中的顺序/方法保持一致
_TASK_SPECS = (
    ("checkiner", "..telegram.checkin_main", "CheckinerManager", "schedule_all"),
    ("monitor", "..telegram.monitor_main", "MonitorManager", "run_all"),
    ("messager", "..telegram.message_main", "MessageManager", "run_all"),
    ("emby", "..emby.main", "EmbyManager", "schedule_all"),
    ("subsonic", "..subsonic.main", "SubsonicManager", "schedule_all"),
    ("registrar", "..telegram.registrar_main", "RegisterManager", "start"),
)


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


async def _ensure_config_observer():
    """确保 config.toml 的文件监视器在运行 (正常情况下 cli.py 已启动它)."""
    from ..config import config

    if not getattr(config, "_conf_file", None):
        return
    observer = getattr(config, "_observer", None)
    if observer is None or observer.done():
        try:
            await config.start_observer()
            logger.info("配置文件监视器已启动, 面板中的配置变更将实时应用到计划任务.")
        except Exception as exc:
            logger.warning(f"配置文件监视器启动失败, 配置变更可能不会即时生效: {exc}")


async def _start_schedulers():
    """在 API 进程内启动与命令行模式一致的常驻计划任务."""
    await _ensure_config_observer()

    wanted = {
        item.strip()
        for item in os.environ.get("EK_PANEL_TASKS", _DEFAULT_TASKS).split(",")
        if item.strip()
    }
    if "none" in wanted:
        wanted.clear()

    managers, tasks, by_name = [], [], {}
    for name, module, cls_name, method in _TASK_SPECS:
        if name not in wanted:
            continue
        try:
            manager = getattr(importlib.import_module(module, __package__), cls_name)()
            task = asyncio.create_task(getattr(manager, method)(), name=f"panel-{name}")
            managers.append(manager)
            tasks.append(task)
            by_name[name] = manager
            logger.info(f"面板模式计划任务已启动: {name}")
        except Exception as exc:
            logger.warning(f"计划任务 {name} 启动失败, 已跳过 (API 服务不受影响): {exc}")

    if tasks:
        logger.info(
            "面板模式后台服务已就绪: 面板中配置的 Telegram / Emby 账号将自动执行签到与保活."
        )
    else:
        logger.warning("面板模式未启用任何计划任务, 当前仅提供 REST API.")

    if _env_flag("EK_PANEL_INSTANT") and by_name:
        for name in ("checkiner", "emby", "subsonic"):
            manager = by_name.get(name)
            if manager is None:
                continue
            try:
                asyncio.create_task(
                    manager.run_all(instant=True), name=f"panel-instant-{name}"
                )
                logger.info(f"启动后立即执行一次: {name}")
            except Exception as exc:
                logger.warning(f"立即执行 {name} 失败: {exc}")

    return managers, tasks


async def _start_notifier():
    """可选: 在后台启用通知推送, 不让 Telegram 连接阻塞 API 启动."""
    from ..notify import start_notifier

    streams = await start_notifier()
    if streams:
        await asyncio.gather(*(stream.join() for stream in streams))


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    # Suppress noisy ConnectionResetError on Windows when SSE clients disconnect.
    if sys.platform == "win32":
        loop = asyncio.get_running_loop()
        _original_handler = loop.get_exception_handler()

        def _suppress_connection_reset(loop, context):
            exc = context.get("exception")
            if isinstance(exc, ConnectionResetError):
                return
            if _original_handler:
                _original_handler(loop, context)
            else:
                loop.default_exception_handler(context)

        loop.set_exception_handler(_suppress_connection_reset)

    managers, tasks, extra_tasks = [], [], []
    try:
        managers, tasks = await _start_schedulers()
    except Exception as exc:
        logger.exception(f"计划任务调度启动失败, API 服务仍将正常提供: {exc}")

    if _env_flag("EK_PANEL_NOTIFIER"):
        extra_tasks.append(asyncio.create_task(_start_notifier(), name="panel-notifier"))

    try:
        yield
    finally:
        for task in tasks + extra_tasks:
            task.cancel()
        pending = tasks + extra_tasks
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        for manager in managers:
            try:
                manager.stop_all()
            except Exception:
                pass
        logger.info("面板模式后台服务已停止.")


def create_app(allow_origins: List[str] = None) -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(title="Embykeeper API", version=__version__, lifespan=_lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=allow_origins
        or [
            "http://localhost:3000",
            "http://127.0.0.1:3000",
            "http://localhost:5173",
            "http://127.0.0.1:5173",
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    from .config import router as config_router
    from .runinfo import router as runinfo_router
    from .auth import router as auth_router
    from .system import router as system_router
    from .telegram import router as telegram_router
    from .emby import router as emby_router
    from .subsonic import router as subsonic_router
    from .sites import router as sites_router
    from .ops import router as ops_router

    app.include_router(config_router, prefix="/api")
    app.include_router(runinfo_router, prefix="/api")
    app.include_router(auth_router, prefix="/api")
    app.include_router(system_router, prefix="/api")
    app.include_router(telegram_router, prefix="/api")
    app.include_router(emby_router, prefix="/api")
    app.include_router(subsonic_router, prefix="/api")
    app.include_router(sites_router, prefix="/api")
    app.include_router(ops_router, prefix="/api")

    @app.get("/")
    async def root():
        return {"message": "Embykeeper API", "version": __version__}

    return app
