#!/usr/bin/env bash
# =============================================================================
# embykeeper 8.1.1 面板模式「单容器后台服务 + 离线自足」补丁
#
# 一次装齐 7 个文件 (容器内路径相对 embykeeper 包目录):
#   panel_scheduler_init.py -> api/__init__.py                     API 进程内启动计划任务
#   api_config.py           -> api/config.py                       面板「AI 与验证码服务」设置端点
#   link.py                 -> telegram/link.py                    取消云端认证 / 能力本地化
#   capability.py           -> telegram/capability.py              本地模型 / 验证码服务提供者
#   capability_settings.py  -> capability_settings.py              面板配置读写 (/app/capability.json)
#   session.py              -> telegram/session.py                 世界时间检测多源容错
#   _templ_a.py             -> telegram/checkiner/_templ_a.py     templ_a 自定义站点「签到验证」自动答题
#
# 重要: capability.py 会 import capability_settings.py, 这些文件必须同时安装,
#       否则容器启动时抛 ModuleNotFoundError. 本脚本因此采用批量安装 + 整体回滚.
#
# 注意: 本脚本只替换容器内的 Python 源码; 新增的 Web 配置页属于已构建的前端
#       静态资源, 只能通过使用 CI 构建的新镜像获得 (Build Docker Image Artifact).
#
# 用法 (在 Docker 宿主机上执行, 7 个 .py 文件与脚本放同一目录):
#   bash apply_patch.sh                # 自动查找面板容器并打补丁
#   bash apply_patch.sh <容器名或ID>    # 明确指定容器
#   bash apply_patch.sh --rollback [容器名或ID]
#
# 流程: 逐个备份 -> 覆盖 -> 容器内 AST + import 校验 (失败自动整体回滚)
#       -> commit 固化成新镜像 -> 重启 -> 等待面板恢复 -> 打印验证方式
# =============================================================================
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PATCH_MAP=(
  "panel_scheduler_init.py|api/__init__.py"
  "api_config.py|api/config.py"
  "link.py|telegram/link.py"
  "capability.py|telegram/capability.py"
  "capability_settings.py|capability_settings.py"
  "session.py|telegram/session.py"
  "_templ_a.py|telegram/checkiner/_templ_a.py"
)
BACKUP_DIR="$SCRIPT_DIR/.panel-patch-backup"

MODE="apply"
CONTAINER=""
for arg in "$@"; do
    case "$arg" in
        --rollback) MODE="rollback" ;;
        -h|--help)  sed -n '2,22p' "$0"; exit 0 ;;
        *)          CONTAINER="$arg" ;;
    esac
done

die() { printf '\n[错误] %s\n' "$*" >&2; exit 1; }
info() { printf '\n==> %s\n' "$*"; }

command -v docker >/dev/null 2>&1 || die "宿主机上找不到 docker 命令"
for entry in "${PATCH_MAP[@]}"; do
    src="${entry%%|*}"
    [ -f "$SCRIPT_DIR/$src" ] || die "缺少补丁文件: $SCRIPT_DIR/$src"
done

# --- 1. 定位面板容器 ---------------------------------------------------------
if [ -z "$CONTAINER" ]; then
    info "正在查找正在运行的面板容器"
    mapfile -t CANDIDATES < <(
        docker ps --format '{{.ID}}|{{.Names}}|{{.Image}}|{{.Ports}}' \
        | grep -E '(:|,)[0-9]+->8000|8000->' \
        | grep -i 'embykeeper' || true
    )
    if [ "${#CANDIDATES[@]}" -eq 0 ]; then
        mapfile -t CANDIDATES < <(
            docker ps --format '{{.ID}}|{{.Names}}|{{.Image}}|{{.Ports}}' \
            | grep -E '8000->' || true
        )
    fi
    [ "${#CANDIDATES[@]}" -eq 1 ] || {
        echo "未能唯一确定面板容器, 请显式指定, 例如: bash $0 <容器名>"
        echo "当前运行中的容器:"
        docker ps --format '  {{.Names}}  {{.Image}}  {{.Ports}}'
        exit 1
    }
    CONTAINER="$(printf '%s' "${CANDIDATES[0]}" | cut -d'|' -f2)"
fi

docker inspect "$CONTAINER" >/dev/null 2>&1 || die "容器不存在: $CONTAINER"
CNAME="$(docker inspect -f '{{.Name}}' "$CONTAINER" | sed 's#^/##')"
IMAGE="$(docker inspect -f '{{.Config.Image}}' "$CONTAINER")"
info "目标容器: $CNAME   镜像: $IMAGE"

# --- 2. 探测包目录 (兼容不同 python 版本 / editable 安装) --------------------
PKG_DIR="$(docker exec "$CONTAINER" python -c \
  'import embykeeper, pathlib; print(pathlib.Path(embykeeper.__file__).parent)' \
  2>/dev/null | tr -d '\r' | tail -1)"
[ -n "$PKG_DIR" ] || die "无法在容器内定位 embykeeper 包目录, 请确认使用的是官方面板镜像"
docker exec "$CONTAINER" test -f "$PKG_DIR/api/__init__.py" \
  || die "容器内不存在 $PKG_DIR/api/__init__.py"
info "补丁目标包目录: $PKG_DIR"

# --- 3. 整体回滚 -------------------------------------------------------------
restore_all() {
    local entry src rel target
    for entry in "${PATCH_MAP[@]}"; do
        src="${entry%%|*}"; rel="${entry#*|}"
        target="$PKG_DIR/$rel"
        if docker exec "$CONTAINER" test -f "$target.pre-nonexistent" 2>/dev/null; then
            docker exec "$CONTAINER" sh -c "rm -f '$target' '$target.pre-nonexistent'" || true
        elif docker exec "$CONTAINER" test -f "$target.bak" 2>/dev/null; then
            docker exec "$CONTAINER" sh -c "cp '$target.bak' '$target'" || true
        elif [ -f "$BACKUP_DIR/$src.bak" ]; then
            docker cp "$BACKUP_DIR/$src.bak" "$CONTAINER:$target" >/dev/null 2>&1 || true
        fi
    done
}

if [ "$MODE" = "rollback" ]; then
    info "回滚: 恢复上游原始文件"
    restore_all
    docker restart -t 20 "$CONTAINER" >/dev/null
    echo "已回滚并重启容器 $CNAME. 需要彻底恢复时, 把镜像改回 $IMAGE 重建容器即可."
    exit 0
fi

# --- 4. 逐文件备份 -----------------------------------------------------------
info "备份原文件"
mkdir -p "$BACKUP_DIR"
for entry in "${PATCH_MAP[@]}"; do
    src="${entry%%|*}"; rel="${entry#*|}"
    target="$PKG_DIR/$rel"
    if docker exec "$CONTAINER" test -f "$target"; then
        docker exec "$CONTAINER" sh -c "cp '$target' '$target.bak'"
        docker cp "$CONTAINER:$target.bak" "$BACKUP_DIR/$src.bak" >/dev/null
        echo "   备份 $rel"
    else
        docker exec "$CONTAINER" sh -c "touch '$target.pre-nonexistent'"
        echo "   $rel 为新增文件 (原镜像不存在)"
    fi
done
echo "   宿主机备份目录: $BACKUP_DIR"

# --- 5. 覆盖全部补丁 ---------------------------------------------------------
info "写入 7 个补丁文件"
for entry in "${PATCH_MAP[@]}"; do
    src="${entry%%|*}"; rel="${entry#*|}"
    docker cp "$SCRIPT_DIR/$src" "$CONTAINER:$PKG_DIR/$rel" >/dev/null
    echo "   $src -> $rel"
done

# --- 6. 容器内校验 (AST + 真实 import, 失败整体回滚) ------------------------
info "容器内语法与依赖校验"
if ! docker exec -i "$CONTAINER" python - "$PKG_DIR" "${PATCH_MAP[@]##*|}" <<'PY'
import ast
import pathlib
import sys

base = pathlib.Path(sys.argv[1])
for rel in sys.argv[2:]:
    ast.parse((base / rel).read_text(encoding="utf-8"))
print(f"  AST OK ({len(sys.argv) - 2} files)")
PY
then
    echo "语法校验失败, 正在整体回滚..." >&2
    restore_all
    die "补丁未生效, 已恢复全部原文件"
fi

if ! docker exec "$CONTAINER" python -c \
    'import embykeeper.capability_settings, embykeeper.telegram.link, embykeeper.telegram.capability, embykeeper.telegram.checkiner._templ_a; print("  import OK")'; then
    echo "导入校验失败, 正在整体回滚..." >&2
    restore_all
    die "补丁未生效, 已恢复全部原文件"
fi

# --- 7. 固化成新镜像 (容器重建后补丁不丢) -----------------------------------
NEW_IMAGE="$(printf '%s' "$IMAGE" | sed -E 's/:[^/:]+$//'):panel-scheduler"
info "固化补丁镜像: $NEW_IMAGE"
docker commit "$CONTAINER" "$NEW_IMAGE" >/dev/null
echo "   以后用镜像 $NEW_IMAGE 重建容器, 无需再次打补丁"

# --- 8. 重启后端进程 ---------------------------------------------------------
info "重启容器以加载补丁"
docker restart -t 20 "$CONTAINER" >/dev/null

# --- 9. 等待面板恢复 ---------------------------------------------------------
PORT="$(docker inspect -f '{{range $p, $conf := .NetworkSettings.Ports}}{{if eq $p "8000/tcp"}}{{range $conf}}{{.HostPort}}{{end}}{{end}}{{end}}' "$CONTAINER" 2>/dev/null || true)"
PORT="${PORT:-8000}"
if command -v curl >/dev/null 2>&1; then
    for _ in $(seq 1 20); do
        if curl -fsS -m 2 "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1 \
           || curl -fsS -m 2 "http://127.0.0.1:$PORT/" >/dev/null 2>&1; then
            echo "   面板已恢复: http://127.0.0.1:$PORT"
            break
        fi
        sleep 1
    done
fi

# --- 10. 交付验证指引 --------------------------------------------------------
cat <<EOF

完成。仍是「一个容器、一个进程」: 面板 (8000) -> 业务后端 (127.0.0.1:8001)
-> 同一进程内的计划任务调度. 面板里新增/修改账号后会自动重建调度, 无需重启.

本次已装好后端设置端点 /api/settings/capability, 可用 API 直接配置 LLM / solver;
侧边栏的「AI 与验证码服务」页面需要换成 CI 构建的新镜像才会出现.

本次补丁同时取消了 @embykeeper_auth_bot 的授权门禁:
  默认视作高级用户, 进程不再向该 Bot 发任何消息, 签到/监控/水群不会再被认证卡住.
  需要云端算力的能力改由本地/自建服务提供 (不配置则对应站点快速跳过):
    EK_LLM_BASE_URL / EK_LLM_API_KEY / EK_LLM_MODEL        智能回答、题库、话术
    EK_LLM_VISION_MODEL                                     图片视觉问答
    EK_CAPTCHA_API_URL / EK_CAPTCHA_API_KEY                  Turnstile 验证码求解
  OCR 已使用镜像自带的 ddddocr, 无需额外配置.
  若 Auth Bot 恢复且想回到上游严格认证: EK_LINK_STRICT_AUTH=1

本次补丁还让 templ_a 自定义站点能自动回答「签到验证」选择题:
  纯算术题 (例如 "6 − 5 = ?") 直接本地计算并点击对应按钮, 不消耗模型额度;
  其他题目走已配置的智能模型, 答完继续等待签到结果 (不再答完即判失败).
  面板「自定义站点」里可调: templ_quiz_enabled / templ_quiz_keywords /
  templ_quiz_local_math / templ_quiz_use_gpt.

验证:
  docker logs --tail 80 $CNAME
    应能看到: 「面板模式计划任务已启动: checkiner / emby / ...」
              「面板模式后台服务已就绪」
              「下一次 Emby 保活将在 ... 进行」/「下一次 ... 签到将在 ...」
  不再出现「服务 MONITOR 认证超时」「检测世界时间发生错误」.
  打开面板「运行状态」页可看到 RunContext 记录; 也可用「立即运行」手动触发.

常用开关:
  EK_PANEL_INSTANT=1                       立刻跑一次 (等价命令行 -i)
  EK_PANEL_TASKS=checkiner,emby,subsonic  只跑指定模块
  EK_PANEL_TASKS=...,monitor,messager     需要群组监控/自动水群时追加
  EK_PANEL_NOTIFIER=1                     启用通知推送
  EK_PANEL_TASKS=none                     退回纯 API 模式

回滚:  bash $0 --rollback $CNAME
注意: 若用 docker compose / 重新 docker run 重建容器, 请把镜像改为
      $NEW_IMAGE
EOF
