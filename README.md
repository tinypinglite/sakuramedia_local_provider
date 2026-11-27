# SakuraMedia 开源插件

SakuraMedia 官方开源插件的源码仓库：源码公开，欢迎审阅和提交 PR。插件包（编译后的 zip）发布在本仓 Releases，并通过 [SakuraMedia 插件市场](https://tinypinglite.github.io/sakuramedia-plugin-market/) 分发。

## 插件

| 插件 | plugin_id | 说明 |
| --- | --- | --- |
| 本地存储与 qBittorrent | `sakuramedia_local_provider` | 接入本地媒体目录，提供手动导入、qBittorrent 下载、后端代理播放、缩略图和片段。[说明](sakuramedia_local_provider/README.md) |

## 安装

在 SakuraMedia 的「系统设置 → 插件」里从插件市场安装，或下载本仓 Releases 里的 zip 手动安装，安装后重启 API 和 APS 服务。

## 目录结构

- 每个插件一个目录，目录名即 `manifest.json` 里的 `plugin_id`
- `i18n/`、`prompts/`、`LICENSE`、`README.md` 等附属文件随插件目录一起打包
- `.github/` 是发版流水线，不参与打包

## 发版

维护者流程（新插件第一次发版按这个顺序，顺序错了市场同步会报错）：

1. 修改插件 `manifest.json` 里的 `version`（新插件先落到 main）
2. 打 tag `<plugin_id>-v<version>` 并发布 Release
3. 流水线在 amd64、arm64 上编译为 `.so`，做加载校验后打包成 `zip` + `.sha256` 传到该 Release
4. 需要让市场收录时，再往 `sakuramedia-plugin-market` 提交 `plugins/<plugin_id>.json`（`source: external`、`repo` 指向本仓）；之后每次发版都会自动同步，客户端即可看到新版本

## 参与贡献

- 代码风格、目录结构和 `manifest.json` 字段要保持与现有插件一致；发版校验会检查 `plugin_id`、`version`、`host_api_version`、`requires_python` 和 `release_api_url`
- 插件测试在各插件目录的 `tests/` 下，用 pytest 运行，需要 Python 3.10、插件依赖，以及把 `SAKURAMEDIA_HOST_ROOT` 指向 [SakuraMediaBE](https://github.com/tinypinglite/sakuramediabe) 根目录
- 提交前请只改与本次改动相关的文件，不要顺手重排或格式化无关代码

## 许可证

[PolyForm Noncommercial License 1.0.0](LICENSE)

Copyright (C) 2026 tinypinglite
