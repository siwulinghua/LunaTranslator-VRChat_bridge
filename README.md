# LunaTranslator 与 VRChat 桥接器 - 实现实时语音翻译

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

将 [LunaTranslator](https://github.com/HIllya51/LunaTranslator) 的实时翻译，通过 OSC 发送到 VRChat 聊天框。

## 功能

- 🎤 获取LunaTranslator 实时语音翻译的翻译结果
- 📡 通过 WebSocket 接收原文和译文
- 🎮 自动转发到 VRChat 聊天框（OSC）
- 🖥️ 图形界面：配置、启动、日志一目了然

## 语音识别说明

- 本桥接器依赖 LunaTranslator 的语音识别和翻译功能。
- **音频来源**：建议在 LunaTranslator 中设置为「回环录制」以捕获系统音频（如 VRChat 的声音），或者设置为「麦克风」以捕获麦克风音频。如见不到音频来源设置，请看[此文章](https://www.bilibili.com/opus/1085051989043183623)

## 使用方法

### 下载 Release（推荐）

从 [Releases](https://github.com/siwulinghua/LunaTranslator-VRChat_bridge/releases) 下载 `LunaVRC_Bridge_v0.9.zip`，解压后运行 `LunaVRC_Bridge.exe`。

### 从源码运行

```bash
pip install -r requirements.txt
python bridge_ui.py
```

## 操作流程

1. 选择 LunaTranslator.exe 路径
2. 点击「读取接口信息」自动填充 WebSocket 地址
3. 点击「启动 LunaTranslator」一键启动翻译器并桥接

## 配置

编辑 `config.json`：

| 字段 | 说明 | 默认值 |
|------|------|--------|
| `luna_ws_origin` | LunaTranslator 原文 WebSocket | `ws://127.0.0.1:2333/api/ws/text/origin` |
| `luna_ws_trans` | LunaTranslator 译文 WebSocket | `ws://127.0.0.1:2333/api/ws/text/trans` |
| `vrchat_osc_ip` | VRChat OSC IP | `127.0.0.1` |
| `vrchat_osc_port` | VRChat OSC 端口 | `9000` |
| `chatbox_max_len` | 聊天框 最大字节数 | `140` |
| `include_origin` | 是否输出原文 | `true` |

## 依赖

- Python 3.10+
- [websockets](https://pypi.org/project/websockets/)
- [python-osc](https://pypi.org/project/python-osc/)
- [customtkinter](https://pypi.org/project/customtkinter/)

## 开源协议

MIT License — 详见 [LICENSE](LICENSE) 文件。
