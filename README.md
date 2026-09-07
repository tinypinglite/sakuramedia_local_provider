# SakuraMedia 本地存储与 qBittorrent

把本地目录接入 SakuraMedia 的媒体库，并提供手动导入、qBittorrent 下载、播放、缩略图和片段。

## 安装

在 SakuraMedia 的「系统设置 → 插件」点击「安装插件」，上传本插件的 `.zip` 包；安装后确认插件已启用，再重启 SakuraMedia 容器。插件的安装、启停、删除或插件配置修改后，前端都会提示需要重启容器才生效。

## 创建本地媒体库

在「系统设置 → 媒体库」点击「新增媒体库」，将「存储 Provider」设为「本地存储与 qBittorrent」，再填写：

- 名称：媒体库在 SakuraMedia 中的显示名称。
- 媒体库路径：影片导入后的最终目录，填写已挂载到 SakuraMedia 容器内的路径，例如 `/mnt/sakuramedia-media`。
- 手动导入根目录：浏览并导入已有本地文件的目录，填写已挂载到 SakuraMedia 容器内的路径，例如 `/mnt/sakuramedia-import`。
- 文件名黑名单：可选，每行一个关键字。文件名包含任一关键字的文件不会导入，匹配不区分大小写，例如 `sample` 或 `trailer`。

这里填写的是 SakuraMedia 容器内的挂载路径，不是宿主机路径。编辑已有媒体库时不能改存储 Provider；需要换 Provider 时，请新建媒体库后再迁移。

## 配置 qBittorrent 下载器

先创建上述本地媒体库，再在「系统设置 → 下载器」点击「新建下载器」，选择该媒体库作为「目标媒体库」。下载器不单独选择 Provider，选择媒体库后才会显示 qBittorrent 的配置字段：

- qBittorrent 地址、用户名、密码：qBittorrent WebUI 的连接信息。
- qBittorrent 保存根目录：qBittorrent 进程看到的下载目录，例如 `/downloads`。
- 后端下载根目录：SakuraMedia 后端容器看到的同一物理目录，填写已挂载到容器内的路径，例如 `/mnt/sakuramedia-downloads`。

两个下载根目录必须映射到同一个物理目录，路径字符串可以不同，例如 qBittorrent 中的 `/downloads` 对应 SakuraMedia 容器中的 `/mnt/sakuramedia-downloads`。点击「测试配置」会检查 qBittorrent 连接和认证、目录映射及硬链接能力；硬链接不可用时会显示警告，仍可保存，导入时会回退为复制。测试未通过也可以保存，但后续下载或导入可能失败。编辑下载器时，密码留空表示保留原值。

## 导入与播放

手动导入只浏览「手动导入根目录」，qBittorrent 完成后的文件只从「后端下载根目录」读取，两种来源不能互换。导入时保留源文件会优先建立硬链接，不能建立时复制；选择导入后删除源文件时，在提交成功后删除源文件。删除 SakuraMedia 中的下载器不会删除 qBittorrent 中已有的下载任务。

本地存储仅支持「后端代理」播放，因此影片详情的「媒体源」不会显示「302直连」切换。
