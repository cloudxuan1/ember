#!/usr/bin/env bash
# 一次性安装 Ember 自动部署。先由人工把已审核并合并的 main 快进到本机，再运行：
#   cd /root/ember && bash scripts/install-autodeploy.sh
# 本脚本不从网络下载代码，也不重建当前健康容器；它只验证现场、记录当前健康提交并安装 cron。
set -euo pipefail

REPO_DIR="${EMBER_DIR:-/root/ember}"
SCRIPT="$REPO_DIR/scripts/autodeploy.sh"
HEALTH_URL="${EMBER_HEALTH_URL:-http://127.0.0.1:8300/health}"

for command_name in git docker crontab flock curl python3; do
  command -v "$command_name" >/dev/null 2>&1 || {
    echo "缺少命令：$command_name，自动部署尚未安装"
    exit 1
  }
done

[ -d "$REPO_DIR/.git" ] || {
  echo "找不到 Ember 仓库：$REPO_DIR"
  exit 1
}
[ -f "$SCRIPT" ] || {
  echo "找不到 $SCRIPT；请先把已审核并合并的 main 快进到 VPS"
  exit 1
}
case "$REPO_DIR" in
  *[[:space:]]*)
    echo "仓库路径不能含空格：$REPO_DIR"
    exit 1
    ;;
esac

cd "$REPO_DIR"
current_branch=$(git branch --show-current)
[ "$current_branch" = "main" ] || {
  echo "当前分支是 $current_branch，不是 main；未安装"
  exit 1
}
[ -z "$(git status --porcelain --untracked-files=no)" ] || {
  echo "VPS 工作区有未提交的受跟踪改动；未安装"
  exit 1
}
curl -fsS --max-time 5 "$HEALTH_URL" | grep -q '"status"[[:space:]]*:[[:space:]]*"ok"' || {
  echo "当前 /health 不正常；先修好现有服务，再安装自动部署"
  exit 1
}

mkdir -p data
chmod +x "$SCRIPT"
git rev-parse HEAD > data/autodeploy.good

cron_line="*/5 * * * * EMBER_DIR=$REPO_DIR $SCRIPT >/dev/null 2>&1"
cron_tmp=$(mktemp)
trap 'rm -f "$cron_tmp" "$cron_tmp.filtered"' EXIT
crontab -l 2>/dev/null > "$cron_tmp" || true
grep -vF "$SCRIPT" "$cron_tmp" > "$cron_tmp.filtered" || true
printf '%s\n' "$cron_line" >> "$cron_tmp.filtered"
crontab "$cron_tmp.filtered"

echo "自动部署已安装：每 5 分钟检查 origin/main；无更新时静默退出。"
echo "当前健康提交：$(git rev-parse --short HEAD)"
echo "查看任务：crontab -l | grep autodeploy"
echo "查看事件：tail -n 50 $REPO_DIR/data/autodeploy.log"
echo "人工重试：FORCE=1 $SCRIPT"
