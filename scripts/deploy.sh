#!/usr/bin/env bash
# NexusCompanion 生产发版脚本（在部署机上运行，compose 项目目录内）。
#
#   scripts/deploy.sh preflight   # 只检查不动：脏文件、口令、磁盘、当前镜像与库版本
#   scripts/deploy.sh backup      # pg_dump 到备份目录（deploy 会自动调）
#   scripts/deploy.sh deploy      # 备份 → 取代码 → alembic upgrade → 重建 → 验证
#   scripts/deploy.sh verify      # 只做发版后验证
#   scripts/deploy.sh rollback    # 回到上一个记录的 commit 并重建容器（库不回滚）
#
# 设计约束（为什么不是一句 docker compose up -d --build）：
# - 迁移与代码必须同批：schema 落后时代码不写新表，代码超前时新表无人建。
# - expand-only 迁移允许「库前进、代码回滚」，所以 rollback 不动数据库。
# - 每次 deploy 前先落一份 pg_dump 与一个 state 文件，失败可查可退。
set -euo pipefail

COMPOSE_PROJECT_DIR="${COMPOSE_PROJECT_DIR:-/opt/NexusCompanion}"
STATE_DIR="${NEXUS_DEPLOY_STATE:-/root/nexus-deploy-state}"
BACKUP_DIR="${NEXUS_PG_BACKUP_DIR:-/root/nexus-pg-backups}"
PG_CONTAINER="${PG_CONTAINER:-nexus-postgres}"
PG_USER="${PG_USER:-nexus}"
PG_DB="${PG_DB:-nexus}"
APP_CONTAINER="${APP_CONTAINER:-NexusCompanion}"
DASHBOARD_URL="${DASHBOARD_URL:-http://127.0.0.1:2236/}"
CHAT_URL="${CHAT_URL:-http://127.0.0.1:6322/}"
TS="$(date +%Y%m%d-%H%M%S)"
KEEP_BACKUPS="${KEEP_BACKUPS:-8}"

log() { printf '[deploy] %s\n' "$*"; }
die() { printf '[deploy][FAIL] %s\n' "$*" >&2; exit 1; }

usage() { sed -n '2,13p' "$0"; }

ACTION="${1:-}"
case "$ACTION" in
  preflight|backup|deploy|verify|rollback) ;;
  *) usage; exit 2 ;;
esac
# 动作与凭据先于任何文件系统动作校验：缺 DEPLOY_REF 不该被「目录不存在」掩盖。
if [[ "$ACTION" == "deploy" ]]; then
  : "${DEPLOY_REF:?deploy 需要 DEPLOY_REF（要发布到的远端分支名）}"
fi

cd "$COMPOSE_PROJECT_DIR" 2>/dev/null || die "compose 项目目录不存在：$COMPOSE_PROJECT_DIR"
mkdir -p "$STATE_DIR" "$BACKUP_DIR"

# 迁移需要同步驱动（alembic env.py 走 SQLAlchemy 同步 engine），端口/主机按容器网络写。
migrate_database_url() {
  local pw="${POSTGRES_PASSWORD:-}"
  [[ -n "$pw" ]] || pw="$(sed -n 's/^[[:space:]]*POSTGRES_PASSWORD:[[:space:]]*"\{0,1\}\([^"]*\)"\{0,1\}[[:space:]]*$/\1/p' docker-compose.yml | head -1)"
  printf 'postgresql+psycopg://%s:%s@postgres:5432/%s' "$PG_USER" "$pw" "$PG_DB"
}

db_version() {
  docker exec "$PG_CONTAINER" psql -U "$PG_USER" -d "$PG_DB" -tAc \
    "select coalesce(string_agg(version_num,','),'(none)') from alembic_version" | tr -d '[:space:]'
}

head_revision() {
  docker compose run --rm -e DATABASE_URL="$(migrate_database_url)" nexus \
    python -c "from alembic.config import Config; from alembic.script import ScriptDirectory; \
s=ScriptDirectory.from_config(Config('alembic.ini')); print(','.join(s.get_heads()))" 2>/dev/null | tail -1
}

preflight() {
  [[ -f docker-compose.yml ]] || die "不在 compose 项目目录"
  [[ -f config.toml ]] || die "缺少 config.toml（应用配置）"
  [[ -f .env ]] || log "提示：没有 .env，compose 渲染会因缺 POSTGRES_PASSWORD 而失败"
  command -v docker >/dev/null || die "无 docker"
  docker compose config >/dev/null 2>&1 || die "docker compose config 渲染失败（先看 .env）"

  local dirty
  dirty="$(git status --porcelain --untracked-files=no)"
  if [[ -n "$dirty" ]]; then
    log "工作树有未提交的跟踪文件改动："
    printf '%s\n' "$dirty"
    local tar="$STATE_DIR/local-changes-$TS.tar"
    tar -cf "$tar" $(printf '%s\n' "$dirty" | awk '{print $2}') 2>/dev/null || true
    log "已备份到 $tar；确认无本仓库独有热修复后可继续"
    [[ "${DEPLOY_ALLOW_LOCAL_CHANGES:-0}" == "1" ]] \
      || die "带脏文件继续需显式 DEPLOY_ALLOW_LOCAL_CHANGES=1"
  fi
  df -P / | awk 'NR==2 {if ($4 < 2*1024*1024) exit 1}' || die "根分区可用空间不足 2G"
  log "preflight OK；当前库版本=$(db_version)"
}

backup_db() {
  local f="$BACKUP_DIR/$PG_DB-$TS.dump"
  docker exec "$PG_CONTAINER" pg_dump -U "$PG_USER" -Fc "$PG_DB" > "$f"
  [[ -s "$f" ]] || die "pg_dump 产出为空：$f"
  log "数据库已备份：$f ($(du -h "$f" | cut -f1))"
  ls -1t "$BACKUP_DIR"/*.dump 2>/dev/null | tail -n +$((KEEP_BACKUPS + 1)) | xargs -r rm -f
}

verify() {
  docker inspect -f '{{.State.Status}} {{.State.Restarting}}' "$APP_CONTAINER" 2>/dev/null \
    | grep -q '^running false' || die "应用容器未在正常运行"
  local code
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "$DASHBOARD_URL" || echo 000)"
  [[ "$code" =~ ^[23] ]] || die "Dashboard 探活失败：HTTP $code"
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "$CHAT_URL" || echo 000)"
  [[ "$code" =~ ^[23] ]] || die "WebChat 探活失败：HTTP $code"
  log "探活 OK（Dashboard/Chat 均 2xx-3xx）"
  docker logs --since 5m "$APP_CONTAINER" 2>&1 | grep -ciE "traceback|error:" >/dev/null \
    && log "注意：近 5 分钟日志含错误字样，请人工复核" || log "近 5 分钟日志无异常字样"
  log "库版本=$(db_version)"
}

deploy() {
  preflight
  local before_sha image_before
  before_sha="$(git rev-parse HEAD)"
  image_before="$(docker inspect -f '{{.Image}}' "$APP_CONTAINER" 2>/dev/null || echo none)"
  {
    printf 'before_sha=%s\n' "$before_sha"
    printf 'before_image=%s\n' "$image_before"
    printf 'backup=%s\n' "$BACKUP_DIR/$PG_DB-$TS.dump"
  } > "$STATE_DIR/last-deploy.env"
  log "回滚锚点：commit=$before_sha image=$image_before"

  backup_db
  log "取代码（远端 $DEPLOY_REF）"
  git fetch --prune origin
  git checkout "$DEPLOY_REF" >> "$STATE_DIR/deploy-$TS.log" 2>&1
  git reset --hard "origin/$DEPLOY_REF" >> "$STATE_DIR/deploy-$TS.log" 2>&1
  log "HEAD=$(git rev-parse --short HEAD)"

  local before after
  before="$(db_version)"
  log "迁移前库版本：$before → head=$(head_revision)"
  docker compose run --rm -e DATABASE_URL="$(migrate_database_url)" nexus alembic upgrade head
  after="$(db_version)"
  log "迁移后库版本：$after"

  log "重建并启动容器"
  docker compose up -d --build >> "$STATE_DIR/deploy-$TS.log" 2>&1
  sleep 8
  verify
  log "发版完成：$before_sha → $(git rev-parse --short HEAD)，日志在 $STATE_DIR/deploy-$TS.log"
}

rollback() {
  [[ -f "$STATE_DIR/last-deploy.env" ]] || die "没有上一次 deploy 的锚点记录"
  # shellcheck disable=SC1091
  source "$STATE_DIR/last-deploy.env"
  log "回滚到 commit=${before_sha}（数据库不回滚：本次迁移为 expand-only）"
  git fetch --prune origin
  git checkout "$before_sha"
  docker compose up -d --build
  sleep 8
  verify
  log "回滚完成"
}

case "$ACTION" in
  preflight) DEPLOY_ALLOW_LOCAL_CHANGES="${DEPLOY_ALLOW_LOCAL_CHANGES:-1}" preflight ;;
  backup)    backup_db ;;
  deploy)    deploy ;;
  verify)    verify ;;
  rollback)  rollback ;;
esac
