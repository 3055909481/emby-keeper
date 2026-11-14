#!/usr/bin/env bash
# 只读诊断: 不修改任何容器、配置或数据
# 用法: sudo bash diagnose.sh [面板容器名, 默认 embykeeper-panel]
set -u
PANEL="${1:-embykeeper-panel}"

section() { printf '\n=== %s ===\n' "$1"; }

section "1. 容器状态"
docker ps -a --filter "name=${PANEL}" --format '{{.Names}} | {{.Status}} | {{.Image}}'
if ! docker inspect -f '{{.State.Running}}' "$PANEL" 2>/dev/null | grep -q true; then
  echo "!! 容器未在运行"
fi

section "2. /app 数据目录挂载"
SRC=$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/app"}}{{.Source}}{{end}}{{end}}' "$PANEL" 2>/dev/null)
echo "宿主机路径: ${SRC:-<未挂载 /app, 容器重建会丢数据!>}"

section "3. 环境变量名 (值已隐藏)"
docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$PANEL" 2>/dev/null \
  | sed -E 's/^(EK_[A-Z_]+)=.*/\1=<hidden>/' \
  | grep -E '^(EK_|EMBYKEEPERAPI|HOME|TZ)' || true

section "4. 配置文件 / 账号 / 会话"
if [ -n "${SRC:-}" ] && [ -d "$SRC" ]; then
  CFG="$SRC/config.toml"
  if [ -f "$CFG" ]; then
    echo "config.toml: $(wc -c < "$CFG") 字节"
    for item in telegram emby subsonic; do
      printf '%-10s 账号条目: %s\n' "$item" "$(grep -c "^\[\[${item}\.account\]\]" "$CFG" || true)"
    done
  else
    echo "!! 没有 config.toml (面板首次启动只生成空文件)"
  fi
  echo "*.session 文件数: $(find "$SRC" -maxdepth 1 -name '*.session' | wc -l)"
else
  echo "无法读取宿主机数据目录, 请用 sudo 重跑"
fi

section "5. 面板 HTTP 状态"
curl -s -m 5 -o /dev/null -w 'healthz            : HTTP %{http_code}\n' http://127.0.0.1:8000/healthz || true
curl -s -m 5 -o /dev/null -w 'api/panel/health   : HTTP %{http_code} (未登录 401 为正常)\n' http://127.0.0.1:8000/api/panel/health || true

section "6. 容器最近日志"
docker logs --tail 30 "$PANEL" 2>&1 | tail -30

section "7. 判读要点"
echo "* 日志停在 '启动 API 服务器模式, 监听 127.0.0.1:8001': 业务后端是 --api 模式, 只提供 REST 接口, 不会启动任何计划任务."
echo "* 账号条目全为 0: 尚未在面板添加账号, 即使调度器存在也没有任务可执行."
echo "* 未挂载 /app 或密码变量名不符: 配置无法持久化 / 面板 API 不可用."
echo "* 修复: 执行 fix-runner.sh (保留现有面板, 新增一个命令行常驻容器)."
