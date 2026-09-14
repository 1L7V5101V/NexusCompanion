#!/bin/sh
# C5 recovery runbook 三路径实跑（canary 环境：nexus-c5-canary + nexus-pg-c5）
#
# 对应 openspec/evidence/c5-auth-provisioning-admin/runbook-drill.md 的路径
# A（lost-token）/ B（suspected-leak）/ C（database-restore + pepper 代次失配）。
#
# 安全约定：
#  - CLI 明文 recovery token 只重定向到宿主机文件，随即提取进容器 /tmp/drill/
#    并删除原始输出；本脚本 stdout 只打印长度/前缀等非敏感事实。
#  - pepper 与 admin_credentials 行在改动前均有备份，脚本末尾恢复原代次，
#    使 canary 回到演练前状态。
set -eu

C=nexus-c5-canary
P=nexus-pg-c5
D=/root/drill          # 宿主机工作目录
K=/tmp/drill           # 容器内工作目录

mkdir -p "$D"

dex() { docker exec -i "$C" "$@"; }
dsh() { docker exec -i "$C" sh -c "$1"; }
psqlq() { docker exec -i "$P" psql -U nexus -d nexus -tA -c "$1"; }
probe() { dex python "$K/drill_http.py" "$@"; }
svc() { docker exec -i -e PYTHONPATH=/app "$C" python "$K/drill_service.py" "$@"; }
cli() { script -qec "docker exec -it $C python main.py pilot-admin $*" /dev/null | tr -d '\000'; }

expect() {  # expect <输出> <期望子串> <标签>
    printf '%s\n' "$1" | sed 's/^/        /'
    case "$1" in
        *"$2"*) echo "    [OK] $3" ;;
        *) echo "    [FAIL] $3（期望含 '$2'）"; exit 1 ;;
    esac
}

rotate_force_local() {  # rotate_force_local <容器内目标文件> <标签>
    raw="$D/raw_rotate.out"
    script -qec "docker exec -it $C python main.py pilot-admin rotate-recovery-token --force-local" /dev/null > "$raw" 2>&1
    tok=$(grep -oE 'nad_[A-Za-z0-9_-]+' "$raw" | head -1)
    if [ -z "$tok" ]; then
        echo "    [FAIL] $2：未从 CLI 输出捕获 recovery token"
        exit 1
    fi
    printf '%s' "$tok" > "$D/.tmp.tok"
    docker exec -i "$C" sh -c "cat > $1 && chmod 600 $1" < "$D/.tmp.tok"
    rm -f "$D/.tmp.tok" "$raw"
    echo "    [OK] $2：明文已捕获（len=${#tok}，前缀 nad_），仅存于容器 $1；CLI 原始输出已删除"
}

bootstrap_capture() {  # bootstrap_capture <容器内目标文件>
    raw="$D/raw_bootstrap.out"
    script -qec "docker exec -it $C python main.py pilot-admin bootstrap" /dev/null > "$raw" 2>&1
    tok=$(grep -oE 'nad_[A-Za-z0-9_-]+' "$raw" | head -1)
    if [ -z "$tok" ]; then
        echo "    [FAIL] bootstrap：未捕获 recovery token"; exit 1
    fi
    printf '%s' "$tok" > "$D/.tmp.tok"
    docker exec -i "$C" sh -c "cat > $1 && chmod 600 $1" < "$D/.tmp.tok"
    rm -f "$D/.tmp.tok" "$raw"
    echo "    [OK] bootstrap：明文已捕获（len=${#tok}），仅存于容器 $1"
}

echo "########## 0. 前置状态 ##########"
rev0=$(psqlq "SELECT revision FROM admin_credentials")
echo "  admin_credentials.revision = $rev0"
psqlq "SELECT 'admin_credentials 行数 = '||count(*) FROM admin_credentials"
psqlq "SELECT 'enabled = '||enabled FROM admin_credentials"
psqlq "SELECT 'access_tokens = '||count(*) FROM access_tokens"
psqlq "SELECT 'auth_sessions = '||count(*) FROM auth_sessions"
echo "  pepper sha256 前 16 位（容器内）：$(dsh "sha256sum /root/.nexus/workspace/secrets/auth_pepper | cut -c1-16")"
docker cp "$C:/root/.nexus/workspace/secrets/auth_pepper" "$D/pepper.orig" > /dev/null
echo "  pepper 已备份到宿主机 $D/pepper.orig（sha256 前 16 位：$(sha256sum "$D/pepper.orig" | cut -c1-16)）"

echo
echo "########## 路径 A：lost-token（未外泄） ##########"
echo "  A1 rotate-recovery-token --force-local（受信主机 TTY）"
rotate_force_local "$K/T1" "A1 轮换后新令牌 T1"
status_a=$(cli status)
echo "  A2 status："
expect "$status_a" "revision: $((rev0 + 1))" "A2 revision 由 $rev0 增至 $((rev0 + 1))"
expect "$(probe admin-exchange "$K/T1" "$K/ck1")" "status=200" "A2 新令牌兑换 200（本机来源）"
echo "  A2 轮换不动浏览器会话（该断言在 B1 以「轮换前建立的会话仍 200」实证）："
expect "$status_a" "active_sessions:" "A2 status 输出含 active_sessions"
echo "  A3 收尾：T1 为当前有效明文，演练结束后按运维流程入 secret 管理（本演练留存于容器 $K/T1）"

echo
echo "########## 路径 B：suspected-leak ##########"
echo "  B1 立即轮换（新 digest 替换；旧明文 T1 不可再兑换）"
rotate_force_local "$K/T2" "B1 轮换后新令牌 T2"
expect "$(probe admin-get /api/admin/test-accounts "$K/ck1")" "status=200" \
    "B1 轮换不影响已有 admin 浏览器会话（§10 DECIDED：轮换与撤销独立）"
echo "  B2 revoke-sessions --all（撤销全部 admin 会话，recovery token 不变）"
expect "$(cli revoke-sessions --all)" "已撤销全部 admin 浏览器会话" "B2 会话撤销命令成功"
expect "$(probe admin-get /api/admin/test-accounts "$K/ck1")" "status=401" \
    "B2 撤销后原 admin 会话立即失效（401）"
echo "  B3 核对审计（admin_audit_events，无明文）"
psqlq "SELECT action||' '||coalesce(detail::text,'') FROM admin_audit_events WHERE action IN ('admin.rotate_recovery','admin.revoke_sessions') ORDER BY created_at DESC LIMIT 3" | sed 's/^/        /'
audit_rot=$(psqlq "SELECT count(*) FROM admin_audit_events WHERE action='admin.rotate_recovery' AND detail->>'force_local'='true'")
audit_rev=$(psqlq "SELECT count(*) FROM admin_audit_events WHERE action='admin.revoke_sessions'")
echo "    [OK] admin.rotate_recovery(force_local=true) 行数=$audit_rot；admin.revoke_sessions 行数=$audit_rev"
echo "  B4 兜底：disable → enable（enable 不复活已撤销会话）"
expect "$(cli disable)" "已关闭" "B4 disable 关闭管理入口并撤销会话"
expect "$(cli enable)" "已启用" "B4 enable 重开管理入口"
echo "  B5 验证：泄露的旧明文 T1 兑换必须 401（统一文案，不泄露原因）"
expect "$(probe admin-exchange "$K/T1")" "status=401" "B5 旧令牌 T1 兑换 401"

echo
echo "########## 路径 C：database-restore / pepper 代次失配 ##########"
echo "  C0 备份 admin_credentials 行（运维清行前的备份）"
psqlq "DROP TABLE IF EXISTS admin_credentials_drill_bak"
psqlq "CREATE TABLE admin_credentials_drill_bak AS SELECT * FROM admin_credentials"
bak_rev=$(psqlq "SELECT revision FROM admin_credentials_drill_bak")
echo "    备份行数 = $(psqlq "SELECT count(*) FROM admin_credentials_drill_bak")，备份行 revision = $bak_rev"

echo "  C-基线 用 T2 建立 admin 会话 ck2，并签发两个用户邀请"
expect "$(probe admin-exchange "$K/T2" "$K/ck2")" "status=200" "C-基线 T2 兑换成功（同代次）"
expect "$(probe admin-get /api/admin/test-accounts "$K/ck2")" "status=200" "C-基线 ck2 会话可用"
echo "    签发用户邀请 utok_old（随即兑换，证明变更前可用）"
expect "$(probe create-account "$K/ck2" "DrillC-Old" "$K/utok_old")" "status=200" "C-基线 建账号+签发 utok_old"
expect "$(svc exchange "$K/utok_old")" "status=200" "C-基线 utok_old 兑换 200"
echo "    签发用户邀请 utok_hold（不兑换，留待变更后断言 401）"
expect "$(svc issue "$K/utok_hold")" "status=200" "C-基线 签发 utok_hold（服务层签发）"

echo "  C2 制造 pepper 代次失配（写入新代次 pepper 并重启服务）"
openssl rand -hex 32 > "$D/pepper.new"
docker cp "$D/pepper.new" "$C:/root/.nexus/workspace/secrets/auth_pepper" > /dev/null
docker restart "$C" > /dev/null
echo "    新 pepper sha256 前 16 位：$(sha256sum "$D/pepper.new" | cut -c1-16)；服务重启中…"
probe wait
expect "$(probe admin-exchange "$K/T2")" "status=401" "C2 失配后旧 recovery token T2 兑换 401"
expect "$(probe admin-get /api/admin/test-accounts "$K/ck2")" "status=401" "C2 失配后旧 admin 会话 401"
expect "$(svc exchange "$K/utok_hold")" "status=401" "C2 失配后既有用户邀请 token 兑换 401（全部 digest 失效）"

echo "  C2 重建 admin（旧行已备份并在下方清行，再 bootstrap）"
psqlq "DELETE FROM admin_credentials"
echo "    清行后行数 = $(psqlq "SELECT count(*) FROM admin_credentials")"
bootstrap_capture "$K/T3"
expect "$(probe admin-exchange "$K/T3" "$K/ck3")" "status=200" "C2 bootstrap 后新令牌 T3 兑换 200"
expect "$(probe admin-get /api/admin/test-accounts "$K/ck3")" "status=200" "C2 新 admin 会话 ck3 可用"
echo "    重新签发用户邀请（新代次）"
expect "$(probe create-account "$K/ck3" "DrillC-New" "$K/utok_new")" "status=200" "C2 新代次建账号+签发 utok_new"
expect "$(svc exchange "$K/utok_new")" "status=200" "C2 新代次邀请兑换 200（重新走邀请流程即恢复）"

echo "  C1 恢复原代次：pepper 与 admin_credentials 行同时回到备份（同代即贯通）"
docker cp "$D/pepper.orig" "$C:/root/.nexus/workspace/secrets/auth_pepper" > /dev/null
psqlq "DELETE FROM admin_credentials"
psqlq "INSERT INTO admin_credentials SELECT * FROM admin_credentials_drill_bak"
psqlq "DROP TABLE admin_credentials_drill_bak"
docker restart "$C" > /dev/null
probe wait
echo "    pepper sha256 前 16 位（恢复后）：$(dsh "sha256sum /root/.nexus/workspace/secrets/auth_pepper | cut -c1-16")"
expect "$(probe admin-get /api/admin/test-accounts "$K/ck2")" "status=200" \
    "C1 恢复同代次后原 admin 会话 ck2 贯通（digest 重新匹配）"
expect "$(probe admin-exchange "$K/T2")" "status=200" "C1 恢复同代次后原 recovery token T2 兑换 200"
expect "$(probe admin-get /api/admin/test-accounts "$K/ck3")" "status=401" \
    "C1 对照：失配期新建的 ck3 在恢复后失效（digest 不跨代次）"
echo "    C4 复核：恢复后 admin_credentials.revision = $(psqlq "SELECT revision FROM admin_credentials")（C0 备份行 revision = $bak_rev），行数 = $(psqlq "SELECT count(*) FROM admin_credentials")"

echo
echo "########## 收尾核对 ##########"
echo "  digest-only（部署态库内）"
psqlq "SELECT 'access_tokens 64-hex 行数 = '||count(*)||' / '||(SELECT count(*) FROM access_tokens) FROM access_tokens WHERE length(token_digest)=64 AND token_digest ~ '^[0-9a-f]+'"
psqlq "SELECT 'auth_sessions 64-hex 行数 = '||count(*)||' / '||(SELECT count(*) FROM auth_sessions) FROM auth_sessions WHERE length(session_digest)=64 AND session_digest ~ '^[0-9a-f]+'"
psqlq "SELECT 'admin_audit_events 明文前缀命中 = '||count(*) FROM admin_audit_events WHERE (actor||action||coalesce(detail::text,'')) ~ '(nxt_|nad_|ns_)'"
echo "  最终 status："
cli status | sed 's/^/        /'
echo
echo "DRILL COMPLETE"
