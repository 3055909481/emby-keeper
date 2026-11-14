#!/usr/bin/env bash
# =============================================================================
# embykeeper 8.1.1 面板模式「单容器后台服务」补丁
#
# 作用: 让面板自己拉起的业务后端 (127.0.0.1:8001) 在提供 REST API 的同时,
#       启动签到 / 保活 / 监控等全部计划任务. 不新增容器、不新增进程、
#       不改动面板前端, 配置仍由 Web 面板管理并实时生效.
#
# 用法 (在 Docker 宿主机上执行):
#   bash apply_patch.sh                # 自动查找面板容器并打补丁
#   bash apply_patch.sh <容器名或ID>    # 明确指定容器
#   bash apply_patch.sh --rollback [容器名或ID]
#
# 脚本会: 备份原文件 -> 覆盖补丁 -> 容器内语法校验 -> commit 固化成新镜像
#         -> 重启容器 -> 等待面板恢复 -> 打印验证方式
# =============================================================================
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PATCH_FILE="$SCRIPT_DIR/panel_scheduler_init.py"
BACKUP_FILE="$SCRIPT_DIR/api_init.py.pre-panel-scheduler.bak"
BAK_MARK="api/__init__.py.pre-panel-scheduler.bak"

MODE="apply"
CONTAINER=""
for arg in "$@"; do
    case "$arg" in
        --rollback) MODE="rollback" ;;
        -h|--help)  sed -n '2,20p' "$0"; exit 0 ;;
        *)          CONTAINER="$arg" ;;
    esac
done

die() { printf '\n[错误] %s\n' "$*" >&2; exit 1; }
info() { printf '\n==> %s\n' "$*"; }

command -v docker >/dev/null 2>&1 || die "宿主机上找不到 docker 命令"
[ -f "$PATCH_FILE" ] || die "补丁文件不存在: $PATCH_FILE"

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

# --- 2. 探测补丁目标路径 (兼容不同 python 版本 / editable 安装) -------------
TARGET="$(docker exec "$CONTAINER" python -c \
  'import embykeeper, pathlib; print(pathlib.Path(embykeeper.__file__).parent / "api" / "__init__.py")' \
  2>/dev/null | tr -d '\r' | tail -1)"
[ -n "$TARGET" ] || die "无法在容器内定位 embykeeper/api/__init__.py, 请确认使用的是官方面板镜像"
docker exec "$CONTAINER" test -f "$TARGET" || die "容器内不存在 $TARGET"
info "补丁目标: $TARGET"

# --- 3. 回滚模式 -------------------------------------------------------------
if [ "$MODE" = "rollback" ]; then
    info "回滚: 恢复上游原始 api/__init__.py"
    if docker exec "$CONTAINER" test -f "$TARGET.bak"; then
        docker exec "$CONTAINER" sh -c "cp '$TARGET.bak' '$TARGET'"
    elif [ -f "$BACKUP_FILE" ]; then
        docker cp "$BACKUP_FILE" "$CONTAINER:$TARGET"
    else
        die "找不到备份文件, 无法回滚 (备份可能只存在于已删除的容器中)"
    fi
    docker restart -t 20 "$CONTAINER" >/dev/null
    echo "已回滚并重启容器 $CNAME. 需要彻底恢复时, 把镜像改回 $IMAGE 重建容器即可."
    exit 0
fi

# --- 4. 备份 -----------------------------------------------------------------
info "备份原文件"
docker exec "$CONTAINER" sh -c "cp '$TARGET' '$TARGET.bak'"
docker cp "$CONTAINER:$TARGET.bak" "$BACKUP_FILE" >/dev/null
# 同时把补丁放进 /app 持久化卷, 便于容器重建后复查 / 手工恢复
docker cp "$PATCH_FILE" "$CONTAINER:/app/panel_scheduler_init.py" >/dev/null
echo "   容器内备份: $TARGET.bak"
echo "   宿主机备份: $BACKUP_FILE"

# --- 5. 覆盖 + 容器内语法校验 ------------------------------------------------
info "写入补丁并做语法校验"
docker cp "$PATCH_FILE" "$CONTAINER:$TARGET" >/dev/null
if ! docker exec "$CONTAINER" python -c \
    'import ast, sys; ast.parse(open(sys.argv[1], encoding="utf-8").read()); print("syntax OK")' \
    "$TARGET"; then
    echo "语法校验失败, 正在自动回滚..." >&2
    docker exec "$CONTAINER" sh -c "cp '$TARGET.bak' '$TARGET'"
    die "补丁未生效, 已恢复原文件"
fi

# --- 6. 固化成新镜像 (容器重建后补丁不丢) -----------------------------------
NEW_IMAGE="$(printf '%s' "$IMAGE" | sed -E 's/:[^/:]+$//'):panel-scheduler"
info "固化补丁镜像: $NEW_IMAGE"
docker commit "$CONTAINER" "$NEW_IMAGE" >/dev/null
echo "   以后用镜像 $NEW_IMAGE 重建容器, 无需再次打补丁"

# --- 7. 重启后端进程 ---------------------------------------------------------
info "重启容器以加载补丁"
docker restart -t 20 "$CONTAINER" >/dev/null

# --- 8. 等待面板恢复 ---------------------------------------------------------
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

# --- 9. 交付验证指引 ---------------------------------------------------------
cat <<EOF

完成。现在仍然是「一个容器、一个进程」: 面板 (8000) -> 业务后端 (127.0.0.1:8001)
-> 同一进程内的计划任务调度. 面板里新增/修改账号后会自动重建调度, 无需重启.

验证:
  docker logs --tail 80 $CNAME
    应能看到: 「面板模式计划任务已启动: checkiner / emby / ...」
              「面板模式后台服务已就绪」
              「下一次 Emby 保活将在 ... 进行」/「下一次 ... 签到将在 ...」
  打开面板的「运行状态」页, 应能看到 RunContext 记录; 也可用「立即运行」手动触发一次.

想立刻跑一次 (等价命令行 -i), 给容器加环境变量 EK_PANEL_INSTANT=1 并重启.
常用开关:
  EK_PANEL_TASKS=checkiner,emby,subsonic   只跑指定模块
  EK_PANEL_NOTIFIER=1                      启用通知推送
  EK_PANEL_TASKS=none                      退回纯 API 模式

回滚:  bash $0 --rollback $CNAME
注意: 若用 docker compose / 重新 docker run 重建容器, 请把镜像改为
      $NEW_IMAGE
EOF
