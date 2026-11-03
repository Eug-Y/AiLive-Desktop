# NOTICE

AI 数字人桌面端（AiLive-Desktop）
Copyright 2026 AiLive-Desktop contributors

本产品以 Apache License 2.0 发布，详见同目录 [LICENSE](LICENSE)。

---

## 一、包含的上游作品

本项目的数字人引擎部分（`LiveTalking/` 目录）派生自：

- **LiveTalking** — <https://github.com/lipku/LiveTalking>
  （国内镜像 <https://gitee.com/lipku/LiveTalking>）
  许可：Apache License 2.0

该上游作品在本仓库中**保留了原始的 `LiveTalking/LICENSE` 与 README**，并在其上做了本地定制
（桌面宿主所需的页面路由收口、账号与会员体系、TTS 音色管理、WebRTC/WHEP 推流、界面外壳等）。
按 Apache-2.0 第 4 条，上述派生文件同样以 Apache-2.0 提供。

上游 README 中列出的模型权重（Wav2Lip / MuseTalk / ERNERF / Ultralight-Digital-Human）
**不包含在本仓库内**，其许可与下载方式以上游说明为准。

## 二、主要第三方依赖

| 依赖 | 用途 | 许可 |
| --- | --- | --- |
| [pywebview](https://pywebview.flowrl.com/) | 桌面窗口（WebView2 宿主） | BSD-3-Clause |
| [PyInstaller](https://pyinstaller.org/) | 打包为 exe | GPL-2.0（含打包例外条款） |
| [Flask](https://flask.palletsprojects.com/) | HTTP 服务 | BSD-3-Clause |
| [aiortc](https://github.com/aiortc/aiortc) | WebRTC 推流 | BSD-3-Clause |
| [PyTorch](https://pytorch.org/) | 模型推理 | BSD-3-Clause |
| [edge-tts](https://github.com/rany2/edge-tts) | 语音合成 | GPL-3.0 |
| [Pillow](https://python-pillow.org/) | 图标生成 | MIT-CMU |
| [cloudflared](https://github.com/cloudflare/cloudflared) | 外网分享（未随仓库分发） | Apache-2.0 |

依赖的许可以其各自发行版中的 LICENSE 为准；完整依赖列表见 `LiveTalking/requirements.txt`。

## 三、素材说明

- `docs/images/` 下的截图为本项目运行时的界面截图。
- `LiveTalking/web/` 与 `yamy_25d/` 中的图片、音频为演示用素材，版权归其各自所有者；
  若要商用请替换为你自己拥有权利的素材。
- 数字人形象（avatar）与克隆音色**不在仓库内**，需由使用者自行录制/生成并承担相应的肖像权与
  声音权合规责任。