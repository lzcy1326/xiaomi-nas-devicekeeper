# Minas NAS Plugins

面向小米 NAS 的自托管插件集合。每个插件独立放在 `plugins/<name>/` 下，根目录只保留 NAS 通用文档、项目元数据和 CI。

[![CI](https://github.com/lzcy1326/xiaomi-nas-devicekeeper/actions/workflows/ci.yml/badge.svg)](https://github.com/lzcy1326/xiaomi-nas-devicekeeper/actions/workflows/ci.yml)

<p align="center">
  <img src="plugins/devicekeeper/docs/images/desktop-overview.png" alt="设备管家桌面端" width="82%">
</p>

## 插件

| 插件 | 说明 | 文档 | 版本 |
| --- | --- | --- | --- |
| [设备管家 Devicekeeper](plugins/devicekeeper/README.md) | 只读设备监测：CPU、内存、温度、风扇、网络、存储池、Docker 和 systemd | [README](plugins/devicekeeper/README.md) · [架构](plugins/devicekeeper/docs/architecture.md) · [验收](plugins/devicekeeper/docs/verification.md) | [v1.0.0](https://github.com/lzcy1326/xiaomi-nas-devicekeeper/releases/tag/v1.0.0) |

## 快速开始

```sh
git clone https://github.com/lzcy1326/xiaomi-nas-devicekeeper.git
cd xiaomi-nas-devicekeeper/plugins/devicekeeper
sudo DEVICEKEEPER_UID=<mi-uid> ./deploy/install-nas.sh
```

安装前请阅读对应插件的 README 和安全策略。插件默认只监听宿主回环地址，页面通过米家原生 `plugin.cgi` 登录链路访问。

## 目录结构

```text
.
├── README.md                 项目主页
├── LICENSE                   仓库许可证
├── SECURITY.md               安全策略
├── .github/workflows/        CI
├── docs/
│   └── nas/                  NAS 通用文档
└── plugins/
    ├── README.md             插件目录约定
    └── devicekeeper/         设备管家完整插件
        ├── README.md
        ├── INFO
        ├── app/
        ├── assets/
        ├── deploy/
        ├── docs/
        ├── scripts/
        ├── tests/
        └── ui/
```

## NAS 通用文档

- [小米 NAS 插件开发规范](docs/nas/plugin-development-spec.md)
- [插件目录约定](plugins/README.md)

## 开发

```sh
cd plugins/devicekeeper
python -m unittest discover -s tests -v
```

## 许可证

[MIT](LICENSE)
