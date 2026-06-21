"""LunaTranslator 与 VRChat 桥接器 - 核心逻辑模块

## 架构
BridgeCore 在独立线程中运行 asyncio event loop，包含 3 个协程：
  _listen_origin  ─→  latest_origin (加锁)
  _listen_trans   ─→  拼接原文 → send_queue
  _send_worker    ─→  消费 send_queue → OSC 发送到 VRChat

## 数据流
  Luna WebSocket → 原文协程/译文协程 → send_queue → OSC → VRChat Chatbox

## 配置读写
  load_config() / save_config() 操作 exe/脚本旁边的 config.json
  - 统一使用 UTF-8 编码，不依赖 BOM
  - 缺失字段自动用默认值补齐
"""
import asyncio
import json
import os
import queue
import sys
import threading

import websockets
from pythonosc import udp_client
from pythonosc.osc_message_builder import OscMessageBuilder


# ================= 路径工具 =================
def _ensure_writable(base_dir: str, app_name: str = "LunaVRC_Bridge") -> str:
    """检测目录是否可写，不可写则回退到 AppData"""
    try:
        test_path = os.path.join(base_dir, ".writable_test")
        with open(test_path, "w") as f:
            f.write("test")
        os.remove(test_path)
        return base_dir
    except (OSError, PermissionError):
        pass
    appdata = os.environ.get("APPDATA", "")
    if appdata:
        fallback = os.path.join(appdata, app_name)
        os.makedirs(fallback, exist_ok=True)
        return fallback
    return base_dir


def get_base_dir():
    """获取 config.json 的存放目录

    兼容以下打包方式：
      - Nuitka onefile  : __compiled__ + NUITKA_ONEFILE_DIRECTORY 环境变量
      - Nuitka standalone: __compiled__ + sys.argv[0]（没有临时目录）
      - PyInstaller      : sys.frozen=True + sys.executable 指向真实 exe
      - 开发模式          : __file__ 所在目录
    """
    # Nuitka（onefile / standalone 均适用）
    if '__compiled__' in globals():
        # onefile 模式：NUITKA_ONEFILE_DIRECTORY 直接指向 exe 所在目录
        # standalone 模式：该变量不存在，回退 sys.argv[0]
        d = os.environ.get('NUITKA_ONEFILE_DIRECTORY', '') or \
            os.path.dirname(os.path.abspath(sys.argv[0]))
        return _ensure_writable(d)

    # PyInstaller / cx_Freeze 等传统打包工具
    if getattr(sys, 'frozen', False):
        d = os.path.dirname(os.path.abspath(sys.executable))
        return _ensure_writable(d)

    # 开发模式
    return os.path.dirname(os.path.abspath(__file__))


def load_config():
    """加载 exe/脚本旁边的 config.json（UTF-8）

    缺失字段自动用默认值补齐；文件不存在或损坏时返回默认配置。
    """
    cfg_path = os.path.join(get_base_dir(), "config.json")
    defaults = {
        "include_origin": True,
        "luna_ws_origin": "ws://127.0.0.1:2333/api/ws/text/origin",
        "luna_ws_trans": "ws://127.0.0.1:2333/api/ws/text/trans",
        "vrchat_osc_ip": "127.0.0.1",
        "vrchat_osc_port": 9000,
        "chatbox_max_len": 140,
        "reconnect_delay": 3.0,
        "luna_exe_path": "",
    }
    if not os.path.exists(cfg_path):
        return defaults.copy()

    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        for k, v in defaults.items():
            cfg.setdefault(k, v)
        return cfg
    except Exception:
        return defaults.copy()


def save_config(cfg: dict):
    """保存配置到 exe/脚本 旁边的 config.json（utf-8 无 BOM）"""
    cfg_path = os.path.join(get_base_dir(), "config.json")
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=4, ensure_ascii=False)


# ================= 工具函数 =================
def safe_chunk_by_bytes(text: str, max_bytes: int) -> list:
    """按 UTF-8 字节数切割字符串，避免截断多字节字符

    VRChat Chatbox 限制 144 字节，中文字符占 3 字节
    此函数确保不会在字符中间切断
    """
    chunks = []
    current = []
    current_len = 0
    for char in text:
        char_bytes = char.encode("utf-8")
        if current_len + len(char_bytes) > max_bytes:
            chunks.append("".join(current))
            current = [char]
            current_len = len(char_bytes)
        else:
            current.append(char)
            current_len += len(char_bytes)
    if current:
        chunks.append("".join(current))
    return chunks


# ================= 桥接核心类 =================
class BridgeCore:
    """桥接核心：在独立线程中运行 asyncio 事件循环

    生命周期：
      start() → 创建守护线程 → _run_loop() → event loop → _main() → 3 个协程
      stop()  → 取消所有协程 → 停止 event loop → join 线程

    线程模型：
      主线程 (UI)          后台线程 (asyncio)
      ─────────           ─────────────────
      start() ──────────→ _run_loop()
      stop()  ──→ cancel ──→ loop.stop()
      log_queue ←── put ─── _log()
    """

    def __init__(self, config: dict, log_queue: queue.Queue | None = None):
        self.config = config
        self.log_queue = log_queue  # 给 UI 发送日志消息
        self._tasks = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._running = False
        self._osc = None

        # 运行时状态
        self.origin_lock = asyncio.Lock()
        self.latest_origin = ""
        self.send_queue = asyncio.Queue()

    # ---------- 日志 ----------
    def _log(self, level: str, msg: str):
        if self.log_queue:
            try:
                self.log_queue.put_nowait((level, msg))
            except queue.Full:
                pass
        else:
            print(f"[{level}] {msg}")

    # ---------- 异步核心（3 个协程） ----------
    async def _listen_origin(self):
        """协程 1：监听 Luna 原文 WebSocket

        收到消息 → 加锁写入 self.latest_origin
        断开后自动重连（间隔 reconnect_delay 秒）
        """
        while self._running:
            try:
                async with websockets.connect(
                    self.config["luna_ws_origin"],
                    ping_interval=20,
                    ping_timeout=10,
                ) as ws:
                    self._log("info", "✅ 原文 WebSocket 已连接")
                    async for msg in ws:
                        if not self._running:
                            break
                        txt = (
                            msg.decode("utf-8", errors="replace").strip()
                            if isinstance(msg, bytes)
                            else str(msg).strip()
                        )
                        if not txt:
                            continue
                        async with self.origin_lock:
                            self.latest_origin = txt
            except asyncio.CancelledError:
                break
            except Exception as e:
                if self._running:
                    self._log("warn", f"⚠️ 原文 WebSocket 断开: {e}")
                    await asyncio.sleep(self.config["reconnect_delay"])

    async def _listen_trans(self):
        """协程 2：监听 Luna 译文 WebSocket

        收到译文 → 读取最新原文（加锁）→ 按配置拼接 → 放入 send_queue
        断开后自动重连
        """
        while self._running:
            try:
                async with websockets.connect(
                    self.config["luna_ws_trans"],
                    ping_interval=20,
                    ping_timeout=10,
                ) as ws:
                    self._log("info", "✅ 译文 WebSocket 已连接")
                    async for msg in ws:
                        if not self._running:
                            break
                        txt_trans = (
                            msg.decode("utf-8", errors="replace").strip()
                            if isinstance(msg, bytes)
                            else str(msg).strip()
                        )
                        if not txt_trans:
                            continue
                        async with self.origin_lock:
                            current_origin = self.latest_origin

                        if self.config["include_origin"] and current_origin:
                            combined = f"{current_origin}\n{txt_trans}"
                        else:
                            combined = txt_trans
                        await self.send_queue.put(combined)
            except asyncio.CancelledError:
                break
            except Exception as e:
                if self._running:
                    self._log("warn", f"⚠️ 译文 WebSocket 断开: {e}")
                    await asyncio.sleep(self.config["reconnect_delay"])

    async def _send_worker(self):
        """协程 3：消费 send_queue，按字节切割后通过 OSC 发送到 VRChat

        间隔 80ms 避免 OSC 发送过快
        """
        while self._running:
            try:
                text = await asyncio.wait_for(self.send_queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            try:
                chunks = safe_chunk_by_bytes(text, self.config["chatbox_max_len"])
                for chunk in chunks:
                    builder = OscMessageBuilder(address="/chatbox/input")
                    builder.add_arg(chunk)
                    builder.add_arg(True)
                    builder.add_arg(1.0)
                    if self._osc:
                        self._osc.send(builder.build())
                    self._log("sent", chunk)
            except Exception as e:
                self._log("error", f"❌ OSC 发送失败: {e}")
            finally:
                self.send_queue.task_done()
                await asyncio.sleep(0.08)

    async def _main(self):
        """创建 3 个协程任务并用 gather 并发运行

        如果用户选择「仅译文」模式，不启动 _listen_origin
        """
        self._tasks = []
        self._tasks.append(asyncio.create_task(self._send_worker(), name="send"))
        self._tasks.append(asyncio.create_task(self._listen_trans(), name="trans"))
        if self.config["include_origin"]:
            self._tasks.append(asyncio.create_task(self._listen_origin(), name="origin"))

        try:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        except asyncio.CancelledError:
            pass

    def _run_loop(self):
        """后台线程入口：创建新 event loop，初始化 OSC 客户端，运行 _main()

        必须在子线程中创建新 loop（asyncio 默认 loop 属于主线程）
        """
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._osc = udp_client.SimpleUDPClient(
            self.config["vrchat_osc_ip"], self.config["vrchat_osc_port"]
        )
        try:
            self._loop.run_until_complete(self._main())
        finally:
            self._loop.close()

    # ---------- 公开方法（由 UI 线程调用） ----------
    def start(self):
        """启动桥接：创建守护线程运行 asyncio 事件循环

        幂等：已运行时直接返回
        """
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        self._log("info", "🔄 桥接器已启动")

    def stop(self):
        """停止桥接：取消所有协程 → 停止 loop → 等待线程退出（最多 5 秒）

        幂等：未运行时直接返回
        """
        if not self._running:
            return
        self._running = False
        self._log("info", "⏹ 正在停止桥接器...")

        if self._loop and self._loop.is_running():
            for t in asyncio.all_tasks(self._loop):
                t.cancel()
            self._loop.call_soon_threadsafe(self._loop.stop)

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        self._log("info", "✅ 桥接器已停止")

    @property
    def is_running(self):
        return self._running
