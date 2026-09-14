#!/usr/bin/env bash
# ember 自动部署（跑在 VPS 上，由 cron 每 2 分钟调一次，见 scripts/install-autodeploy.sh）
#
# 流程：origin/main 有新提交 → 拍 ember.db 快照 → git 快进到新提交 → docker compose 重建
#       → 等 /health 返回 ok → 成功收工；失败则回退到部署前的提交重建，并记下这个坏提交不再重试。
# 安全边界：VPS 工作区有手改、分支分叉、不在 main 上，一律不动只记日志；数据库快照只清理本脚本自己拍的。
# 日志：data/autodeploy.log（data/ 已 gitignore）。手动强制重部署当前提交：FORCE=1 scripts/autodeploy.sh
set -euo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

REPO_DIR="${EMBER_DIR:-/root/ember}"
BRANCH="${EMBER_DEPLOY_BRANCH:-main}"
HEALTH_URL="${EMBER_HEALTH_URL:-http://127.0.0.1:8300/health}"
HEALTH_WAIT_SECONDS="${EMBER_HEALTH_WAIT:-120}"
KEEP_SNAPSHOTS=5

cd "$REPO_DIR"
mkdir -p data
LOG="$REPO_DIR/data/autodeploy.log"
SKIP_FILE="$REPO_DIR/data/autodeploy.skip"   # 上次部署失败的提交，避免每 2 分钟反复重试

log() { echo "$(date '+%F %T') $*" >> "$LOG"; }

# 上一轮还没跑完（比如 docker 还在 build）就直接退出，不排队
exec 9>"/tmp/ember-autodeploy.lock"
flock -n 9 || exit 0

wait_healthy() {
  local deadline=$(( $(date +%s) + HEALTH_WAIT_SECONDS ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    if curl -fsS --max-time 3 "$HEALTH_URL" 2>/dev/null | grep -q '"status": *"ok"'; then
      return 0
    fi
    sleep 2
  done
  return 1
}

rebuild() {
  docker compose up -d --build >> "$LOG" 2>&1
}

git fetch -q origin "$BRANCH"
local_sha=$(git rev-parse HEAD)
remote_sha=$(git rev-parse "origin/$BRANCH")

if [ "$local_sha" = "$remote_sha" ] && [ "${FORCE:-0}" != "1" ]; then
  exit 0
fi
if [ -f "$SKIP_FILE" ] && [ "$(cat "$SKIP_FILE")" = "$remote_sha" ] && [ "${FORCE:-0}" != "1" ]; then
  exit 0
fi

current_branch=$(git rev-parse --abbrev-ref HEAD)
if [ "$current_branch" != "$BRANCH" ]; then
  log "跳过：VPS 当前在分支 $current_branch，不是 $BRANCH，请人工处理"
  exit 0
fi
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  log "跳过：VPS 工作区有未提交的手改，不敢覆盖，请人工处理（git status 看一眼）"
  exit 0
fi
if ! git merge-base --is-ancestor "$local_sha" "$remote_sha"; then
  log "跳过：本地 ${local_sha:0:7} 不在 origin/$BRANCH 历史里（分叉），请人工处理"
  exit 0
fi

short=${remote_sha:0:7}
log "开始部署 ${local_sha:0:7} -> $short"

# 部署前拍数据库快照（项目红线），只保留最近 $KEEP_SNAPSHOTS 份自动快照；手动拍的 ember.db.pre-*.bak 不碰
if [ -f data/ember.db ]; then
  cp data/ember.db "data/ember.db.auto-$short.bak"
  ls -t data/ember.db.auto-*.bak 2>/dev/null | tail -n +$((KEEP_SNAPSHOTS + 1)) | xargs -r rm -f
fi

git merge -q --ff-only "origin/$BRANCH"

if rebuild && wait_healthy; then
  log "部署成功 $short，/health ok"
  rm -f "$SKIP_FILE"
  exit 0
fi

log "部署失败（build 报错或 /health ${HEALTH_WAIT_SECONDS}s 内没回 ok），回退到 ${local_sha:0:7}"
echo "$remote_sha" > "$SKIP_FILE"
git reset -q --hard "$local_sha"
if rebuild && wait_healthy; then
  log "已回退到 ${local_sha:0:7}，/health ok。坏提交 $short 已记入 autodeploy.skip，main 再有新提交才会重试"
else
  log "回退重建后 /health 仍异常！需要人工上 VPS 处理（docker compose logs ember）"
fi
exit 1
