#!/usr/bin/env bash
# 一次性安装 ember 自动部署（在 VPS 上以 root 跑一遍即可，重复跑也安全）：
#   curl -fsSL https://raw.githubusercontent.com/cloudxuan1/ember/main/scripts/install-autodeploy.sh | bash
# 做的事：把仓库快进到 main → 装 cron（每 2 分钟跑 scripts/autodeploy.sh）→ 立刻强制部署一次当作验收。
set -euo pipefail

REPO_DIR="${EMBER_DIR:-/root/ember}"
SCRIPT="$REPO_DIR/scripts/autodeploy.sh"

for cmd in git docker crontab flock curl; do
  command -v "$cmd" >/dev/null 2>&1 || { echo "缺少命令：$cmd，先装上再来"; exit 1; }
done
[ -d "$REPO_DIR/.git" ] || { echo "找不到仓库 $REPO_DIR（可用 EMBER_DIR=/路径 指定）"; exit 1; }

cd "$REPO_DIR"
git fetch -q origin main
git merge -q --ff-only origin/main
[ -f "$SCRIPT" ] || { echo "main 上还没有 scripts/autodeploy.sh，是不是 PR 还没合？"; exit 1; }
chmod +x "$SCRIPT"

CRON_LINE="*/2 * * * * EMBER_DIR=$REPO_DIR $SCRIPT >> $REPO_DIR/data/autodeploy.cron.log 2>&1"
( crontab -l 2>/dev/null | grep -vF "$SCRIPT" || true; echo "$CRON_LINE" ) | crontab -
echo "cron 已装好：每 2 分钟检查一次 origin/main。"

echo "现在强制部署一次当作验收（会重建容器，几十秒）..."
if FORCE=1 EMBER_DIR="$REPO_DIR" "$SCRIPT"; then
  echo "验收通过。日志看 $REPO_DIR/data/autodeploy.log："
else
  echo "验收失败，看日志排查："
fi
tail -n 5 "$REPO_DIR/data/autodeploy.log"
