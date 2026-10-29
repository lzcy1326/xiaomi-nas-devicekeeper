# Plugins

每个插件都是自包含目录：

```text
plugins/<plugin-name>/
├── README.md
├── INFO
├── Dockerfile
├── docker-compose.yml
├── app/
├── assets/
├── deploy/
├── docs/
├── scripts/
├── tests/
└── ui/
```

约定：

- 插件运行时代码、安装脚本、测试、UI、图标和插件专属文档都放在自己的目录下。
- 根目录只放 NAS 通用文档、许可证、安全策略、CI 和项目主页。
- 新插件必须提供 `README.md`、`INFO`、部署脚本和最小测试。
- 不把个人 UID、内网地址、DNS token、私钥或真实设备标识写进仓库。

当前插件：

- [设备管家 Devicekeeper](devicekeeper/README.md)
