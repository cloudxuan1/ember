#!/usr/bin/env bash
# 「VPS 操作」按钮在 VPS 这头的白名单：.github/workflows/vps-op.yml 经 SSH 调用。
#
# GitHub Actions 用的那把专用钥匙在 /root/.ssh/authorized_keys 里写成
#   restrict,command="/bin/bash /root/ember/scripts/remote-op.sh" ssh-ed25519 … github-actions vps-op
# 不管对面发什么命令，sshd 都只跑这个脚本，发来的命令放在 SSH_ORIGINAL_COMMAND 里；
# 只认下面五个操作名，别的一律拒绝；restrict 让它开不了终端、不能转发端口。
# 仓库是公开的，按钮的运行日志谁都能看：这里只打印版本、状态和数字，不打印记忆内容和密钥。
set -euo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
umask 077

REPO_DIR="${EMBER_DIR:-/root/ember}"
HEALTH_URL="${EMBER_HEALTH_URL:-http://127.0.0.1:8300/health}"
CONTAINER="${EMBER_CONTAINER:-ember}"
KEEP_BACKUPS="${EMBER_KEEP_OP_BACKUPS:-10}"
LOCK_WAIT_SECONDS="${EMBER_LOCK_WAIT:-300}"

cd "$REPO_DIR"
DEPLOY_LOCK="$REPO_DIR/data/autodeploy.lock"
LOG="$REPO_DIR/data/autodeploy.log"

say() {
  printf '%s\n' "$*"
}

# 跟自动部署用同一把锁：它正在重建容器时先等它做完，拿到锁以后它这 5 分钟一轮会跳过，不会中途重建。
hold_deploy_lock() {
  exec 9>"$DEPLOY_LOCK"
  flock -w "$LOCK_WAIT_SECONDS" 9 || {
    say "自动部署 ${LOCK_WAIT_SECONDS} 秒还没做完，这次先不跑，过几分钟再点"
    exit 1
  }
}

health() {
  if curl -fsS --max-time 5 "$HEALTH_URL" 2>/dev/null | grep -q '"status"[[:space:]]*:[[:space:]]*"ok"'; then
    say "/health ok"
  else
    say "/health 不通！"
  fi
}

# 自动部署日志里只挑它自己写的事件行（日期开头），build 的输出不往外打。
deploy_events() {
  grep -E '^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2} ' "$LOG" 2>/dev/null || true
}

status() {
  say "== 代码版本 =="
  git log -1 --format='%h  %cd  %s' --date=format:'%F %T'
  say "上次部署成功：$(cut -c1-7 data/autodeploy.good 2>/dev/null || say 无记录)"
  if [ -s data/autodeploy.skip ]; then
    say "暂停重试的坏提交：$(cut -c1-7 data/autodeploy.skip)"
  fi
  say "GitHub 上的 main：$(git rev-parse --short origin/main)（自动部署最后一次拉到的）"
  say "== 容器 =="
  docker compose ps --format '{{.Name}}  {{.State}}  {{.Status}}' || say "docker compose ps 出错"
  health
  say "== 自动部署日志（最近 10 条） =="
  deploy_events | tail -n 10
  say "== 磁盘 =="
  df -h / | tail -n 1
}

backup() {
  local dest
  dest="data/ember.db.op-$(date '+%Y%m%d-%H%M%S')-$$.bak"
  python3 scripts/sqlite_backup.py data/ember.db "$dest" > /dev/null
  say "备份好了：$dest（$(du -h "$dest" | cut -f1)，完整性检查 ok）"
  # 只清这个按钮自己拍的 op-*，留最近 N 份；自动部署的 auto-*、手工和日 / 周备份都不碰
  find data -maxdepth 1 -type f -name 'ember.db.op-*.bak' -printf '%T@ %p\n' \
    | sort -rn \
    | tail -n +$((KEEP_BACKUPS + 1)) \
    | cut -d' ' -f2- \
    | xargs -r rm -f --
}

embed_dry_run() {
  hold_deploy_lock
  docker exec "$CONTAINER" python -m app.embeddings --dry-run
}

# 补算照 Codex 的老流程一键走完：备份 → 只数 → 真算 → 再数一遍，最后应是 rings_to_embed: 0。
embed() {
  hold_deploy_lock
  backup
  say "== 补算前（只数，不调 API） =="
  docker exec "$CONTAINER" python -m app.embeddings --dry-run
  say "== 补算 =="
  docker exec "$CONTAINER" python -m app.embeddings
  say "== 补算后（应为 rings_to_embed: 0） =="
  docker exec "$CONTAINER" python -m app.embeddings --dry-run
}

redeploy() {
  # 不能自己拿着锁再调 autodeploy（它拿不到锁会直接静默退出），所以只等锁空出来
  flock -w "$LOCK_WAIT_SECONDS" "$DEPLOY_LOCK" true || {
    say "自动部署 ${LOCK_WAIT_SECONDS} 秒还没做完，这次先不跑，过几分钟再点"
    exit 1
  }
  local since events rc=0
  since=$(date '+%F %T')
  FORCE=1 bash scripts/autodeploy.sh || rc=$?
  # 按时间挑这次写的行（日志超过 1MB 会轮转，数行数不可靠）
  events=$(deploy_events | awk -v since="$since" 'substr($0, 1, 19) >= since')
  say "== 这次部署的日志 =="
  if [ -z "$events" ] && [ "$rc" -eq 0 ]; then
    say "自动部署刚好也在跑，这次没重新部署；过几分钟点「查部署状态」看结果"
  elif [ -z "$events" ]; then
    say "自动部署出错退出（$rc），没写日志；点「查部署状态」看看"
  else
    say "$events"
  fi
  health
  return "$rc"
}

case "${SSH_ORIGINAL_COMMAND:-}" in
  status) status ;;
  backup) backup ;;
  embed-dry-run) embed_dry_run ;;
  embed) embed ;;
  redeploy) redeploy ;;
  *)
    say "只能做这五件事：status / backup / embed-dry-run / embed / redeploy"
    exit 2
    ;;
esac
