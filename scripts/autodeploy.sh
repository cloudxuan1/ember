#!/usr/bin/env bash
# Ember 自动部署：由 VPS cron 每 5 分钟检查一次 origin/main。
#
# 没有新提交时静默退出；纯文档/测试/脚本更新只快进代码；只有后端或
# docker-compose.yml 变化才拍 SQLite 一致性快照、重建容器并检查 /health。
# 部署失败会回退程序到上一个健康提交，但不会自动覆盖数据库，避免丢失部署期间的新写入。
# 手动重试当前提交：FORCE=1 /root/ember/scripts/autodeploy.sh
set -euo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export GIT_TERMINAL_PROMPT=0

REPO_DIR="${EMBER_DIR:-/root/ember}"
BRANCH="${EMBER_DEPLOY_BRANCH:-main}"
HEALTH_URL="${EMBER_HEALTH_URL:-http://127.0.0.1:8300/health}"
HEALTH_WAIT_SECONDS="${EMBER_HEALTH_WAIT:-120}"
KEEP_SNAPSHOTS="${EMBER_KEEP_SNAPSHOTS:-5}"
LOG_MAX_BYTES="${EMBER_LOG_MAX_BYTES:-1048576}"
KEEP_OLD_LOGS="${EMBER_KEEP_OLD_LOGS:-3}"

cd "$REPO_DIR"
mkdir -p data

LOG="$REPO_DIR/data/autodeploy.log"
SKIP_FILE="$REPO_DIR/data/autodeploy.skip"
GOOD_FILE="$REPO_DIR/data/autodeploy.good"
LOCK_FILE="$REPO_DIR/data/autodeploy.lock"

log() {
  printf '%s %s\n' "$(date '+%F %T')" "$*" >> "$LOG"
}

rotate_log() {
  [ -f "$LOG" ] || return 0
  local size
  size=$(wc -c < "$LOG")
  [ "$size" -lt "$LOG_MAX_BYTES" ] || {
    local index
    rm -f "$LOG.$KEEP_OLD_LOGS"
    for ((index = KEEP_OLD_LOGS - 1; index >= 1; index--)); do
      [ -f "$LOG.$index" ] && mv "$LOG.$index" "$LOG.$((index + 1))"
    done
    mv "$LOG" "$LOG.1"
  }
}

wait_healthy() {
  local deadline=$(( $(date +%s) + HEALTH_WAIT_SECONDS ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    if curl -fsS --max-time 3 "$HEALTH_URL" 2>/dev/null | grep -q '"status"[[:space:]]*:[[:space:]]*"ok"'; then
      return 0
    fi
    sleep 2
  done
  return 1
}

rebuild() {
  docker compose up -d --build >> "$LOG" 2>&1
}

prune_snapshots() {
  local snapshot_count
  snapshot_count=$(find data -maxdepth 1 -type f -name 'ember.db.auto-*.bak' | wc -l)
  [ "$snapshot_count" -le "$KEEP_SNAPSHOTS" ] || {
    find data -maxdepth 1 -type f -name 'ember.db.auto-*.bak' -printf '%T@ %p\n' \
      | sort -rn \
      | tail -n +$((KEEP_SNAPSHOTS + 1)) \
      | cut -d' ' -f2- \
      | xargs -r rm -f --
  }
}

# 上一轮仍在 build 时直接退出，不排队。
exec 9>"$LOCK_FILE"
flock -n 9 || exit 0
rotate_log

current_branch=$(git branch --show-current)
if [ "$current_branch" != "$BRANCH" ]; then
  log "跳过：VPS 当前在分支 $current_branch，不是 $BRANCH，请人工处理"
  exit 0
fi
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  log "跳过：VPS 工作区有未提交的受跟踪改动，不敢覆盖，请人工处理"
  exit 0
fi
if ! git fetch -q origin "$BRANCH"; then
  log "拉取 origin/$BRANCH 失败，保留当前版本"
  exit 1
fi

local_sha=$(git rev-parse HEAD)
remote_sha=$(git rev-parse "origin/$BRANCH")
force="${FORCE:-0}"

if [ "$local_sha" = "$remote_sha" ] && [ "$force" != "1" ]; then
  exit 0
fi
if [ -f "$SKIP_FILE" ] && [ "$(cat "$SKIP_FILE")" = "$remote_sha" ] && [ "$force" != "1" ]; then
  exit 0
fi
if ! git merge-base --is-ancestor "$local_sha" "$remote_sha"; then
  log "跳过：本地 ${local_sha:0:7} 与 origin/$BRANCH 分叉，请人工处理"
  exit 0
fi

changed_files=$(git diff --name-only "$local_sha" "$remote_sha")
needs_rebuild=0
if [ "$force" = "1" ]; then
  needs_rebuild=1
else
  while IFS= read -r changed_file; do
    case "$changed_file" in
      backend/*|docker-compose.yml)
        needs_rebuild=1
        break
        ;;
    esac
  done <<< "$changed_files"
fi

short=${remote_sha:0:7}

if [ "$needs_rebuild" = "0" ]; then
  if git merge -q --ff-only "origin/$BRANCH"; then
    printf '%s\n' "$remote_sha" > "$GOOD_FILE"
    rm -f "$SKIP_FILE"
    log "已同步 $short；未涉及后端或容器配置，无需重建"
    exit 0
  fi
  log "同步 $short 失败，保留当前版本"
  exit 1
fi

log "开始部署 ${local_sha:0:7} -> $short"

snapshot=""
if [ -f data/ember.db ]; then
  snapshot="data/ember.db.auto-$(date '+%Y%m%d-%H%M%S')-$short-$$.bak"
  if ! python3 scripts/sqlite_backup.py data/ember.db "$snapshot" >> "$LOG" 2>&1; then
    log "停止部署：SQLite 一致性快照失败，代码和容器均未改动"
    exit 1
  fi
  prune_snapshots
fi

if ! git merge -q --ff-only "origin/$BRANCH"; then
  log "同步 $short 失败，代码未部署；数据库快照保留在 ${snapshot:-未生成}"
  exit 1
fi

if rebuild && wait_healthy; then
  printf '%s\n' "$remote_sha" > "$GOOD_FILE"
  rm -f "$SKIP_FILE"
  log "部署成功 $short，/health ok"
  exit 0
fi

rollback_sha="$local_sha"
if [ -s "$GOOD_FILE" ]; then
  candidate=$(cat "$GOOD_FILE")
  if git cat-file -e "$candidate^{commit}" 2>/dev/null; then
    rollback_sha="$candidate"
  fi
fi

log "部署失败：build 报错或 /health ${HEALTH_WAIT_SECONDS}s 内未恢复；程序回退到 ${rollback_sha:0:7}，数据库不自动覆盖"
printf '%s\n' "$remote_sha" > "$SKIP_FILE"
git reset -q --hard "$rollback_sha"

if rebuild && wait_healthy; then
  log "程序已回退到 ${rollback_sha:0:7}，/health ok；坏提交 $short 暂停重试；快照保留在 ${snapshot:-未生成}"
else
  log "程序回退后 /health 仍异常！请人工检查 docker compose logs ember；快照保留在 ${snapshot:-未生成}"
fi
exit 1
