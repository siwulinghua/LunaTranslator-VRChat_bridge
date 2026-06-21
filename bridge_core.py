"""LunaTranslator 与 VRChat 桥接器 - 核心逻辑模块
可被 UI 或命令行调用，支持启动/停止。
"""
import asyncio
import threading
import json
import os
import sys
import queue

import websockets
from pythonosc import udp_client
from pythonosc.osc_message_builder import OscMessageBuilder


# ================= 路径工具 =================
def get_base_dir():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def load_config():
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
    if os.path.exists(cfg_path):
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        for k, v in defaults.items():
            cfg.setdefault(k, v)
    else:
        cfg = defaults.copy()
    return cfg


def save_config(cfg: dict):
    cfg_path = os.path.join(get_base_dir(), "config.json")
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=4, ensure_ascii=False)


# ================= 工具函数 =================
def safe_chunk_by_bytes(text: str, max_bytes: int) -> list:
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

    # ---------- 异步核心 ----------
    async def _listen_origin(self):
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
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._osc = udp_client.SimpleUDPClient(
            self.config["vrchat_osc_ip"], self.config["vrchat_osc_port"]
        )
        try:
            self._loop.run_until_complete(self._main())
        finally:
            self._loop.close()

    # ---------- 公开方法 ----------
    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        self._log("info", "🔄 桥接器已启动")

    def stop(self):
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
