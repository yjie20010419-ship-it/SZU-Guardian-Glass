# 校园网守护 · Glass 版（SZU Network Guardian Glass）

[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Windows](https://img.shields.io/badge/Windows-10%20%7C%2011-0078D4?logo=windows11&logoColor=white)](https://www.microsoft.com/windows)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

深圳大学校园网断线监控与自动重连工具的 Qt 界面版，并会在重新认证后重启 UU 远程。
基于 [Georgeupup/szu-network-guardian](https://github.com/Georgeupup/szu-network-guardian)，界面从 Tk 改为 PySide6，并加入了下面这些功能。

## 功能

**校园网**
- 教学/办公区使用深澜 SRun challenge 认证，宿舍区使用 ePortal 认证。
- 每次检测都会先向认证服务器核验会话。即使百度等白名单网页还能打开，只要会话失效，也会立即重新认证。
- 只有认证掉了才重新登录。认证在线但外网不通时（学校出口或外部网站的问题），只记录、不登录，因为登录解决不了这种问题。
- 外网检测用微软、华为、vivo 三家的联网检测地址，三个里两个通过就算正常，单个网站抽风不会误判为断网；不跟随重定向，认证门户的拦截页也不会被误认成“已联网”。
- 检测失败后按 30 秒、1 分钟、2 分钟的节奏快速复查，恢复后再回到正常间隔。
- 联网检测和认证都绑定在校园网物理网卡上直连，不会被 Clash、FlClash 等代理的 TUN / Fake-IP 干扰。直连时 TCP 连接发往校园 DNS 解析出的 IP，但 HTTPS 仍按域名完整校验证书。SRun 登录报文里的密码，只用服务器下发的 challenge 加密，一旦被截获就能还原，所以证书校验失败时会直接停止认证，绝不会发出密码。
- 网卡地址一变化（插拔网线、WiFi 重连、DHCP 续租）就立即检测，不用等到下一个检测间隔。
- 从睡眠恢复后，重新建立网卡绑定并立即检测。
- 保存设置后立即生效，不需要暂停再重新开始。

**诊断**
- 日志里会记录认证服务器返回的状态码、账号的在线设备列表（有变化时才记），以及登录后的复查结果。复查结果能区分“登录后又被其他设备挤下线”和“认证正常但外网不通”。
- 网络正常时，每小时只写一句汇总；掉线、重连、设备变化等事件会立即写入。
- 记录页可以查看当天的完整记录（包括重启前的），还有掉线统计：今天掉线几次、累计多久、最长一次，以及近 7 天的情况。

**UU 远程**
- 重新认证成功后，重启 UU 远程的主程序和后台服务（需要先授权一次）。
- 重启后确认 `GameViewerServer.exe` 已经连上服务器；60 秒内没连上，就再重启一次。

**界面**
- 状态图标：在线时绿色“呼吸”，检测时转圈，掉线时变红；外圈显示距下次检测的进度。
- 五种主题色，也可以自定义颜色。换色有过渡动画，Windows 标题栏跟着一起变色。
- 页面淡入、导航滑块、开关和按钮悬停都有动画。窗口隐藏时动画停止，不占用 CPU。
- 托盘图标的小圆点表示状态：绿色正常，红色掉线，橙色检测中，灰色已暂停。
- 只能运行一个实例：再次打开 exe 时，会切换到已经在运行的窗口。

## 使用

1. 把 `SZU-Guardian-Glass.exe` 放到一个长期保留的位置，然后双击运行。
2. 填写账号、统一身份认证密码，选择实际所在的网络区域，设置检测间隔（建议 3～5 分钟）。
3. 用代理软件时，保持“检测与认证强制直连”开启。
4. 点击“开始守护”。关闭窗口后，程序会留在系统托盘继续运行。
5. 如果需要自动重启 UU 远程，点击“授权完整重启”，在弹出的 Windows 提示里确认一次。

> [!IMPORTANT]
> 先把 exe 移到最终位置，再打开“开机启动”。开机启动项记录的是 exe 当前的位置。

## 文件位置

| 内容 | 位置 |
| --- | --- |
| 配置（密码用 DPAPI 加密） | `%LOCALAPPDATA%\SZUNetworkGuardian\config.json` |
| 外观 | `%LOCALAPPDATA%\SZUNetworkGuardian\appearance.json` |
| 日志（保留 7 天） | 默认 `E:\Apps\Logs\SZU-Guardian\guardian-YYYY-MM-DD.log`（没有本地 E 盘时为 `%LOCALAPPDATA%\SZUNetworkGuardian\logs`）；在“设置”页可以修改，立即生效，旧日志会一起移过去 |
| 运行时解压目录 | 用户环境变量 `SZU_GUARDIAN_CACHE`，默认是 `E:\Apps\Cache\SZU-Guardian`（没有 E 盘时放在 `%LOCALAPPDATA%` 下）；在“设置”页可以修改 |

exe 是单文件程序，每次启动都会先解压到运行时目录，正常退出时自动删除。如果程序是被强制结束的，残留文件会在下次启动时自动清理，也可以在设置页手动清理。

## 从源码构建

需要 Windows x64 和 Python 3.10 以上版本（当前用 3.14 构建）。

```powershell
.\build.ps1            # 第一次运行：创建 .venv-build、安装依赖、跑测试、打包
.\build.ps1 -SkipInstall
```

打包配置写在 `guardian.spec` 里：单文件、解压到 `%SZU_GUARDIAN_CACHE%`，并去掉了用不到的 Qt 组件（软件 OpenGL、翻译文件、多余插件）。成品是 `dist\SZU-Guardian-Glass.exe`，大约 30 MB。

只跑测试：

```powershell
.\.venv-build\Scripts\python.exe -m unittest discover -s tests
```

## 项目结构

```text
szu_guardian/
  network.py         检测与认证流程、诊断信息
  srun.py            SRun 协议（含在线设备解析）
  direct_network.py  物理网卡绑定与直连 DNS
  monitor.py         后台监控循环
  net_watch.py       网卡地址变化监听
  uu_remote.py       UU 远程重启与上线确认
  log_policy.py      日志精简规则
  outage_stats.py    掉线统计
  local_log.py       本地日志
  cache_dir.py       运行时解压目录
  single_instance.py 防止重复运行
  motion.py          动画控件
  ui.py              主界面
tests/               自动化测试
guardian.spec        打包配置
```

## 隐私

- 源码和构建产物里没有预设任何账号或密码；测试里的 IP 都是示例地址。
- 日志不会记录密码和认证载荷，但会记录账号在线设备的 IP，只保存在本机。
- 不要把 `config.json` 上传到公开仓库。

## 致谢

- [Georgeupup/szu-network-guardian](https://github.com/Georgeupup/szu-network-guardian)：本项目的基础。
- [ackness/szu-autoconnect](https://github.com/ackness/szu-autoconnect)、[Sleepstars/SZU-login](https://github.com/Sleepstars/SZU-login)、[vidar-team/srun-login](https://github.com/vidar-team/srun-login)：校园网认证流程和 SRun 协议的参考实现。

第三方归属和许可说明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) 和 [COMPONENTS.md](COMPONENTS.md)。

## 许可证

[MIT License](LICENSE)。仅用于维护本人账号的正常校园网连接，请遵守学校的网络管理规定。
