#!/usr/bin/env bash
# NexusCompanion 部署闸门：GitHub Actions 那把部署密钥在服务器上的唯一命令面
# （装在 /usr/local/bin/nexus-deploy-gate，由 root 的 authorized_keys 以
# command="..." 强制引用，且只针对那把密钥，日常登录密钥不受影响）。
#
# 为什么要有这一层：仓库 secret 里放的是能登录生产机的私钥。不锁命令面，拿到
# secret 就等于拿到任意 root shell；锁了之后，最多只能触发下面这几个动作。
#
# 允许的 SSH 命令（严格匹配，其它一律 78 拒绝）：
#   preflight <ref>   只检查不动任何东西（脏文件、口令、磁盘、当前库版本）
#   deploy    <ref>   取该 ref 的代码 → pg_dump 备份 → alembic upgrade → 重建 → 探活
#   verify    <ref>   只做探活与日志复核
#   rollback          回到 last-deploy.env 记录的 commit 并重建（库不回滚）
#
# 被执行的 deploy.sh 从刚 fetch 到的 ref 里取（git show FETCH_HEAD:scripts/deploy.sh），
# 而不是服务器上工作树里的旧副本 —— 保证「发版逻辑与要发布的代码同源」，同时远程
# 命令面里不存在任何可以上传/执行任意文件的位置。
#
# 本闸门自己不碰工作树：切代码与 git reset --hard 都留在 scripts/deploy.sh 里，并由
# 它的 preflight（脏文件需显式 DEPLOY_ALLOW_LOCAL_CHANGES=1）把关。
set -euo pipefail

DIR="/opt/NexusCompanion"
raw="${SSH_ORIGINAL_COMMAND:-}"

if [[ "$raw" =~ ^(preflight|deploy|verify)[[:space:]]+([A-Za-z0-9][A-Za-z0-9._/-]{0,63})$ ]]; then
  action="${BASH_REMATCH[1]}"
  ref="${BASH_REMATCH[2]}"
elif [[ "$raw" == "rollback" ]]; then
  # rollback 用工作树里的现有副本：要退回的正是当前已部署的那版流程。
  cd "$DIR"
  export COMPOSE_PROJECT_DIR="$DIR"
  exec bash "$DIR/scripts/deploy.sh" rollback
else
  echo "nexus-deploy-gate: 拒绝命令 —— 只接受 'preflight|deploy|verify <ref>' 或 'rollback'" >&2
  exit 78
fi

cd "$DIR"
# 这台机到 github.com:443 的连接不稳定（实测 3 次里 2 次超时，但重连常常 1 秒就成），
# 所以 fetch 要有界重试；超时用 timeout 兜住，避免整条流水线挂在半开连接上。
fetched=0
for attempt in 1 2 3; do
  if timeout 90 git fetch --quiet --prune origin "$ref" 2>/dev/null; then
    fetched=1
    break
  fi
  echo "nexus-deploy-gate: 第 $attempt 次 fetch origin/$ref 失败，重试" >&2
  sleep 10
done
[[ "$fetched" == "1" ]] || { echo "nexus-deploy-gate: 取不到 origin/$ref" >&2; exit 79; }

script="$(mktemp /tmp/nexus-deploy-script.XXXXXXXX)"
trap 'rm -f "$script"' EXIT
if ! git show FETCH_HEAD:scripts/deploy.sh > "$script"; then
  echo "nexus-deploy-gate: origin/$ref 里没有 scripts/deploy.sh，无法发版" >&2
  exit 79
fi
chmod 700 "$script"

export COMPOSE_PROJECT_DIR="$DIR"
export DEPLOY_REF="$ref"
# 不用 exec：exec 会替换掉本 shell，EXIT trap 就不触发，临时脚本会留在 /tmp。
bash "$script" "$action"
