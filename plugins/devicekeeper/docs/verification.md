# 验收记录

验收日期：2026-10-30（Asia/Shanghai）；桌面客户端入口复测：2026-10-31。

对象：一台四核 aarch64 小米 NAS，固件 `1.0.9.462` 系列。本文不记录真实主机名、内网地址、Tailscale 名称、Mi UID 或凭据。

## 结果

最终安装后验收 **72/72 通过**，单元测试 18/18 通过。

## 检查项

| 类别 | 检查 | 结果 |
| --- | --- | --- |
| 插件中心 | `plugincenter info` 返回 `code=0` | 通过 |
| 插件中心 | `status=running`、`install=true`、`enable=true`、进度 100 | 通过 |
| 注册表 | 只新增 `devicekeeper`，原有条目逐项保持不变 | 通过 |
| 应用入口 | admin 应用列表可见，普通用户列表不可见 | 通过 |
| 静态资源 | 首页、CSS、JS、图标均返回 200 | 通过 |
| API | health、snapshot、history、events 返回 200 | 通过 |
| 权限 | 普通用户直链首页和 API 返回 403，缺少 `MIUID` 返回 403 | 通过 |
| 认证 | 未登录 HTTPS 入口返回 400 | 通过 |
| 路径安全 | `../INFO` 等穿越路径返回 404 | 通过 |
| 端口 | 仅监听 `127.0.0.1:9365`，局域网访问拒绝 | 通过 |
| 容器 | `running`、`healthy`、重启次数 0 | 通过 |
| 健康检查 | 最近 5 次探针退出码均为 0 | 通过 |
| 宿主探针 | timer active，service result success，错误列表为空 | 通过 |
| 数据 | CPU、内存、温度、风扇、网络、存储池、Docker、systemd 可用 | 通过 |
| 持久化 | SQLite `integrity_check=ok`，采样记录持续增长 | 通过 |
| 现有插件 | central、ipc、mediacenter 保持 running/enabled | 通过 |
| 安全配置 | 只读 rootfs、非 root、cap-drop ALL、无 Docker Socket 挂载 | 通过 |

## 修复记录

### 桌面客户端入口

小米智能存储桌面端在启动时缓存 `get_app_list`，只会动态刷新
`plugin_list`。如果插件安装时客户端已经打开，“全部应用”里能看到图标，
但点击时 `gl.allApps` 中还没有该条目，页面会抛出
`Cannot read properties of undefined (reading 'id')`，表现为点击无反应。
安装后重启一次桌面客户端即可让 `get_app_list` 返回新插件。

桌面端还会把 iframe 路径带成 `/D:/plugin/...`，导致相对 API 请求变成
`/D:/plugin/.../api/v1/...`。现在 UI 从 `/plugin/<用户>/<插件>/` 路径段
推导 API 根路径，手机端和桌面端共用同一套逻辑。

桌面端微应用容器不会自动裁剪 `position: fixed` 的侧边栏。侧边栏现在改为
容器内网格布局，工作区在弹窗内部滚动，不再延伸到弹窗外侧。

### UI 目录定位

固件 `plugin.cgi` 通过 `fexecve` 启动子 CGI，子进程的 `$0` 为 `/dev/fd/3`。原实现使用 `dirname "$0"` 推导 UI 目录，导致首页、CSS 和 JS 返回 404。现在优先从 `REQUEST_URI` 还原 `/data/plugin/www/<user>/<plugin>`。

### 直链权限

插件列表的 `permission` 只负责隐藏入口。普通用户知道直链时仍可能访问 API。现在 CGI 从 `MIUID` 取得登录用户，查询 UCI `user.<user>.permission`，非 `admin` 返回 403。

## 边界

- 验收使用与 `plugin.cgi` 相同的 `USER`、`MIUID`、`REQUEST_URI` 环境执行真实固件入口；米家 App 内的实际视觉点击仍需在目标手机上确认。
- 低负载时固件风扇读数可能为 0 RPM。
- 新装后 1 小时历史覆盖率会从较低值逐步增长。
- 固件升级可能改变 Lua 函数、字段和 Docker API，需要重新运行验收。
