"""
MD Transcriber — near-real-time local speech-to-text for a Music Director's mic,
displayed as a chat-style grid for an audio engineer to read instead of wearing headphones.

Rows are grouped into "chunks": consecutive speech is appended to the current chunk;
a chunk ends and a new one begins after SILENCE_GAP_SECONDS of no speech.
"""

import json
import queue
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk

import psutil
import sounddevice as sd
from vosk import Model, KaldiRecognizer

MODEL_PATH = "model"           # unzip a vosk model here (see README instructions)
SAMPLE_RATE = 16000
SILENCE_GAP_SECONDS = 2        # gap that starts a new chunk/row
AGO_REFRESH_MS = 1000          # how often the "seconds ago" column + colors update
RETENTION_SECONDS = 10 * 60    # rows older than this are dropped
CPU_POLL_SECONDS = 1.0
MIN_MSG_FONT = 10
MAX_MSG_FONT = 32
FONT_STEP = 2

COLOR_FRESH = "#ffffff"        # < 1 min
COLOR_AGING = "#f5d742"        # 1 - 3 min
COLOR_STALE = "#ff5c5c"        # >= 3 min

BG_DARK = "#161616"
BG_ROW_SEP = "#333333"
FG_META = "#8a8a8a"
FG_META_ACCENT = "#5fb0ff"


def format_clock(epoch_seconds: float) -> str:
    return time.strftime("%-I:%M %p", time.localtime(epoch_seconds))


def format_ago(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s ago"
    m, s = divmod(seconds, 60)
    return f"{m}m {s:02d}s ago"


def message_color(age_seconds: float) -> str:
    if age_seconds < 60:
        return COLOR_FRESH
    if age_seconds < 180:
        return COLOR_AGING
    return COLOR_STALE


class Row:
    def __init__(self, parent, chunk_start, time_font, ago_font, msg_font):
        self.chunk_start = chunk_start
        self.last_word_time = chunk_start
        self.text = ""

        self.frame = tk.Frame(parent, bg=BG_DARK)
        self.frame.columnconfigure(0, minsize=130)
        self.frame.columnconfigure(1, weight=1)

        self.time_var = tk.StringVar(value=format_clock(chunk_start))
        self.ago_var = tk.StringVar()
        self.msg_var = tk.StringVar()

        self.time_label = tk.Label(
            self.frame, textvariable=self.time_var, font=time_font,
            anchor="ne", justify="right", bg=BG_DARK, fg=FG_META_ACCENT,
        )
        self.ago_label = tk.Label(
            self.frame, textvariable=self.ago_var, font=ago_font,
            anchor="ne", justify="right", bg=BG_DARK, fg=FG_META,
        )
        self.msg_label = tk.Label(
            self.frame, textvariable=self.msg_var, font=msg_font,
            anchor="nw", justify="left", bg=BG_DARK, fg=COLOR_FRESH,
            wraplength=560,
        )

        self.time_label.grid(row=0, column=0, sticky="ne", padx=8, pady=(6, 0))
        self.ago_label.grid(row=1, column=0, sticky="ne", padx=8, pady=(0, 6))
        self.msg_label.grid(row=0, column=1, rowspan=2, sticky="nw", padx=8, pady=6)

        sep = tk.Frame(self.frame, bg=BG_ROW_SEP, height=1)
        sep.grid(row=2, column=0, columnspan=2, sticky="ew")

        self.frame.pack(fill="x", side="top")
        self.refresh(chunk_start)

    def append(self, text, now):
        self.text = (self.text + " " + text).strip()
        self.msg_var.set(self.text)
        self.last_word_time = now

    def refresh(self, now):
        age = now - self.chunk_start
        self.ago_var.set(format_ago(age))
        self.msg_label.configure(fg=message_color(age))

    def destroy(self):
        self.frame.destroy()


class TranscriberApp:
    def __init__(self, root):
        self.root = root
        self.root.title("SeeMD")
        self.root.geometry("900x650")
        self.root.minsize(480, 320)
        self.root.configure(bg=BG_DARK)

        self.rows = []
        self.current_row = None

        self.audio_q = queue.Queue()
        self.text_q = queue.Queue()
        self.running = True
        self.listening = False
        self.always_on_top = False

        self.throttle_sleep = 0.0
        self.cpu_cap = None          # None = no cap, else int percent
        self.cpu_percent = 0.0

        self.font_size = 16
        self.msg_font = tkfont.Font(family="Helvetica", size=self.font_size)
        self.time_font = tkfont.Font(family="Menlo", size=13, weight="bold")
        self.ago_font = tkfont.Font(family="Menlo", size=10)

        self._build_gui()
        self._init_model_and_devices()
        self._open_stream(self.selected_device)

        self.cpu_thread = threading.Thread(target=self._cpu_monitor_loop, daemon=True)
        self.cpu_thread.start()
        self.worker = threading.Thread(target=self._recognize_loop, daemon=True)
        self.worker.start()

        self.root.after(100, self._poll_text_queue)
        self.root.after(AGO_REFRESH_MS, self._refresh_rows)
        self.root.after(5000, self._sweep_old_rows)
        self.root.after(1000, self._poll_cpu_display)

        self._set_listening(True)

    # ---------- GUI ----------

    def _build_gui(self):
        # Deliberately do NOT force a theme (e.g. "clam") here: leaving ttk on its
        # platform-default theme is what gives native-looking, native-contrast widgets
        # on each OS (Aqua on macOS, the native theme on Windows, clam/alt on Linux
        # and Raspberry Pi) instead of fighting the platform's own button rendering.
        style = ttk.Style()
        style.configure("Cpu.Horizontal.TProgressbar", thickness=10)

        # ---- Title: editable label, useful for telling instances apart (e.g. multiple
        # feeds running side by side) ----
        title_bar = ttk.Frame(self.root)
        title_bar.pack(fill="x", side="top")
        self.title_var = tk.StringVar(value="Transcript")
        self.title_entry = tk.Entry(
            title_bar, textvariable=self.title_var, justify="center",
            font=("Helvetica", 20, "bold"), relief="flat", bd=0,
            highlightthickness=0,
        )
        self.title_entry.pack(fill="x", expand=True, side="left", padx=(12, 0), pady=(10, 4))

        self.controls_visible = True
        self.collapse_btn = ttk.Button(title_bar, text="▾", width=2, command=self._toggle_controls)
        self.collapse_btn.pack(side="right", padx=12, pady=(10, 4))

        # Toolbars use plain ttk.Frame/ttk.Label with no custom colors, so the whole
        # toolbar area (background included) is drawn by the native theme and matches
        # the native controls inside it perfectly, on every platform. The custom dark
        # theme is reserved for the transcript panel below, which is fully custom-drawn.

        # ---- Top toolbar: Start/Stop, Clear, Font (left)  |  Mic selector (right) ----
        self.top_bar = top_bar = ttk.Frame(self.root)
        top_bar.pack(fill="x", side="top")

        top_left = ttk.Frame(top_bar)
        top_left.pack(side="left", padx=10, pady=8)
        top_right = ttk.Frame(top_bar)
        top_right.pack(side="right", padx=10, pady=8)

        self.toggle_btn = ttk.Button(top_left, text="⏸", width=3, command=self._toggle_listening)
        self.toggle_btn.pack(side="left", padx=(0, 8))

        ttk.Button(top_left, text="Clear", command=self._clear_transcript).pack(side="left", padx=(0, 14))

        ttk.Button(top_left, text="A-", width=3, command=self._font_smaller).pack(side="left", padx=(0, 4))
        ttk.Button(top_left, text="A+", width=3, command=self._font_larger).pack(side="left")

        ttk.Label(top_right, text="Mic").pack(side="left", padx=(0, 6))
        self.mic_var = tk.StringVar()
        self.mic_combo = ttk.Combobox(top_right, textvariable=self.mic_var, state="readonly", width=24)
        self.mic_combo.pack(side="left")
        self.mic_combo.bind("<<ComboboxSelected>>", self._on_mic_change)

        # ---- Bottom toolbar: Stay-on-top checkbox (left)  |  CPU cap/meter (right) ----
        self.bottom_bar = bottom_bar = ttk.Frame(self.root)
        bottom_bar.pack(fill="x", side="top")

        bottom_left = ttk.Frame(bottom_bar)
        bottom_left.pack(side="left", padx=10, pady=(0, 8))
        bottom_right = ttk.Frame(bottom_bar)
        bottom_right.pack(side="right", padx=10, pady=(0, 8))

        self.always_on_top_var = tk.BooleanVar(value=False)
        top_check = ttk.Checkbutton(
            bottom_left, text="Stay on top", variable=self.always_on_top_var,
            command=self._toggle_always_on_top,
        )
        top_check.pack(side="left")

        ttk.Label(bottom_right, text="CPU cap").pack(side="left", padx=(0, 6))
        self.cap_var = tk.StringVar(value="Off")
        cap_values = ["Off"] + [f"{p}%" for p in range(10, 101, 10)]
        cap_combo = ttk.Combobox(bottom_right, textvariable=self.cap_var, state="readonly", width=5, values=cap_values)
        cap_combo.pack(side="left", padx=(0, 14))
        cap_combo.bind("<<ComboboxSelected>>", self._on_cap_change)

        ttk.Label(bottom_right, text="CPU").pack(side="left", padx=(0, 6))
        self.cpu_bar = ttk.Progressbar(
            bottom_right, style="Cpu.Horizontal.TProgressbar", length=110, mode="determinate", maximum=100,
        )
        self.cpu_bar.pack(side="left", padx=(0, 8))
        self.cpu_label_var = tk.StringVar(value="0%")
        ttk.Label(bottom_right, textvariable=self.cpu_label_var, width=4, anchor="w").pack(side="left")

        self.container = container = tk.Frame(self.root, bg=BG_DARK)
        container.pack(fill="both", expand=True)

        self.canvas = tk.Canvas(container, borderwidth=0, highlightthickness=0, bg=BG_DARK)
        scrollbar = ttk.Scrollbar(container, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scrollbar.set)

        scrollbar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)

        self.grid_frame = tk.Frame(self.canvas, bg=BG_DARK)
        self.canvas_window = self.canvas.create_window((0, 0), window=self.grid_frame, anchor="nw")

        self.grid_frame.bind("<Configure>", self._on_frame_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)

        # Clicking anywhere outside the title field should drop its focus, so the
        # blinking cursor doesn't get stuck there with no way to click it away.
        self.root.bind_all("<Button-1>", self._on_global_click, add="+")

    def _on_global_click(self, event):
        if event.widget is not self.title_entry:
            self.root.focus_set()

    def _on_frame_configure(self, event):
        self._reflow_canvas()

    def _on_canvas_configure(self, event):
        self.canvas.itemconfig(self.canvas_window, width=event.width)
        self._reflow_canvas()

    def _reflow_canvas(self):
        # Bottom-align the transcript when it's shorter than the viewport (newest pinned
        # to the window's bottom edge); once it overflows, fall back to normal scrolling
        # pinned to the newest (bottom) entry.
        self.canvas.update_idletasks()
        canvas_height = self.canvas.winfo_height()
        frame_height = self.grid_frame.winfo_reqheight()
        width = self.canvas.winfo_width()

        if frame_height <= canvas_height:
            y_offset = canvas_height - frame_height
            self.canvas.coords(self.canvas_window, 0, y_offset)
            self.canvas.configure(scrollregion=(0, 0, width, canvas_height))
        else:
            self.canvas.coords(self.canvas_window, 0, 0)
            self.canvas.configure(scrollregion=(0, 0, width, frame_height))
            self.canvas.yview_moveto(1.0)

    def _scroll_to_bottom(self):
        self.root.update_idletasks()
        self._reflow_canvas()

    # ---------- Toolbar actions ----------

    def _toggle_listening(self):
        self._set_listening(not self.listening)

    def _set_listening(self, on):
        self.listening = on
        if on:
            self.stream.start()
            self.toggle_btn.configure(text="⏸")
        else:
            self.stream.stop()
            self.toggle_btn.configure(text="▶")

    def _toggle_always_on_top(self):
        self.always_on_top = self.always_on_top_var.get()
        self.root.attributes("-topmost", self.always_on_top)

    def _toggle_controls(self):
        self.controls_visible = not self.controls_visible
        if self.controls_visible:
            self.top_bar.pack(fill="x", side="top", before=self.container)
            self.bottom_bar.pack(fill="x", side="top", before=self.container)
            self.collapse_btn.configure(text="▾")
        else:
            self.top_bar.pack_forget()
            self.bottom_bar.pack_forget()
            self.collapse_btn.configure(text="▸")
        self.root.after_idle(self._reflow_canvas)

    def _on_mic_change(self, event=None):
        idx = self.mic_combo.current()
        if idx < 0:
            return
        device_index = self.input_devices[idx][0]
        was_listening = self.listening
        self.stream.stop()
        self.stream.close()
        self._open_stream(device_index)
        self.recognizer = KaldiRecognizer(self.model, SAMPLE_RATE)
        if was_listening:
            self.stream.start()

    def _clear_transcript(self):
        for row in self.rows:
            row.destroy()
        self.rows = []
        self.current_row = None

    def _font_smaller(self):
        self.font_size = max(MIN_MSG_FONT, self.font_size - FONT_STEP)
        self.msg_font.configure(size=self.font_size)

    def _font_larger(self):
        self.font_size = min(MAX_MSG_FONT, self.font_size + FONT_STEP)
        self.msg_font.configure(size=self.font_size)

    def _on_cap_change(self, event=None):
        val = self.cap_var.get()
        self.cpu_cap = None if val == "Off" else int(val.rstrip("%"))

    # ---------- Model / devices / audio ----------

    def _init_model_and_devices(self):
        self.model = Model(MODEL_PATH)
        self.recognizer = KaldiRecognizer(self.model, SAMPLE_RATE)

        devices = sd.query_devices()
        self.input_devices = [
            (i, d["name"]) for i, d in enumerate(devices) if d["max_input_channels"] > 0
        ]
        names = [name for _, name in self.input_devices]
        self.mic_combo.configure(values=names)

        default_index = sd.default.device[0] if sd.default.device else None
        selected_pos = 0
        for pos, (idx, _name) in enumerate(self.input_devices):
            if idx == default_index:
                selected_pos = pos
                break
        if self.input_devices:
            self.mic_combo.current(selected_pos)
            self.selected_device = self.input_devices[selected_pos][0]
        else:
            self.selected_device = None

    def _open_stream(self, device):
        def callback(indata, frames, time_info, status):
            self.audio_q.put(bytes(indata))

        self.stream = sd.RawInputStream(
            samplerate=SAMPLE_RATE, blocksize=2000, dtype="int16",
            channels=1, latency="low", device=device, callback=callback,
        )
        self.stream.stop()  # start explicitly via _set_listening

    # ---------- Recognition loop (background thread) ----------

    def _recognize_loop(self):
        while self.running:
            try:
                data = self.audio_q.get(timeout=0.2)
            except queue.Empty:
                continue

            if self.recognizer.AcceptWaveform(data):
                result = json.loads(self.recognizer.Result())
                text = result.get("text", "").strip()
                if text:
                    self.text_q.put(text)
            else:
                partial = json.loads(self.recognizer.PartialResult())
                ptext = partial.get("partial", "").strip()
                if ptext:
                    self.text_q.put(("partial", ptext))

            if self.throttle_sleep > 0:
                time.sleep(self.throttle_sleep)

    # ---------- CPU monitor (background thread) ----------

    def _cpu_monitor_loop(self):
        proc = psutil.Process()
        proc.cpu_percent(None)  # prime the counter
        ncpu = psutil.cpu_count(logical=True) or 1
        while self.running:
            time.sleep(CPU_POLL_SECONDS)
            raw = proc.cpu_percent(None)
            norm = raw / ncpu
            self.cpu_percent = norm

            if self.cpu_cap is not None:
                if norm > self.cpu_cap:
                    self.throttle_sleep = min(self.throttle_sleep + 0.01, 0.25)
                elif norm < self.cpu_cap * 0.7:
                    self.throttle_sleep = max(self.throttle_sleep - 0.01, 0.0)
            else:
                self.throttle_sleep = 0.0

    def _poll_cpu_display(self):
        pct = min(100, self.cpu_percent)
        self.cpu_bar["value"] = pct
        self.cpu_label_var.set(f"{pct:.0f}%")
        self.root.after(1000, self._poll_cpu_display)

    # ---------- Text queue / rows ----------

    def _poll_text_queue(self):
        try:
            while True:
                item = self.text_q.get_nowait()
                if isinstance(item, tuple):
                    self._handle_partial(item[1])
                else:
                    self._handle_final(item)
        except queue.Empty:
            pass
        self.root.after(50, self._poll_text_queue)

    def _handle_partial(self, ptext):
        now = time.time()
        row = self._current_or_new_row(now)
        row.last_word_time = now  # actively speaking; keep this row alive
        preview = (row.text + " " + ptext).strip() if row.text else ptext
        row.msg_var.set(preview)
        self._scroll_to_bottom()

    def _handle_final(self, text):
        now = time.time()
        row = self._current_or_new_row(now)
        row.append(text, now)
        self._scroll_to_bottom()

    def _current_or_new_row(self, now):
        needs_new_row = (
            self.current_row is None
            or (now - self.current_row.last_word_time) >= SILENCE_GAP_SECONDS
        )
        if needs_new_row:
            row = Row(self.grid_frame, now, self.time_font, self.ago_font, self.msg_font)
            self.rows.append(row)
            self.current_row = row
        return self.current_row

    def _refresh_rows(self):
        now = time.time()
        for row in self.rows:
            row.refresh(now)
        self.root.after(AGO_REFRESH_MS, self._refresh_rows)

    def _sweep_old_rows(self):
        now = time.time()
        while self.rows and (now - self.rows[0].chunk_start) > RETENTION_SECONDS:
            old = self.rows.pop(0)
            if old is self.current_row:
                self.current_row = None
            old.destroy()
        self.root.after(5000, self._sweep_old_rows)

    # ---------- Shutdown ----------

    def on_close(self):
        self.running = False
        try:
            self.stream.stop()
            self.stream.close()
        except Exception:
            pass
        self.root.destroy()


def main():
    root = tk.Tk()
    app = TranscriberApp(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
