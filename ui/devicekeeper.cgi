#!/bin/sh
set -u

PORT="${DEVICEKEEPER_PORT:-9365}"
URI="${FILE_URI:-}"
URI="${URI%%\?*}"
URI="${URI%%#*}"
URI="${URI#/}"
METHOD="${REQUEST_METHOD:-GET}"
QUERY="${QUERY_STRING:-}"
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

# plugin.cgi starts this file through fexecve, so $0 is /dev/fd/3 for real
# requests. Rebuild the UI directory from the request path in that case.
if [ -n "${REQUEST_URI:-}" ] && [ -n "${USER:-}" ]; then
    request_path="${REQUEST_URI#/}"
    request_rest="${request_path#*/}"
    plugin_name="${request_rest%%/*}"
    candidate="/data/plugin/www/$USER/$plugin_name"
    if [ -n "$plugin_name" ] && [ -d "$candidate" ]; then
        SCRIPT_DIR="$candidate"
    fi
fi

common_headers() {
    printf 'X-Content-Type-Options: nosniff\r\n'
    printf 'Referrer-Policy: no-referrer\r\n'
    printf 'X-Frame-Options: SAMEORIGIN\r\n'
    printf 'Permissions-Policy: camera=(), microphone=(), geolocation=()\r\n'
    printf "Content-Security-Policy: default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; base-uri 'self'; form-action 'self'; frame-ancestors 'self'\r\n"
}

forbidden() {
    printf 'Status: 403 Forbidden\r\n'
    printf 'Content-Type: application/json; charset=utf-8\r\n'
    printf 'Cache-Control: no-store\r\n'
    common_headers
    printf '\r\n'
    printf '{"ok":false,"code":"forbidden","message":"需要管理员权限"}\n'
}

# The URL owner in USER may be an admin; MIUID identifies the logged-in user.
# Keep the admin-only contract even if a non-admin knows the direct URL.
UCI_BIN=/usr/sbin/uci
[ -x "$UCI_BIN" ] || UCI_BIN=uci
if [ -z "${MIUID:-}" ]; then
    forbidden
    exit 0
fi
actual_user="u${MIUID#u}"
permission=$("$UCI_BIN" -q get "user.$actual_user.permission" 2>/dev/null || true)
if [ -z "$permission" ]; then
    permission=$(awk -v u="$actual_user" '
        $1 == "config" && $2 == "user" { cur=$3; gsub(/\047/, "", cur) }
        cur == u && $1 == "option" && $2 == "permission" {
            gsub(/\047/, "", $3); print $3; exit
        }
    ' /etc/config/user 2>/dev/null)
fi
if [ "$permission" != "admin" ]; then
    forbidden
    exit 0
fi

not_found() {
    printf 'Status: 404 Not Found\r\n'
    printf 'Content-Type: application/json; charset=utf-8\r\n'
    printf 'Cache-Control: no-store\r\n'
    common_headers
    printf '\r\n'
    printf '{"ok":false,"code":"not_found","message":"资源不存在"}\n'
}

serve_static() {
    filename="$1"
    content_type="$2"
    file_path="$SCRIPT_DIR/$filename"
    if [ ! -f "$file_path" ]; then
        not_found
        return
    fi
    printf 'Status: 200 OK\r\n'
    printf 'Content-Type: %s\r\n' "$content_type"
    if [ "$filename" = "index.html" ]; then
        printf 'Cache-Control: no-store\r\n'
    else
        printf 'Cache-Control: public, max-age=3600\r\n'
    fi
    common_headers
    printf '\r\n'
    if [ "$METHOD" != "HEAD" ]; then
        cat "$file_path"
    fi
}

case "$URI" in
    ""|"/"|"index.html")
        serve_static "index.html" "text/html; charset=utf-8"
        exit 0
        ;;
    "styles.css")
        serve_static "styles.css" "text/css; charset=utf-8"
        exit 0
        ;;
    "app.js")
        serve_static "app.js" "application/javascript; charset=utf-8"
        exit 0
        ;;
    "icon.png"|"favicon.ico")
        serve_static "icon.png" "image/png"
        exit 0
        ;;
esac

case "$URI" in
    api/v1/health|api/v1/capabilities|api/v1/snapshot|api/v1/storage|api/v1/services|api/v1/events|api/v1/history)
        api_path="$URI"
        ;;
    *)
        not_found
        exit 0
        ;;
esac

if [ "$METHOD" != "GET" ]; then
    printf 'Status: 405 Method Not Allowed\r\n'
    printf 'Allow: GET\r\n'
    printf 'Content-Type: application/json; charset=utf-8\r\n'
    printf 'Cache-Control: no-store\r\n'
    common_headers
    printf '\r\n'
    printf '{"ok":false,"code":"method_not_allowed","message":"仅支持 GET"}\n'
    exit 0
fi

url="http://127.0.0.1:$PORT/$api_path"
if [ -n "$QUERY" ]; then
    url="$url?$QUERY"
fi

if ! response=$(curl -sS --connect-timeout 2 --max-time 6 -w '\n%{http_code}' "$url" 2>/dev/null); then
    response="
502"
fi
status=$(printf '%s\n' "$response" | tail -n 1)
body=$(printf '%s\n' "$response" | sed '$d')

case "$status" in
    200) reason="OK" ;;
    400) reason="Bad Request" ;;
    401) reason="Unauthorized" ;;
    403) reason="Forbidden" ;;
    404) reason="Not Found" ;;
    405) reason="Method Not Allowed" ;;
    413) reason="Payload Too Large" ;;
    429) reason="Too Many Requests" ;;
    503) reason="Service Unavailable" ;;
    *)
        status="502"
        reason="Bad Gateway"
        body='{"ok":false,"code":"upstream_unavailable","message":"设备管家服务暂时不可达"}'
        ;;
esac

printf 'Status: %s %s\r\n' "$status" "$reason"
printf 'Content-Type: application/json; charset=utf-8\r\n'
printf 'Cache-Control: no-store\r\n'
common_headers
printf '\r\n'
printf '%s\n' "$body"
