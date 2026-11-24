import datetime as dt
import queue
import re
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    serial = None
    list_ports = None


STAMP_RE = re.compile(r"\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3})\]")
HEX_RE = re.compile(r"^[0-9a-fA-F\s]+$")


def load_frames(path: Path):
    lines = path.read_text(encoding="gb18030", errors="replace").splitlines()
    frames = []
    for i, line in enumerate(lines):
        match = STAMP_RE.search(line)
        if not match:
            continue
        raw = ""
        for candidate in lines[i + 1:]:
            candidate = candidate.strip()
            if candidate:
                raw = candidate
                break
        if not raw or not HEX_RE.fullmatch(raw):
            continue
        try:
            payload = bytes.fromhex(raw)
            timestamp = dt.datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S.%f")
        except ValueError:
            continue
        frames.append((timestamp, payload, raw))
    if not frames:
        raise ValueError("没有解析到有效的时间戳/HEX 报文")
    first = frames[0][0]
    return [(float((stamp - first).total_seconds()), payload, raw, stamp) for stamp, payload, raw in frames]


class App:
    def __init__(self, root):
        self.root = root
        self.root.title("XyeBus 报文模拟上位机")
        self.root.geometry("1180x700")
        self.root.minsize(850, 520)
        self.source = Path(__file__).with_name("0907智谷-feiyi室内外机报文.txt")
        self.frames = []
        self.ser = None
        self.worker = None
        self.stop_event = threading.Event()
        self.pause_event = threading.Event()
        self.events = queue.Queue()
        self._build_ui()
        self.refresh_ports()
        self.load_file(self.source)
        self.root.after(50, self.process_events)

    def _build_ui(self):
        top = ttk.Frame(self.root, padding=10)
        top.pack(fill="x")
        ttk.Label(top, text="报文文件:").pack(side="left")
        self.file_var = tk.StringVar(value=self.source.name)
        ttk.Entry(top, textvariable=self.file_var, state="readonly", width=42).pack(side="left", padx=5)
        ttk.Button(top, text="选择文件", command=self.choose_file).pack(side="left", padx=3)
        ttk.Label(top, text="串口:").pack(side="left", padx=(18, 3))
        self.port_var = tk.StringVar()
        self.port_box = ttk.Combobox(top, textvariable=self.port_var, state="readonly", width=15)
        self.port_box.pack(side="left")
        ttk.Button(top, text="刷新串口", command=self.refresh_ports).pack(side="left", padx=5)
        ttk.Label(top, text="4800  8N1").pack(side="left", padx=8)
        ttk.Label(top, text="发送条数:").pack(side="left", padx=(12, 3))
        self.limit_var = tk.StringVar(value="1")
        self.limit_box = tk.Spinbox(top, from_=1, to=1, textvariable=self.limit_var,
                                    width=8)
        self.limit_box.pack(side="left")
        self.start_btn = ttk.Button(top, text="开始发送", command=self.start)
        self.start_btn.pack(side="left", padx=(15, 3))
        self.stop_btn = ttk.Button(top, text="停止发送", command=self.pause, state="disabled")
        self.stop_btn.pack(side="left")

        info = ttk.Frame(self.root, padding=(10, 0, 10, 6))
        info.pack(fill="x")
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(info, textvariable=self.status_var).pack(side="left")
        self.count_var = tk.StringVar(value="0 条")
        ttk.Label(info, textvariable=self.count_var).pack(side="right")

        panes = ttk.PanedWindow(self.root, orient="horizontal")
        panes.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self.pending = self.make_log(panes, "待发送数据")
        self.sent = self.make_log(panes, "已发送数据")

    def make_log(self, parent, title):
        frame = ttk.Labelframe(parent, text=title, padding=5)
        text = tk.Text(frame, wrap="none", font=("Consolas", 9), state="disabled")
        text.tag_configure("interval", foreground="red")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        parent.add(frame, weight=1)
        return text

    def log(self, widget, value, tag=None):
        widget.configure(state="normal")
        if tag:
            widget.insert("end", value + "\n", tag)
        else:
            widget.insert("end", value + "\n")
        widget.see("end")
        widget.configure(state="disabled")

    def load_file(self, path):
        try:
            self.frames = load_frames(path)
            self.file_var.set(str(path))
            self.limit_box.configure(from_=1, to=len(self.frames))
            self.limit_var.set(len(self.frames))
            self.count_var.set(f"共 {len(self.frames)} 条")
            self.log(self.pending, f"已加载 {len(self.frames)} 条报文")
        except Exception as exc:
            messagebox.showerror("加载失败", str(exc))

    def choose_file(self):
        path = filedialog.askopenfilename(filetypes=[("报文文本", "*.txt"), ("所有文件", "*.*")])
        if path:
            self.load_file(Path(path))

    def refresh_ports(self):
        if list_ports is None:
            self.port_box["values"] = []
            self.status_var.set("未安装 pyserial，请先运行 install.bat")
            return
        ports = [p.device for p in list_ports.comports()]
        self.port_box["values"] = ports
        if ports and self.port_var.get() not in ports:
            self.port_var.set(ports[0])
        self.status_var.set(f"发现 {len(ports)} 个串口")

    def start(self):
        if self.worker and self.worker.is_alive():
            self.pause_event.set()
            self.stop_btn.configure(text="停止发送")
            self.status_var.set("正在发送")
            return
        if not self.frames or not self.port_var.get():
            messagebox.showwarning("无法开始", "请先加载报文并选择串口")
            return
        try:
            self.ser = serial.Serial(self.port_var.get(), 4800, bytesize=8, parity=serial.PARITY_NONE,
                                     stopbits=1, timeout=1)
        except Exception as exc:
            messagebox.showerror("串口打开失败", str(exc))
            return
        self.stop_event.clear()
        self.pause_event.set()
        self.pending.configure(state="normal"); self.pending.delete("1.0", "end"); self.pending.configure(state="disabled")
        self.sent.configure(state="normal"); self.sent.delete("1.0", "end"); self.sent.configure(state="disabled")
        try:
            requested_limit = int(self.limit_var.get())
        except (TypeError, ValueError):
            messagebox.showwarning("发送条数无效", f"请输入 1 到 {len(self.frames)} 之间的整数")
            return
        if not 1 <= requested_limit <= len(self.frames):
            messagebox.showwarning("发送条数无效", f"请输入 1 到 {len(self.frames)} 之间的整数")
            self.limit_var.set(min(max(1, requested_limit), len(self.frames)))
            return
        self.send_limit = requested_limit
        # 固定显示本次任务的原始抓包数据，发送过程中不再修改此窗口。
        selected_frames = self.frames[:self.send_limit]
        for frame_index, (_, _, raw, stamp) in enumerate(selected_frames):
            self.log(self.pending, f"[{stamp:%Y-%m-%d %H:%M:%S.%f}] {raw}")
            if frame_index + 1 < self.send_limit:
                interval_ms = (selected_frames[frame_index + 1][3] - stamp).total_seconds() * 1000
                self.log(self.pending, f"[{interval_ms:.3f} ms]", "interval")
        self.last_actual_stamp = None
        self.worker = threading.Thread(target=self.send_loop, daemon=True)
        self.worker.start()
        self.start_btn.configure(text="继续发送")
        self.stop_btn.configure(state="normal", text="停止发送")
        self.status_var.set("正在发送")

    def pause(self):
        if self.pause_event.is_set():
            self.pause_event.clear()
            self.status_var.set("已暂停（点击开始发送继续）")
        else:
            self.pause_event.set()
            self.status_var.set("正在发送")

    def send_loop(self):
        base = time.monotonic()
        selected_frames = self.frames[:self.send_limit]
        for index, (delay, payload, raw, stamp) in enumerate(selected_frames):
            while not self.stop_event.is_set() and not self.pause_event.wait(0.05):
                pass
            while not self.stop_event.is_set():
                remaining = delay - (time.monotonic() - base)
                if remaining <= 0:
                    break
                if not self.pause_event.wait(min(remaining, 0.05)):
                    continue
            if self.stop_event.is_set():
                return
            try:
                self.ser.write(payload)
            except Exception as exc:
                self.events.put(("error", str(exc)))
                return
            actual_stamp = dt.datetime.now()
            self.events.put(("sent", index, raw, stamp, actual_stamp))
        self.events.put(("done",))

    def process_events(self):
        try:
            while True:
                event = self.events.get_nowait()
                if event[0] == "sent":
                    _, index, raw, stamp, actual_stamp = event
                    self.log(self.sent, f"[{actual_stamp:%Y-%m-%d %H:%M:%S.%f}][{index + 1}/{self.send_limit}] {raw}")
                    if self.last_actual_stamp is not None:
                        interval_ms = (actual_stamp - self.last_actual_stamp).total_seconds() * 1000
                        self.log(self.sent, f"[{interval_ms:.3f} ms]", "interval")
                    self.last_actual_stamp = actual_stamp
                elif event[0] == "error":
                    messagebox.showerror("发送失败", event[1]); self.finish()
                elif event[0] == "done":
                    self.status_var.set("发送完成"); self.finish()
        except queue.Empty:
            pass
        self.root.after(50, self.process_events)

    def finish(self):
        self.stop_event.set()
        if self.ser:
            try: self.ser.close()
            except Exception: pass
        self.start_btn.configure(text="开始发送")
        self.stop_btn.configure(state="disabled")


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
