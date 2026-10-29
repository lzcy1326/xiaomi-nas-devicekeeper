# 小米 NAS 插件开发规范（手动部署实测整理）

> 来源：小红书笔记《小米 NAS 插件开发规范｜手动部署实测总结》，作者 Jray，正文及 13 张配图。
>
> 原文链接：https://xhslink.cn/o/5Q4f5qKs0YX
>
> 性质：这是第三方开发者的手动部署实测总结，不是小米官方公开 API 文档。不同固件版本的路径、命令、服务名和鉴权行为可能不同。修改 `/etc`、Nginx、systemd、plugincenter 前必须先备份，并在测试 NAS 上验证。

## 1. 先记住三件事

小米 NAS 插件不是单一网页，而是三部分：

1. **本地能力**：本地 HTTP 服务，或纯静态 UI 文件。
2. **Web UI**：在米家 App 内嵌 WebView 中打开的页面。
3. **注册条目**：通过 `.list` 和应用市场配置让 App 发现、展示和打开插件。

缺任何一项，插件都可能显示异常、点击空白或无法运行。

两个基本形态：

| 形态 | 组成 | 适合场景 |
| --- | --- | --- |
| 本地服务型 | Python / Go / Node 等本地 HTTP 服务 + UI | 采集、控制、有后端逻辑 |
| 静态 UI 型 | HTML / JS / CSS + `plugin.cgi` 入口 | 展示、跳转、简单页面 |

## 2. 总体链路

```text
米家 App / 应用市场
        |
        | HTTPS 443（mTLS + 登录态）
        v
      Nginx
        |
        +--> plugin.cgi / <plugin>.cgi（静态 UI、登录态校验）
        |
        +--> 自定义 Nginx 反代（本地服务型、第三方 Web App）
                 |
                 v
        本地 HTTP 服务（如 127.0.0.1:9300）
```

关键约束：

- App 实际访问路径是 `/plugin/<uid>/<plugin>/...`，**不带 `u` 前缀**。
- 文件系统里的用户目录、`.list` 文件名经常出现 `u<uid>`，但 HTTP 路径里的 UID 不带 `u`。
- Nginx 自定义规则要兼容 `u?<uid>`，否则可能出现 UI 或 API 只通一半。
- 443 入口带有 mTLS 和登录态；没有有效客户端证书或登录态时，可能返回 400、403 或跳转登录。

## 3. 目录结构

### 3.1 源码目录

笔记示例：

```text
/nas/pool0/<uid>/plugin/pluginsrc/<plugin>/
```

部分配图写成 `/nas/pool0/u<uid>/...` 或 `/home/u<uid>/...`。不同固件或不同用户的路径前缀可能不同，部署前先确认：

```sh
ls -ld /nas/pool0/*/plugin/pluginsrc 2>/dev/null
ls -ld /data/plugin/www/*
ls -l /data/plugin/*.list
```

### 3.2 推荐文件树

```text
<plugin>/
├── INFO                         # 插件元数据 JSON
├── files/
│   ├── httpd.py                 # 本地 HTTP 服务，或替换成你的后端
│   ├── server.sh                # start/stop/status/restart 控制脚本
│   └── collect.sh               # 业务逻辑，如采集、上报
├── scripts/
│   └── control                  # 生命周期脚本
└── ui/
    ├── index.html               # 主页面
    ├── <plugin>.cgi             # 静态 UI 型入口
    ├── icon.png                 # 图标源文件
    ├── css/
    ├── js/
    └── img/
```

必须注意：

- 目录名 `<plugin>` 必须与 `INFO.plugin` 一致。
- `ui/index.html` 是 App 默认入口。
- 静态 UI 型必须有 `ui/<plugin>.cgi`，且可执行。
- `scripts/control` 是生命周期钩子，常见动作包括 `enable`、`disable`、`postinstall`；完整动作集合要按固件确认。
- 脚本建议 UTF-8 无 BOM、Linux LF 换行。
- 普通静态文件一般 `644`，脚本和目录按需 `755`；敏感配置用 `600`。

## 4. INFO 元数据

INFO 是插件的“身份证”，plugincenter 和应用市场依赖它识别插件。

笔记示例要求同一份 INFO 放在两个位置：

```text
/nas/pool0/<uid>/plugin/pluginsrc/<plugin>/INFO
/home/<uid>/plugin/<plugin>/INFO
```

不同固件可能使用 `u<uid>` 前缀，实际以现有官方插件目录为准。两处内容必须保持一致。

### 4.1 合法 JSON 示例

JSON 不能写注释，下面只展示可用结构：

```json
{
  "plugin": "sysinfo",
  "name": "设备信息",
  "id": 99,
  "version": "1.0.0",
  "tags": ["tool"],
  "timestamp": 1788100000,
  "desc": "插件描述",
  "developer": "YourName",
  "publisher": "YourTeam",
  "changelog": "版本说明",
  "system": false,
  "size": 10000,
  "port": "9300",
  "type": "standard",
  "forceupgrade": false,
  "ext": {}
}
```

### 4.2 字段规则

| 字段 | 要求 |
| --- | --- |
| `plugin` | 插件 ID，必须与目录名一致 |
| `name` | App 中显示的插件名 |
| `id` | 数字 ID；自研插件建议使用高位数字，避开官方占用 |
| `version` | 版本号，建议语义化版本 |
| `tags` | 分类标签数组 |
| `timestamp` | 秒级时间戳，可用于排序或更新判断 |
| `desc` | 简要描述 |
| `developer` / `publisher` | 开发者和发布者 |
| `changelog` | 版本说明 |
| `system` | 自研插件固定 `false` |
| `size` | 估计大小，单位字节 |
| `port` | 本地服务端口，**必须是字符串** |
| `type` | 固定为 `"standard"` |
| `forceupgrade` | 是否强制升级 |
| `ext` | 扩展字段，自研插件建议先留空对象 `{}` |

最小可用示例：

```json
{
  "plugin": "sysinfo",
  "name": "设备信息",
  "id": 99,
  "version": "1.0.0",
  "port": "9300",
  "type": "standard",
  "system": false,
  "ext": {}
}
```

常见错误：

- `port` 写成数字而不是字符串。
- 字段名大小写不统一。
- JSON 里写 `//` 注释。
- 两处 INFO 内容不一致。
- `plugin` 与目录名不一致。
- 修改 INFO 后没有重启相关服务。

## 5. `.list` 注册条目

### 5.1 文件位置

```text
/data/plugin/u<uid>.list
```

`.list` 是 App 能否显示插件的核心配置。每个插件是顶层的一个 key，值是一个对象。

修改前必须备份：

```sh
cp /data/plugin/u<uid>.list /data/plugin/u<uid>.list.bak-$(date +%F)
```

### 5.2 结构示例

```json
{
  "sysinfo": {
    "status": "running",
    "install": true,
    "upgrade": false,
    "enable": true,
    "changetime": 1788100000,
    "icon": "/icon/sysinfo.icon",
    "progress": "100",
    "resource": {
      "mpk": "/nas/pool0/u<uid>/plugin/pluginsrc/sysinfo/INFO",
      "icon": "/icon/sysinfo.icon",
      "preview": []
    },
    "info": {
      "plugin": "sysinfo",
      "name": "设备信息",
      "id": 99,
      "version": "1.0.0",
      "tags": ["tool"],
      "timestamp": 1788100000,
      "desc": "设备信息采集与展示",
      "developer": "YourName",
      "publisher": "YourTeam",
      "changelog": "",
      "system": false,
      "size": 10000,
      "port": "9300",
      "type": "standard",
      "forceupgrade": false,
      "ext": {}
    },
    "frontend": {
      "title": "设备信息",
      "desc": "查看 NAS 与硬件状态",
      "icon": "/sysinfo.icon",
      "type": "url",
      "dev_type": [1, 2, 3, 4],
      "url": {
        "dev_type": [1, 2, 3, 4],
        "url": "/index.html"
      },
      "sortid": 20,
      "widget": []
    }
  }
}
```

注意：上面是结构示例，实际 `.list` 里如果已有其他插件，只在自己的插件 key 下追加对象，不要覆盖整个文件。

### 5.3 关键字段

| 字段 | 作用 |
| --- | --- |
| `status` | `running` 等运行状态 |
| `install` | 是否已安装 |
| `upgrade` | 是否可升级 |
| `enable` | 是否启用 |
| `changetime` | 变更时间戳 |
| `icon` | 图标路径，如 `/icon/<plugin>.icon` |
| `progress` | 安装进度字符串 |
| `resource.mpk` | **必须指向本地 INFO 文件，不能为空** |
| `resource.icon` | 图标资源 |
| `resource.preview` | 预览图数组 |
| `info` | 必须完整，并与 INFO 一致 |
| `frontend` | App 打开插件的方式 |
| `frontend.url.url` | 推荐 `"/index.html"` 或空串 `""` |
| `frontend.dev_type` | 设备类型数组，示例为 `[1,2,3,4]` |
| `sortid` | 排序 |
| `widget` | 组件配置，没有就空数组 |

### 5.4 致命坑

- `info` 对象缺失：可能让**整个应用市场变空**，不只是当前插件不显示。
- `resource.mpk` 为空字符串：同样可能导致列表异常。
- `resource.mpk` 必须指向真实的本地 INFO。
- `url` 写成 `"./"`：App 可能拼出 `/plugin/<id>/<plugin>./` 这类畸形路径。
- 修改后不验证：可能插件不显示，或整个列表异常。

### 5.5 验证

修改后执行（具体二进制名以固件为准）：

```sh
plugincenter -u u<uid> list | grep <plugin>
```

返回列表中包含新插件，并返回成功码，才继续下一步。笔记配图中该命令的字样略有 OCR 歧义，可能是 `plugincenter` 或类似名称；先执行 `command -v plugincenter`，以实际固件命令为准。

## 6. UI 部署

App 通过固定路径读取 UI：

```text
/data/plugin/www/<uid>/<plugin>/...
```

把源码里的 `ui/` 链接到该位置：

```sh
ln -sfn /nas/pool0/<uid>/plugin/pluginsrc/<plugin>/ui \
  /data/plugin/www/<uid>/<plugin>
```

把图标复制到 icon 目录，并重命名为 `.icon`：

```sh
cp /nas/pool0/<uid>/plugin/pluginsrc/<plugin>/ui/icon.png \
  /data/plugin/www/icon/<plugin>.icon
```

图标要求：

- 300 × 300 像素
- PNG，建议 RGBA
- 文件名必须是 `<plugin>.icon`
- 路径 `/data/plugin/www/icon/`

验证：

```sh
ls -l /data/plugin/www/<uid>/<plugin>
ls -l /data/plugin/www/icon/<plugin>.icon
nginx -t && nginx -s reload
```

浏览器访问：

```text
https://NAS:443/plugin/<uid>/<plugin>/index.html
```

注意：这里访问的是 NAS 内置的 443 入口，和公网 Caddy 的 8443 不是同一条链路。

## 7. App URL 与 Nginx

### 7.1 URL 规则

正确路径：

```text
https://NAS:443/plugin/<uid>/<plugin>/...
```

不是：

```text
https://NAS:443/plugin/u<uid>/<plugin>/...
```

App 拼接 UID 时通常不带 `u`，但文件系统里的 `.list` 文件名和部分目录带 `u`。这是最容易混淆的地方。

`.list` 的 `url` 字段：

| 写法 | 结果 |
| --- | --- |
| `"/index.html"` | 正确，指向插件根下的 index.html |
| `""` | 正确，访问插件根目录 |
| `"./"` | 错误，可能拼出畸形路径 |
| `"api/info"` | 相对插件根，通常可行 |

推荐使用 `"/index.html"` 或空串。

### 7.2 Nginx 配置位置

```text
/etc/nginx/conf.d/luci/plugincenter.conf
```

先备份：

```sh
cp /etc/nginx/conf.d/luci/plugincenter.conf \
  /etc/nginx/conf.d/luci/plugincenter.conf.bak-$(date +%F)
```

### 7.3 顺序规则

- 自定义正则 `location` 必须写在 `location /plugin` 前面。
- 正则 `location` 不要嵌套在 `location /plugin` 内部。
- 多个插件规则建议按业务分组，追加在文件顶部。

错误顺序：

```nginx
location /plugin { ... }
location ~ ^/plugin/u?<uid>/<plugin>/api/ { ... }
```

正确顺序：

```nginx
location ~ ^/plugin/u?<uid>/<plugin>/api/ { ... }
location /plugin { ... }
```

### 7.4 API 反代示例

```nginx
location ~ ^/plugin/u?<uid>/<plugin>/api/(.*)$ {
    proxy_pass http://127.0.0.1:<port>/api/$1;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-Proto https;
    add_header Access-Control-Allow-Origin "*" always;
    add_header Cache-Control "no-store";
}
```

适合：后端提供 REST API，静态 UI 仍由 `plugin.cgi` 提供。

### 7.5 完整劫持第三方 Web App

```nginx
location ~ ^/plugin/u?<uid>/<plugin>/(.*)$ {
    proxy_pass http://127.0.0.1:<port>/$1;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto https;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_read_timeout 300s;
}
```

适合：Portainer 这类需要根路径、静态资源和 WebSocket 的 Web App。

### 7.6 静态文件 alias

```nginx
location ~ ^/plugin/u?<uid>/<plugin>/((?!api/).*)$ {
    alias /data/plugin/www/<uid>/<plugin>/$1;
    default_type text/html;
}
```

这个写法会绕过 `plugin.cgi` 的登录态，只建议用于可信的本地静态页面或经过额外认证的第三方嵌入。不要默认把内部管理页面直接暴露出去。

以上三种规则应按场景选择，不要无条件同时叠加。同一个插件既需要 API 反代又需要 UI 静态文件时，API 正则应放在完整劫持正则之前；Nginx 正则匹配按配置顺序生效，不是按“最长匹配”生效。改完后用 `nginx -T` 查看最终展开的配置，再逐条验证。

### 7.7 改完必须验证

```sh
nginx -t
nginx -s reload
```

检查：

- API 返回 200，数据格式正确。
- UI 页面不是 403 / 404。
- WebSocket 能连接。
- `/var/log/nginx/error.log` 没有新错误。

## 8. `plugin.cgi` 入口

`plugin.cgi` 是 NAS 原生 FastCGI 二进制，负责：

- 校验 NAS 登录态；无效 token 返回 403。
- 注入 `FILE_URI`、`FILE_REQ`、`REQUEST_METHOD`、`QUERY_STRING` 等环境变量。
- 执行插件自己的 `<plugin>.cgi` 脚本并返回响应。

静态服务型插件必须提供 `ui/<plugin>.cgi`。

### 8.1 环境变量

| 变量 | 说明 |
| --- | --- |
| `FILE_URI` | 相对路径，如 `api/info`、`css/style.css` |
| `FILE_REQ` | 绝对文件路径 |
| `REQUEST_METHOD` | GET / POST 等 |
| `QUERY_STRING` | 查询参数，不含 `?` |

### 8.2 入口脚本模板

下面是一个可读性较好的 POSIX sh 版本，重点是逻辑，不是唯一实现：

```sh
#!/bin/sh
set -u

URI="${FILE_URI:-}"
REQ="${FILE_REQ:-}"

case "$URI" in
  api/*)
    printf 'Content-Type: application/json\r\n\r\n'
    curl -sS --max-time 5 "http://127.0.0.1:<port>/$URI"
    exit 0
    ;;
esac

if [ -f "$REQ" ]; then
  ext="${REQ##*.}"
  case "$ext" in
    png)  mime="image/png" ;;
    css)  mime="text/css" ;;
    js)   mime="application/javascript" ;;
    html) mime="text/html" ;;
    *)    mime="application/octet-stream" ;;
  esac
  printf 'Content-Type: %s\r\n\r\n' "$mime"
  cat "$REQ"
else
  printf 'Status: 404 Not Found\r\nContent-Type: text/html\r\n\r\n'
  printf '<h1>404 Not Found</h1>'
fi
```

注意：

- 原图模板使用 `echo -e`；不同 `/bin/sh` 对 `-e` 支持不一致，建议用 `printf`。
- HTTP 头必须使用 CRLF。
- `chmod +x <plugin>.cgi`。
- 脚本 UTF-8 无 BOM。
- 不要输出调试信息或多余空行。
- 路径以 `FILE_REQ` 为准，不要硬编码静态文件路径。

安全补充：

- 不要把 `FILE_URI` 直接拼进不受控的本地 URL；应限制方法、路径和参数。
- 对 `api/*` 做白名单，避免把任意路径转发到本地服务。
- 不要让入口脚本执行用户可控的 shell 字符串。
- 静态文件路径要防止 `..` 路径穿越。

## 9. 本地服务与 systemd

推荐用 systemd 托管本地 HTTP 服务，而不是用 `nohup` 做长期后台进程。

### 9.1 systemd 示例

```ini
[Unit]
Description=<plugin> service
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /nas/pool0/<uid>/plugin/pluginsrc/<plugin>/files/httpd.py
Restart=on-failure
RestartSec=5
Environment=PLUG_PORT=<port>
Environment=PLUG_DATA=/tmp/<plugin>
StandardOutput=append:/tmp/<plugin>/server.log
StandardError=append:/tmp/<plugin>/server.log

[Install]
WantedBy=multi-user.target
```

部署：

```sh
mkdir -p /tmp/<plugin>
systemctl daemon-reload
systemctl enable <plugin>.service
systemctl start <plugin>.service
systemctl status <plugin>.service
```

### 9.2 `files/server.sh`

如果 `scripts/control` 通过 `files/server.sh` 控制服务，至少要支持：

```sh
#!/bin/sh
PORT="${PLUG_PORT:-<port>}"
PIDFILE="/tmp/<plugin>/server.pid"

start() {
  nohup python3 /path/to/httpd.py >> /tmp/<plugin>/server.log 2>&1 &
  echo $! > "$PIDFILE"
}

stop() {
  [ -f "$PIDFILE" ] && kill "$(cat "$PIDFILE")" 2>/dev/null || true
  rm -f "$PIDFILE"
}

status() {
  [ -f "$PIDFILE" ] && echo running || echo stopped
}

restart() {
  stop
  start
}

case "${1:-}" in
  start|stop|status|restart) "$1" ;;
  *) echo "usage: $0 start|stop|status|restart" >&2; exit 2 ;;
esac
```

重要区别：

- systemd 的 `ExecStart` 应直接运行前台服务。
- 不要让 systemd 启动一个脚本，而脚本又把主进程 `nohup` 到后台然后退出；systemd 会认为服务已经结束。
- `server.sh` 更适合手动控制或 `scripts/control` 兼容，不建议和 systemd 同时管理同一个进程。

### 9.3 踩坑

- 日志目录必须先创建，否则可能因目录不存在退出。
- 避免使用官方常用端口，如 9000、9100、9200；9300 只是示例，也要检查冲突。
- 端口变化时同步修改 INFO、systemd、Nginx 和前端 API 地址。
- 服务启动后检查 `systemctl status`、端口监听和日志。

## 10. `scripts/control` 生命周期

目录规范中明确要求 `scripts/control`。笔记配图只明确列出了 `enable`、`disable`、`postinstall` 等动作，完整动作集合需按固件确认。

建议把它写成幂等、无交互、可日志追踪的脚本：

```sh
#!/bin/sh
set -eu

PLUGIN_DIR=/nas/pool0/<uid>/plugin/pluginsrc/<plugin>

case "${1:-}" in
  enable|start)
    "$PLUGIN_DIR/files/server.sh" start
    ;;
  disable|stop)
    "$PLUGIN_DIR/files/server.sh" stop
    ;;
  status)
    "$PLUGIN_DIR/files/server.sh" status
    ;;
  postinstall)
    mkdir -p /tmp/<plugin>
    ;;
  *)
    echo "unsupported action: ${1:-}" >&2
    exit 2
    ;;
esac
```

原则上：

- `enable/disable`、`postinstall` 等动作要能重复执行。
- 失败返回非零。
- 不依赖交互式终端。
- 不把密码、token 写入日志。
- 升级和卸载要能清理临时文件，但不要误删用户配置。

## 11. 安全与合规

### 11.1 运行权限

- 笔记中的插件服务可能以 root 运行，代码必须审查，不能留后门。
- 能降权就降权；必须 root 时，只开放必要能力。
- 不要把本地管理接口直接暴露到公网。

### 11.2 鉴权链路

- 443 入口有 mTLS 和登录态；没有客户端证书时可能返回 400。
- `plugin.cgi` 会校验登录 token，token 无效返回 403。
- 第三方 App 通过 Nginx 反代或静态 alias 绕过 `plugin.cgi` 时，必须自行补认证和访问控制。

### 11.3 官方安装通道

- 官方 plugincenter 安装通道存在 RSA 验签，不能直接用自研包绕过。
- 本规范描述的是手动部署路径，适合开发和自用，不应被理解为绕过官方安全机制。
- `plugincenter update` 在某些实测中不会覆盖手动注册条目，但云端策略可能变化，升级前要重新检查。

### 11.4 固件升级

固件升级可能重置 `/etc`、systemd、Nginx 配置。至少备份：

- `/data/plugin/u<uid>.list`
- `/etc/nginx/conf.d/luci/plugincenter.conf`
- 插件的 systemd unit
- 两处 `INFO`
- 插件源码和 UI
- 必要的系统配置（如 shadow 等，按实际改动范围）

建议保留一套可重复执行的部署脚本，升级后重新部署并验收。

### 11.5 常见安全风险

- 路径穿越：`FILE_URI`、静态文件路径必须规范化。
- 命令注入：不要把用户输入拼进 `sh -c`。
- SSRF：API 反代不要把任意路径转发到本地服务。
- 越权：静态 alias 绕过登录态时，必须另加认证。
- 密钥泄露：token、密码、私钥不要写进前端或日志。
- 端口暴露：只监听 `127.0.0.1` 或必要的内网地址，不要直接监听公网。

## 12. 测试与验收

### 12.1 测试环境

- 测试 NAS，最新稳定固件，开发者模式已开启。
- 插件包、UI、`plugin.cgi`、`plugincenter` 配置、文件权限齐全。
- Nginx、plugincenter 正常。
- 浏览器、API 工具、SSH 日志查看工具可用。
- 测试前重启 Nginx 和 plugincenter，确保环境干净。

### 12.2 功能测试

| 项目 | 预期 |
| --- | --- |
| 应用市场显示插件卡片 | 正常显示，无报错 |
| 点击安装 | 安装成功 |
| 点击入口 | UI 正常加载 |
| API 请求 | 200，JSON 格式正确 |
| CSS / JS / 图片 | 200，无 404 |
| 修改配置后重启 | 配置保留 |
| 断网、权限不足、文件缺失 | 友好报错，不崩溃 |
| 卸载 | 文件和临时数据清理干净 |

### 12.3 API 示例

```http
GET https://NAS:443/plugin/<uid>/<plugin>/api/info
```

```json
{
  "code": 0,
  "message": "success",
  "data": {
    "name": "插件名称",
    "version": "1.0.0",
    "status": "running"
  }
}
```

### 12.4 日志

```sh
cat /mnt/logs/plugin/<uid>/error.log
cat /var/log/nginx/error.log
dmesg | tail -50
```

重点看错误、警告、权限问题和性能问题。

### 12.5 验收清单

- `plugincenter -u u<uid> list` 返回新插件，成功码为 0。
- App 应用市场显示插件。
- 点击插件能正常加载 UI，不是空白页。
- API 经 Nginx 或 `plugin.cgi` 返回正确数据。
- 图标显示正常，尺寸 300×300。
- systemd 服务 `active` 且 `enabled`。
- 重启 NAS 后插件仍可用。
- 备份齐全，能回滚。

笔记给出的性能目标：

- 页面加载 < 3 秒。
- API 响应 < 2 秒。

## 13. 发布与版本管理

如果是自用插件，至少也要做版本管理：

- 使用语义化版本：`MAJOR.MINOR.PATCH`。
- 修复 Bug：PATCH + 1。
- 新增功能：MINOR + 1。
- 不兼容变更：MAJOR + 1。
- 每次更新同步修改 INFO、`.list`、changelog 和前端显示版本。
- 升级前备份用户配置。
- 数据或配置结构变化时提供迁移脚本。
- 升级失败要能回滚。

如果走官方应用商店，笔记还列出：

- 应用名称不超过 30 个字符。
- 商店图标 512×512 PNG。
- 至少 3 张截图，推荐 1280×720。
- 描述不超过 500 字。
- 隐私声明和权限说明。
- 审核时间约 1–3 个工作日。

注意：商店上架要求和手动部署的运行时要求不是一回事。运行时图标是 300×300 的 `<plugin>.icon`，商店图标是 512×512 的素材。

## 14. 手动部署 Runbook

### 14.1 备份

```sh
cp /data/plugin/u<uid>.list /data/plugin/u<uid>.list.bak-$(date +%F)
cp /etc/nginx/conf.d/luci/plugincenter.conf \
  /etc/nginx/conf.d/luci/plugincenter.conf.bak-$(date +%F)
```

再备份现有插件目录、两处 INFO、systemd unit 和图标。

### 14.2 部署

1. 创建源码目录和 `files/`、`scripts/`、`ui/`。
2. 写入两处 INFO，确保 JSON 合法且内容一致。
3. 写入 UI、`<plugin>.cgi`、图标。
4. 创建 `/data/plugin/www/<uid>/<plugin>` 软链接。
5. 复制图标到 `/data/plugin/www/icon/<plugin>.icon`。
6. 写入 `.list` 条目，`resource.mpk` 指向 INFO。
7. 写入 Nginx 规则，自定义正则放在 `location /plugin` 前。
8. 写入 systemd unit，`daemon-reload`、`enable`、`start`。
9. 执行 `nginx -t && nginx -s reload`。
10. 执行 `plugincenter -u u<uid> list | grep <plugin>`。
11. 浏览器访问 UI 和 API。
12. 重启 NAS，验证服务自启动和插件仍可用。

### 14.3 回滚

1. 恢复 `.list` 和 Nginx 配置备份。
2. `nginx -t && nginx -s reload`。
3. `systemctl disable --now <plugin>.service`。
4. 删除 `/data/plugin/www/<uid>/<plugin>` 软链接和图标。
5. 重启 plugincenter，确认应用市场恢复正常。
6. 保留日志和故障现场，再定位问题。

## 15. 需要按固件确认的点

以下内容在笔记配图中存在路径或拼写歧义，不能直接当成所有固件的固定值：

- UID 目录到底使用 `<uid>` 还是 `u<uid>`；不同路径前缀可能不同。
- `plugincenter` 的准确二进制名和子命令。
- `scripts/control` 的完整动作集合和参数。
- 443 mTLS 的客户端证书要求。
- `/home/<uid>` 与 `/home/u<uid>` 的实际布局。
- `resource.mpk` 的路径是否允许软链接。
- 官方固件升级后是否保留手动 `.list` 条目。

部署前用 `command -v`、`ls -ld`、`systemctl cat` 和现有官方插件目录做一次现场核对。

## 16. 来源与提醒

- 笔记标题：小米 NAS 插件开发规范｜手动部署实测总结
- 作者：Jray
- 原文：https://xhslink.cn/o/5Q4f5qKs0YX
- 已阅读正文和 13 张配图。

再次提醒：这是第三方手动部署实测整理，不是官方接口承诺。涉及 root、`/etc`、Nginx、systemd 和 plugincenter 的修改，先备份、先测试、再上线；不要绕过官方安装通道的 RSA 验签。
