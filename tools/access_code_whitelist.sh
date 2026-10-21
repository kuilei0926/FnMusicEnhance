#!/bin/bash
# 把飞牛音乐增强的接口路径加入「访问码（安全码）」白名单。
#
# 背景：飞牛系统设置里的「访问码」开启后，所有 HTTP 请求（含 /music-enhance/*）
#       都要先过 nginx 的 auth_request 校验。手机端调用 /music-enhance/* 的
#       companion 客户端不携带访问码凭据，于是拿到一张 HTTP 200 的校验页
#       （HTML），App 解析 JSON 失败后提示「未检测到增强服务」。
#       服务端无法替客户端完成校验，只能给这两个前缀开白名单。
#
#       详见 README「访问码 / 安全码兼容」。
#
# 用法：
#   sudo ./access_code_whitelist.sh             # 应用（幂等）
#   sudo ./access_code_whitelist.sh --check     # 只读检查，不改动
#   sudo ./access_code_whitelist.sh --rollback  # 从最近一次备份恢复
#
# 环境变量：
#   TRIM_PORT=<端口>   指定探测端口，默认依次尝试 5667(https) / 5666(http)

set -uo pipefail

WL=${WL:-/usr/trim/etc/safe_code_whitelist.conf}
SVC=${SVC:-security_service}

# 白名单里只放增强插件用到的两个前缀：
#   - 不写成 prefix /music-enhance/，以免将来新增接口被自动放行
#   - 官方音乐 /music/api/v1/* 不放行，它本来就带凭据
R1='true * * prefix /music-enhance/health        feiniu-music-enhance probe'
R2='true * * prefix /music-enhance/music/api/v1/ feiniu-music-enhance api'
BASE=/music-enhance

die() { echo "错误: $*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# 探测：经 nginx 不带任何凭据访问，用实际响应判断是否已放行
# ---------------------------------------------------------------------------

API=""

# 2xx / 401 / 403 才算「nginx 应答正常」；3xx 说明该端口只是跳转
# （飞牛的 5666 明文口会 302 到 https），换下一个候选。
try_api() {
    local base=$1 code
    code=$(curl -sk -m 3 -o /dev/null -w '%{http_code}' "$base/" 2>/dev/null)
    case "$code" in
        2??|401|403) API="$base"; return 0 ;;
    esac
    return 1
}

detect_api() {
    local p
    for p in "${TRIM_PORT:-}" 5667 5666; do
        [ -n "$p" ] || continue
        try_api "https://127.0.0.1:$p" && return 0
        try_api "http://127.0.0.1:$p"  && return 0
    done
    return 1
}

# 取 $BASE/health 的状态码 / 字节数 / 正文首字符
fetch_health() {
    local out
    out=$(curl -sk -m 5 -w '\n%{http_code} %{size_download}' "$API$BASE/health" 2>/dev/null)
    H_CODE=$(printf '%s' "$out" | tail -n 1 | awk '{print $1}')
    H_SIZE=$(printf '%s' "$out" | tail -n 1 | awk '{print $2}')
    H_FIRST=$(printf '%s' "$out" | head -n -1 | cut -c 1)
}

# 只有「200 + JSON」才算真正放行。访问码校验页也是 200，但正文是 HTML，
# 所以按首字符判断，不硬编码字节数，兼容不同版本的校验页。
probe_once() {
    fetch_health
    [ "$H_CODE" = "200" ] && [ "$H_FIRST" = "{" ]
}

probe() {
    local sources verdict
    fetch_health
    if [ "$H_CODE" = "200" ] && [ "$H_FIRST" = "{" ]; then
        verdict="✅ 已放行（插件真实响应 ${H_SIZE}B）"
    elif [ "$H_CODE" = "200" ]; then
        verdict="❌ 被访问码拦截（校验页 ${H_SIZE}B）"
    elif [ "$H_CODE" = "404" ]; then
        verdict="❓ 路径未注册（检查 fnmusic_enhance.conf 是否已注入 nginx）"
    elif [ -z "$H_CODE" ] || [ "$H_CODE" = "000" ]; then
        verdict="❌ 无法连接（端口不对或 nginx 未运行）"
    else
        verdict="❓ 异常响应（HTTP ${H_CODE}，${H_SIZE}B）"
    fi
    sources=$(curl -sk -m 5 -o /dev/null -w '%{http_code}' \
              "$API$BASE/music/api/v1/search/sources" 2>/dev/null)
    echo "  $BASE/health              -> HTTP ${H_CODE:-?} ${H_SIZE:-?}B  $verdict"
    echo "  $BASE/.../search/sources  -> HTTP ${sources:-?}（401 = 插件自身 X-API-Key 鉴权，属正常）"
}

# 白名单是否已含该路径（跳过注释行，method 必须为 * 才能覆盖 POST）
has_rule() {
    awk -v p="$1" '
        /^[[:space:]]*#/ { next }
        NF >= 5 && $1 == "true" && $5 == p { found = 1 }
        END { exit(found ? 0 : 1) }
    ' "$WL"
}

require_root() {
    [ "$(id -u)" -eq 0 ] || die "需要 root 权限（请用 sudo 运行）"
}

require_env() {
    [ -f "$WL" ] || die "找不到 $WL —— 本版飞牛可能没有访问码功能，无需处理"
    systemctl list-unit-files 2>/dev/null | grep -q "^$SVC\.service" \
        || die "找不到 $SVC 服务 —— 本版飞牛可能没有访问码功能，无需处理"
    detect_api || die "无法连接本机 nginx（尝试过 5667/5666，可用 TRIM_PORT 指定）"
}

# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------

cmd_check() {
    require_root
    require_env
    echo "== 白名单中的 music-enhance 规则 =="
    grep -nE "^[[:space:]]*[^#].*${BASE}" "$WL" || echo "  （无）"
    echo "== 文件权限（必须是 root:root 644，否则整份白名单会被忽略） =="
    ls -l "$WL"
    echo "== 实际效果（$API） =="
    probe
}

cmd_rollback() {
    require_root
    require_env
    local bak
    bak=$(ls -1t "$WL".bak.* 2>/dev/null | head -1)
    [ -n "$bak" ] || die "没有找到备份 $WL.bak.*（只能手工删除规则后重启 $SVC）"
    cp -a "$bak" "$WL" || die "从 $bak 恢复失败"
    chown root:root "$WL" && chmod 644 "$WL"
    systemctl restart "$SVC" || die "重启 $SVC 失败"
    echo "已从 $bak 恢复并重启 $SVC。"
    probe
}

cmd_apply() {
    require_root
    require_env

    if probe_once; then
        echo "当前 $BASE 已可直通（访问码未开启，或规则已生效），无需处理。"
        probe
        return 0
    fi

    echo "[1/5] 备份白名单"
    local bak
    bak="$WL.bak.$(date +%Y%m%d-%H%M%S)"
    cp -a "$WL" "$bak" || die "备份失败"
    echo "  -> $bak"

    echo "[2/5] 写入规则（幂等）"
    # 文件末尾无换行时，直接 >> 会把新规则粘到上一行，导致整行失效
    if [ -s "$WL" ] && [ -n "$(tail -c1 "$WL")" ]; then
        printf '\n' >> "$WL"
        echo "  已补齐文件末尾换行"
    fi
    local r pat
    for r in "$R1" "$R2"; do
        pat=$(printf '%s' "$r" | awk '{print $5}')
        if has_rule "$pat"; then
            echo "  已存在，跳过: $pat"
        else
            printf '%s\n' "$r" >> "$WL"
            echo "  已追加: $pat"
        fi
    done

    echo "[3/5] 校正权限"
    chown root:root "$WL" && chmod 644 "$WL" || die "校正权限失败"
    ls -l "$WL"

    echo "[4/5] 重启 $SVC（白名单仅在进程启动时加载）"
    systemctl restart "$SVC" || die "重启失败，请执行 --rollback 恢复"
    local i
    for i in $(seq 1 10); do
        systemctl is-active --quiet "$SVC" && break
        sleep 1
    done
    systemctl is-active --quiet "$SVC" || die "$SVC 未运行，请执行 --rollback 恢复"

    echo "[5/5] 校验"
    for i in $(seq 1 5); do
        probe_once && break
        sleep 1
    done
    probe

    if probe_once; then
        echo
        echo "完成。"
    else
        echo
        echo "⚠️  规则已写入但未生效，请依次检查："
        echo "  1) journalctl -u $SVC -n 50 里是否有 invalid whitelist line"
        echo "  2) $WL 是否被系统升级覆盖"
        echo "  3) 回滚：$0 --rollback"
        return 1
    fi
}

case "${1:-}" in
    ""|apply)   cmd_apply ;;
    --check)    cmd_check ;;
    --rollback) cmd_rollback ;;
    -h|--help)
        sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'
        ;;
    *)
        die "未知参数: $1（可用 --check / --rollback / --help）"
        ;;
esac
