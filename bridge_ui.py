"""LunaTranslator 与 VRChat 桥接器 - UI 界面"""
import os
import sys
import json
import queue
import subprocess
import customtkinter as ctk
from tkinter import filedialog, messagebox

from bridge_core import BridgeCore, load_config, save_config

# ================= 主题设置 =================
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


# ================= LunaTranslator 配置工具 =================
def find_luna_config(exe_path: str) -> str | None:
    """根据 exe 路径推测 LunaTranslator 的 userconfig/config.json"""
    exe_dir = os.path.dirname(exe_path)
    candidates = [
        os.path.join(exe_dir, "userconfig", "config.json"),
        os.path.join(exe_dir, "..", "userconfig", "config.json"),
        os.path.join(os.path.expandvars("%APPDATA%"), "LunaTranslator", "userconfig", "config.json"),
    ]
    for p in candidates:
        if os.path.isfile(p):
            return os.path.normpath(p)
    return None


def read_luna_ws_port(config_path: str) -> int:
    """读取 LunaTranslator 配置中的 WebSocket 端口"""
    with open(config_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    return cfg.get("networktcpport", 2333)


def enable_luna_ws_and_sr(config_path: str):
    """修改 LunaTranslator 配置：开启 WebSocket 服务和语音识别"""
    with open(config_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    cfg["networktcpenable"] = True
    cfg["networktcpport"] = cfg.get("networktcpport", 2333)
    if "sourcestatus2" not in cfg:
        cfg["sourcestatus2"] = {}
    if "mssr" not in cfg["sourcestatus2"]:
        cfg["sourcestatus2"]["mssr"] = {}
    cfg["sourcestatus2"]["mssr"]["use"] = True
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=4, ensure_ascii=False)


def launch_luna(exe_path: str, config_path: str | None = None):
    """启动 LunaTranslator，可选先修改配置"""
    if config_path and os.path.isfile(config_path):
        enable_luna_ws_and_sr(config_path)
    subprocess.Popen([exe_path], cwd=os.path.dirname(exe_path))


# ================= URL ↔ IP/Port 转换 =================
def url_to_ipport(url: str) -> tuple[str, int]:
    """从 ws://IP:PORT/path 提取 IP 和端口"""
    # ws://127.0.0.1:2333/api/ws/text/origin → ("127.0.0.1", 2333)
    import re
    m = re.match(r"ws://([^:/]+):(\d+)", url)
    if m:
        return m.group(1), int(m.group(2))
    return "127.0.0.1", 2333


def ipport_to_urls(ip: str, port: int) -> tuple[str, str]:
    """根据 IP+端口生成原文/译文两个 URL"""
    origin = f"ws://{ip}:{port}/api/ws/text/origin"
    trans = f"ws://{ip}:{port}/api/ws/text/trans"
    return origin, trans


# ================= 主界面 =================
class BridgeApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("LunaTranslator 与 VRChat 桥接器")
        self.geometry("580x700")
        self.minsize(500, 550)

        # 桥接核心
        self.bridge: BridgeCore | None = None
        self.log_queue: queue.Queue = queue.Queue()

        # 加载已有配置
        self.cfg = load_config()
        self._ws_ip, self._ws_port = url_to_ipport(self.cfg.get("luna_ws_origin", ""))

        # LunaTranslator 路径记忆
        self._luna_config_path: str | None = None

        # 构建 UI
        self._build_ui()

        # 启动日志轮询
        self._poll_logs()

        # 关闭时停止桥接
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ========== UI 构建 ==========
    def _build_ui(self):
        # ---- LunaTranslator 连接 ----
        self.frame_luna = ctk.CTkFrame(self)
        self.frame_luna.pack(fill="x", padx=10, pady=(10, 5))

        ctk.CTkLabel(
            self.frame_luna, text="📡 LunaTranslator 连接",
            font=ctk.CTkFont(size=14, weight="bold"),
        ).pack(anchor="w", padx=10, pady=(10, 5))

        # 路径选择行
        row_path = ctk.CTkFrame(self.frame_luna, fg_color="transparent")
        row_path.pack(fill="x", padx=10, pady=2)
        ctk.CTkLabel(row_path, text="LunaTranslator.exe:").pack(side="left")
        self.entry_luna_path = ctk.CTkEntry(row_path, width=290, placeholder_text="选择或输入路径...")
        self.entry_luna_path.pack(side="left", padx=5)
        ctk.CTkButton(row_path, text="浏览...", width=60, command=self._browse_luna).pack(side="left")

        # 读取接口信息按钮
        row_read = ctk.CTkFrame(self.frame_luna, fg_color="transparent")
        row_read.pack(fill="x", padx=10, pady=(5, 2))
        ctk.CTkButton(
            row_read, text="📋 读取接口信息",
            width=140, command=self._read_luna_config,
        ).pack(side="left")

        # WebSocket IP + 端口
        ws_frame = ctk.CTkFrame(self.frame_luna, fg_color="transparent")
        ws_frame.pack(fill="x", padx=10, pady=2)
        ctk.CTkLabel(ws_frame, text="WebSocket IP:", width=90).pack(side="left")
        self.entry_ws_ip = ctk.CTkEntry(ws_frame, width=110)
        self.entry_ws_ip.insert(0, self._ws_ip)
        self.entry_ws_ip.pack(side="left", padx=(0, 5))
        ctk.CTkLabel(ws_frame, text="端口:").pack(side="left", padx=(10, 0))
        self.entry_ws_port = ctk.CTkEntry(ws_frame, width=70)
        self.entry_ws_port.insert(0, str(self._ws_port))
        self.entry_ws_port.pack(side="left", padx=5)
        ctk.CTkLabel(ws_frame, text="(原文/译文共用)", text_color="gray", font=ctk.CTkFont(size=11)).pack(side="left", padx=10)

        # ---- VRChat 输出 ----
        self.frame_params = ctk.CTkFrame(self)
        self.frame_params.pack(fill="x", padx=10, pady=5)

        ctk.CTkLabel(
            self.frame_params, text="🎮 VRChat 输出",
            font=ctk.CTkFont(size=14, weight="bold"),
        ).pack(anchor="w", padx=10, pady=(10, 5))

        # OSC IP + 端口
        row_osc = ctk.CTkFrame(self.frame_params, fg_color="transparent")
        row_osc.pack(fill="x", padx=10, pady=2)
        ctk.CTkLabel(row_osc, text="OSC IP:", width=70).pack(side="left")
        self.entry_osc_ip = ctk.CTkEntry(row_osc, width=110)
        self.entry_osc_ip.insert(0, self.cfg.get("vrchat_osc_ip", "127.0.0.1"))
        self.entry_osc_ip.pack(side="left", padx=(0, 5))
        ctk.CTkLabel(row_osc, text="端口:").pack(side="left", padx=(10, 0))
        self.entry_osc_port = ctk.CTkEntry(row_osc, width=70)
        self.entry_osc_port.insert(0, str(self.cfg.get("vrchat_osc_port", 9000)))
        self.entry_osc_port.pack(side="left", padx=5)

        # Chatbox + 重连
        row_cb = ctk.CTkFrame(self.frame_params, fg_color="transparent")
        row_cb.pack(fill="x", padx=10, pady=2)
        ctk.CTkLabel(row_cb, text="最大长度:", width=70).pack(side="left")
        self.entry_chatbox = ctk.CTkEntry(row_cb, width=55)
        self.entry_chatbox.insert(0, str(self.cfg.get("chatbox_max_len", 140)))
        self.entry_chatbox.pack(side="left", padx=(0, 2))
        ctk.CTkLabel(row_cb, text="字节").pack(side="left")
        ctk.CTkLabel(row_cb, text="重连间隔:").pack(side="left", padx=(15, 0))
        self.entry_reconnect = ctk.CTkEntry(row_cb, width=55)
        self.entry_reconnect.insert(0, str(self.cfg.get("reconnect_delay", 3.0)))
        self.entry_reconnect.pack(side="left", padx=5)
        ctk.CTkLabel(row_cb, text="秒").pack(side="left")

        # 输出模式
        row_mode = ctk.CTkFrame(self.frame_params, fg_color="transparent")
        row_mode.pack(fill="x", padx=10, pady=(5, 10))
        ctk.CTkLabel(row_mode, text="输出模式:", width=70).pack(side="left")
        self.mode_var = ctk.StringVar(value="both" if self.cfg.get("include_origin", True) else "trans")
        ctk.CTkRadioButton(row_mode, text="原文+译文", variable=self.mode_var, value="both").pack(side="left", padx=5)
        ctk.CTkRadioButton(row_mode, text="仅译文", variable=self.mode_var, value="trans").pack(side="left", padx=5)

        # ---- 控制 ----
        self.frame_control = ctk.CTkFrame(self)
        self.frame_control.pack(fill="x", padx=10, pady=5)

        ctk.CTkLabel(
            self.frame_control, text="🔧 控制",
            font=ctk.CTkFont(size=14, weight="bold"),
        ).pack(anchor="w", padx=10, pady=(10, 5))

        # 一键启动行
        row_launch = ctk.CTkFrame(self.frame_control, fg_color="transparent")
        row_launch.pack(fill="x", padx=10, pady=2)
        self.check_auto_sr = ctk.CTkCheckBox(row_launch, text="自动打开Luna的语音识别和通信功能，翻译AI和音频来源需手动设置")
        self.check_auto_sr.pack(side="left")
        self.check_auto_sr.select()

        # 状态 + 启停
        row_ctrl = ctk.CTkFrame(self.frame_control, fg_color="transparent")
        row_ctrl.pack(fill="x", padx=10, pady=5)
        ctk.CTkButton(
            row_ctrl, text="🚀 启动 LunaTranslator",
            width=180, fg_color="#2e7d32", hover_color="#1b5e20",
            command=self._launch_luna,
        ).pack(side="left", padx=(0, 5))
        self.btn_start = ctk.CTkButton(
            row_ctrl, text="▶ 启动桥接", width=110,
            fg_color="#1b5e20", hover_color="#2e7d32",
            command=self._start_bridge,
        )
        self.btn_start.pack(side="left", padx=(0, 5))
        self.btn_stop = ctk.CTkButton(
            row_ctrl, text="⏹ 停止桥接", width=90,
            fg_color="#b71c1c", hover_color="#c62828",
            state="disabled", command=self._stop_bridge,
        )
        self.btn_stop.pack(side="left")
        ctk.CTkLabel(
            row_ctrl, text="💡 此处不能停止翻译",
        ).pack(side="left", padx=15)

        # 双状态
        row_status = ctk.CTkFrame(self.frame_control, fg_color="transparent")
        row_status.pack(fill="x", padx=10, pady=(2, 10))
        self.label_luna_status = ctk.CTkLabel(
            row_status, text="LunaTranslator: ● 未检测", text_color="gray",
            font=ctk.CTkFont(size=12),
        )
        self.label_luna_status.pack(side="left", padx=(0, 20))
        self.label_bridge_status = ctk.CTkLabel(
            row_status, text="桥接器: ● 未启动", text_color="gray",
            font=ctk.CTkFont(size=12),
        )
        self.label_bridge_status.pack(side="left")

        # ---- 日志区 ----
        self.frame_log = ctk.CTkFrame(self)
        self.frame_log.pack(fill="both", expand=True, padx=10, pady=(5, 10))

        row_log_header = ctk.CTkFrame(self.frame_log, fg_color="transparent")
        row_log_header.pack(fill="x", padx=10, pady=(8, 2))
        ctk.CTkLabel(
            row_log_header, text="📋 运行日志",
            font=ctk.CTkFont(size=14, weight="bold"),
        ).pack(side="left")
        self.check_show_sent = ctk.CTkCheckBox(row_log_header, text="显示发送内容")
        self.check_show_sent.pack(side="right", padx=5)
        self.btn_clear_log = ctk.CTkButton(
            row_log_header, text="清空", width=50,
            fg_color="gray", hover_color="#555",
            command=self._clear_log,
        )
        self.btn_clear_log.pack(side="right", padx=5)

        self.text_log = ctk.CTkTextbox(self.frame_log, wrap="word", state="disabled")
        self.text_log.pack(fill="both", expand=True, padx=10, pady=(2, 10))

    # ========== 动作回调 ==========
    def _browse_luna(self):
        path = filedialog.askopenfilename(
            title="选择 LunaTranslator.exe",
            filetypes=[("可执行文件", "*.exe"), ("所有文件", "*.*")],
        )
        if path:
            self.entry_luna_path.delete(0, "end")
            self.entry_luna_path.insert(0, path)
            cfg_path = find_luna_config(path)
            if cfg_path:
                self._luna_config_path = cfg_path
                self.label_luna_status.configure(
                    text="LunaTranslator: ● 已找到配置", text_color="green"
                )
            else:
                self._luna_config_path = None
                self.label_luna_status.configure(
                    text="LunaTranslator: ● 未找到配置", text_color="orange"
                )

    def _read_luna_config(self):
        exe_path = self.entry_luna_path.get().strip()
        if not exe_path:
            cfg_path = os.path.join(
                os.path.expandvars("%APPDATA%"),
                "LunaTranslator", "userconfig", "config.json"
            )
            if not os.path.isfile(cfg_path):
                messagebox.showwarning("提示", "请先选择 LunaTranslator.exe 路径，或确保配置在默认位置。")
                return
            self._luna_config_path = cfg_path
        else:
            cfg_path = find_luna_config(exe_path)
            if not cfg_path:
                messagebox.showerror("错误", "未能在 exe 目录下找到 userconfig/config.json")
                return
            self._luna_config_path = cfg_path

        try:
            port = read_luna_ws_port(cfg_path)
            self.entry_ws_ip.delete(0, "end")
            self.entry_ws_ip.insert(0, "127.0.0.1")
            self.entry_ws_port.delete(0, "end")
            self.entry_ws_port.insert(0, str(port))

            self.label_luna_status.configure(
                text=f"LunaTranslator: ● 已读取 (端口:{port})", text_color="green"
            )
            self._append_log("info", f"📋 已从配置读取 WebSocket 端口: {port}")
        except Exception as e:
            messagebox.showerror("错误", f"读取配置失败: {e}")

    def _launch_luna(self):
        exe_path = self.entry_luna_path.get().strip()
        if not exe_path:
            messagebox.showwarning("提示", "请先选择 LunaTranslator.exe 路径")
            return
        if not os.path.isfile(exe_path):
            messagebox.showerror("错误", f"文件不存在: {exe_path}")
            return

        cfg_path = self._luna_config_path
        if not cfg_path:
            cfg_path = find_luna_config(exe_path)

        try:
            if self.check_auto_sr.get() and cfg_path:
                enable_luna_ws_and_sr(cfg_path)
                self._append_log("info", "✅ 已修改配置: 启用 WebSocket 服务 + 语音识别")
            launch_luna(exe_path)
            self._append_log("info", f"🚀 已启动 LunaTranslator: {exe_path}")
            self.label_luna_status.configure(
                text="LunaTranslator: ● 已启动", text_color="green"
            )
            # 自动启动桥接
            self._start_bridge()
        except Exception as e:
            messagebox.showerror("错误", f"启动失败: {e}")

    def _start_bridge(self):
        try:
            ws_ip = self.entry_ws_ip.get().strip()
            ws_port = int(self.entry_ws_port.get().strip())
            origin_url, trans_url = ipport_to_urls(ws_ip, ws_port)
            config = {
                "include_origin": self.mode_var.get() == "both",
                "luna_ws_origin": origin_url,
                "luna_ws_trans": trans_url,
                "vrchat_osc_ip": self.entry_osc_ip.get().strip(),
                "vrchat_osc_port": int(self.entry_osc_port.get().strip()),
                "chatbox_max_len": int(self.entry_chatbox.get().strip()),
                "reconnect_delay": float(self.entry_reconnect.get().strip()),
            }
        except ValueError as e:
            messagebox.showerror("参数错误", f"请检查数值输入: {e}")
            return

        save_config(config)
        self.bridge = BridgeCore(config, log_queue=self.log_queue)
        self.bridge.start()

        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.label_bridge_status.configure(text="桥接器: ● 运行中", text_color="green")
        self._set_params_state("disabled")

    def _stop_bridge(self):
        if self.bridge:
            self.bridge.stop()
            self.bridge = None

        self.btn_start.configure(state="normal")
        self.btn_stop.configure(state="disabled")
        self.label_bridge_status.configure(text="桥接器: ● 已停止", text_color="gray")
        self._set_params_state("normal")

    def _set_params_state(self, state: str):
        widgets = [
            self.entry_ws_ip, self.entry_ws_port,
            self.entry_osc_ip, self.entry_osc_port,
            self.entry_chatbox, self.entry_reconnect,
        ]
        for w in widgets:
            w.configure(state=state)

    def _append_log(self, level: str, msg: str):
        self.text_log.configure(state="normal")
        self.text_log.insert("end", f"{msg}\n")
        self.text_log.see("end")
        self.text_log.configure(state="disabled")

    def _clear_log(self):
        self.text_log.configure(state="normal")
        self.text_log.delete("1.0", "end")
        self.text_log.configure(state="disabled")

    def _poll_logs(self):
        """定时从 log_queue 取日志，驱动 UI 更新"""
        while not self.log_queue.empty():
            try:
                level, msg = self.log_queue.get_nowait()
                # WebSocket 连接状态驱动 LunaTranslator 状态灯
                if "✅ 原文 WebSocket 已连接" in msg or "✅ 译文 WebSocket 已连接" in msg:
                    self.label_luna_status.configure(
                        text="LunaTranslator: ● WebSocket 已连接", text_color="green"
                    )
                elif "⚠️ 原文 WebSocket 断开" in msg or "⚠️ 译文 WebSocket 断开" in msg:
                    self.label_luna_status.configure(
                        text="LunaTranslator: ● WebSocket 已断开", text_color="orange"
                    )

                if level == "sent" and not self.check_show_sent.get():
                    continue
                if level == "sent":
                    msg = f"📤 {msg}"
                self._append_log(level, msg)
            except queue.Empty:
                break
        self.after(200, self._poll_logs)

    def _on_close(self):
        if self.bridge and self.bridge.is_running:
            self.bridge.stop()
        self.destroy()


# ================= 入口 =================
if __name__ == "__main__":
    app = BridgeApp()
    app.mainloop()
