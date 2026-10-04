"""Intuitive Windows GUI for the shared DVD2HEVC job engine."""

from __future__ import annotations

import argparse
import ctypes
import copy
import json
import os
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from .paths import REPORT_ROOT
from typing import Any, Callable

from .gui_support import (WorkflowUI, destination_path, source_path, path_text, pack_scrollable, update_row, prune_rows, set_detail, job_error)

from .config import all_presets, remove_named_preset, resolve_preset, save_named_preset
from .encoders import HEVC_ENCODERS, encoder_label
from .subprocess_utils import hidden_subprocess_kwargs
from .frontend import (
    ROOT,
    cmd_cancel,
    cmd_cancel_all,
    cmd_pause_queue,
    cmd_play,
    cmd_resume_queue,
    cmd_resume_job,
    collect_iso_sources,
    create_watched_batch,
    current_cancel_generation,
    default_output_for,
    ensure_active_watchers,
    ensure_dispatcher,
    known_job_files,
    lane_progress_snapshot,
    pipeline_percent,
    pipeline_status,
    prepare_job,
    parse_audio_language_overrides,
    resolve_conversion_settings,
    queue_is_paused,
    queue_pause_reason,
    queue_prepared_job,
    read_json,
    refresh_job,
    resolve_job,
    reset_watched_batch,
    resume_watched_batch,
    stop_watched_batch,
    try_read_job,
    try_read_watched_batch,
    watched_batch_files,
    watched_batch_summary,
)
from .vlc_setup import (
    discover_vlc_base,
    discover_vlc_source,
    find_ready_vlc_root,
    inspect_private_vlc,
    managed_vlc_root,
    prepare_private_vlc,
    validate_vlc_source,
)


APP_BG = "#eaf1f5"
PANEL = "#ffffff"
INK = "#172033"
MUTED = "#5e6b82"
HEADER = "#153d4b"
HEADER_MUTED = "#c6e1e6"
ACCENT = "#008b84"
ACCENT_DARK = "#006b66"
ACCENT_LIGHT = "#d8f1ee"
TAB_BG = "#d7e6eb"
WARN = "#b36b00"
APP_USER_MODEL_ID = "DVD2HEVC.Project.GUI.1"

FIELD_HELP = {
    "Source ISO": "The decrypted DVD backup to convert. The source ISO is only read; DVD2HEVC never changes it.",
    "Output ISO": "Where the compact HEVC ISO will be written. Choose a different path from the source.",
    "Filename tags": "Automatically add (DVD) (HEVC) to generated output names for compatible library frontends. Turn this off to use a simple '- converted' name. Pasted and custom output names receive enabled tags too; the parent folder stays unchanged.",
    "Source folder": "A folder containing decrypted DVD ISO backups to find for batch conversion.",
    "Output folder": "The folder that will receive converted ISOs from this batch.",
    "Job name": "A friendly name used in the queue, progress reports, and job history.",
    "Disc label": "The short volume label embedded in the output ISO. Unsupported characters are normalized safely.",
    "Preset": "Loads a coordinated set of video, audio, and task settings. You can still adjust any field afterward.",
    "Video mode": "Target bitrate derives an exact video bitrate from the DVD. Manual CQ uses HandBrake's direction: lower is higher quality and larger.",
    "Target multiplier": "Multiplies the source-derived bitrate. 1.00× targets 25% of measured MPEG-2 video; 1.20× gives it 20% more.",
    "Bitrate control": "VBR varies bitrate with scene complexity while targeting the requested average. CBR constrains the encoder to a constant or near-constant rate.",
    "HEVC encoder": "Selects the H.265 encoder. NVENC is fastest and release-tested; other choices are checked before a job starts.",
    "Efficiency": "Trades encoding speed for compression efficiency. P1 is fastest; P7 is slowest and most space-efficient.",
    "Deinterlace": "Auto samples the pixels in each physical DVD cell. Confidently progressive cells are untouched; interlaced cells use full-motion BWDIF. Inverse telecine and within-cell Decomb are not yet release-gated.",
    "Title override": "Optionally gives the main feature or the largest N titles a different quality from the rest of the disc.",
    "Override quality": "The manual CQ used by the selected title override. Shared DVD cells cause that override to apply to the whole title set.",
    "Top N": "How many of the largest logical titles receive the override quality when 'Top N titles' is selected.",
    "General audio": "Passthrough keeps original audio. Compact-stereo preserves track slots and language metadata while converting DVD AC-3, DTS, LPCM, or MPEG audio to stereo AC-3.",
    "Stereo AC-3": "Bitrate used for compact-stereo two-channel AC-3 tracks. 256 kbit/s is the recommended default.",
    "Mono AC-3": "Bitrate used when compact audio produces a mono AC-3 track.",
    "Audio workers": "Maximum simultaneous audio encodes within a disc. More can improve throughput but uses more CPU.",
    "Pipeline depth": "At 2 or more, one hardware encode may overlap one validation pass while compact audio runs in its bounded lane. DVD2HEVC still permits only one NVENC session and one disc job, keeping the desktop usable; larger values mainly affect audio title-set preparation.",
}


class ToolTip:
    """Small delayed hover help that works for both Tk and ttk controls."""

    def __init__(self, widget: tk.Misc, text: str, *, delay_ms: int = 550) -> None:
        self.widget = widget
        self.text = text
        self.delay_ms = delay_ms
        self._after_id: str | None = None
        self._window: tk.Toplevel | None = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")
        widget.bind("<Destroy>", self._hide, add="+")

    def _schedule(self, _event: tk.Event | None = None) -> None:
        self._cancel()
        self._after_id = self.widget.after(self.delay_ms, self._show)

    def _cancel(self) -> None:
        if self._after_id is not None:
            try:
                self.widget.after_cancel(self._after_id)
            except tk.TclError:
                pass
            self._after_id = None

    def _show(self) -> None:
        self._after_id = None
        if self._window is not None or not self.widget.winfo_exists():
            return
        window = tk.Toplevel(self.widget)
        window.wm_overrideredirect(True)
        window.attributes("-topmost", True)
        label = tk.Label(
            window, text=self.text, justify="left", wraplength=360,
            bg="#fff9db", fg=INK, relief="solid", borderwidth=1,
            padx=9, pady=7, font=("Segoe UI", 9),
        )
        label.pack()
        window.update_idletasks()
        x = self.widget.winfo_pointerx() + 14
        y = self.widget.winfo_pointery() + 18
        x = min(x, self.widget.winfo_screenwidth() - window.winfo_reqwidth() - 8)
        y = min(y, self.widget.winfo_screenheight() - window.winfo_reqheight() - 8)
        window.wm_geometry(f"+{max(0, x)}+{max(0, y)}")
        self._window = window

    def _hide(self, _event: tk.Event | None = None) -> None:
        self._cancel()
        if self._window is not None:
            try:
                self._window.destroy()
            except tk.TclError:
                pass
            self._window = None


def _friendly_encoder(value: str) -> str:
    return f"{encoder_label(value)}  ({value})"


def _encoder_from_label(value: str) -> str:
    if "(" in value and value.endswith(")"):
        return value.rsplit("(", 1)[1][:-1]
    return value


class DVD2HEVCApp(WorkflowUI, tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self._init_workflow("DVD")
        self.title("DVD2HEVC")
        self._icon_photo: tk.PhotoImage | None = None
        self._native_icons: tuple[int, int] | None = None
        icon_png = ROOT / "assets" / "DVD2HEVC.png"
        if icon_png.is_file():
            try:
                self._icon_photo = tk.PhotoImage(file=str(icon_png))
                self.iconphoto(True, self._icon_photo)
            except tk.TclError:
                self._icon_photo = None
        try:
            self.iconbitmap(default=str(ROOT / "assets" / "DVD2HEVC.ico"))
        except tk.TclError:
            pass
        self.geometry("1080x780")
        self.minsize(940, 680)
        self.configure(bg=APP_BG)
        self._busy = 0
        self._job_rows: dict[str, tuple[Path, dict[str, Any]]] = {}
        self._watch_rows: dict[str, tuple[Path, dict[str, Any]]] = {}
        self._batch_sources: list[Path] = []
        self._language_overrides: dict[str, str] = {}
        self._jobs_after_id: str | None = None
        self._convert_canvas: tk.Canvas | None = None
        self._tooltips: list[ToolTip] = []
        self._vlc_setup_in_progress = False
        self._build_style()
        self._build_menu()
        self._build_header()
        self._build_tabs()
        self._setup_workflow()
        try:
            ensure_active_watchers()
        except Exception:
            # A stale or damaged watch must not prevent the GUI from opening.
            pass
        self._apply_preset("balanced")
        self.bind_all("<MouseWheel>", self._route_mousewheel, add="+")
        self.bind_all("<F1>", lambda _event: self._show_quick_help(), add="+")
        self.after(100, self._apply_windows_icons)
        self.after(500, self._refresh_jobs)

    def _build_style(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(".", font=("Segoe UI", 10), background=APP_BG, foreground=INK)
        style.configure("TFrame", background=APP_BG)
        style.configure("Panel.TFrame", background=PANEL)
        style.configure("Card.TFrame", background=PANEL, borderwidth=1, relief="solid")
        style.configure("Header.TFrame", background=HEADER)
        style.configure("TLabel", background=APP_BG, foreground=INK)
        style.configure("Panel.TLabel", background=PANEL, foreground=INK)
        style.configure("Muted.TLabel", background=PANEL, foreground=MUTED)
        style.configure("Title.TLabel", font=("Segoe UI Semibold", 20), background=HEADER, foreground="white")
        style.configure("HeaderMuted.TLabel", background=HEADER, foreground=HEADER_MUTED)
        style.configure("Queue.TLabel", font=("Segoe UI Semibold", 10), background=HEADER, foreground="white")
        style.configure("Section.TLabel", font=("Segoe UI Semibold", 11), background=PANEL, foreground=ACCENT_DARK)
        style.configure("Accent.TButton", font=("Segoe UI Semibold", 10), foreground="white", background=ACCENT)
        style.map("Accent.TButton", background=[("active", ACCENT_DARK), ("disabled", "#9cbfbd")])
        style.configure("TNotebook", background=APP_BG, borderwidth=0)
        style.configure("TNotebook.Tab", padding=(16, 9), font=("Segoe UI Semibold", 10), background=TAB_BG)
        style.map(
            "TNotebook.Tab",
            background=[("selected", PANEL), ("active", ACCENT_LIGHT)],
            foreground=[("selected", ACCENT_DARK), ("active", ACCENT_DARK)],
        )
        style.configure("Treeview", rowheight=27, background=PANEL, fieldbackground=PANEL)
        style.configure("Treeview.Heading", font=("Segoe UI Semibold", 9), background="#dcebed", foreground=INK)
        style.configure("Horizontal.TProgressbar", troughcolor="#dfe7ef", background=ACCENT)
        style.configure("Video.Horizontal.TProgressbar", troughcolor="#dfe7ef", background=ACCENT)
        style.configure("Audio.Horizontal.TProgressbar", troughcolor="#dfe7ef", background="#3b78c8")
        style.configure("Mux.Horizontal.TProgressbar", troughcolor="#dfe7ef", background="#d98b2b")

    def _build_menu(self) -> None:
        menu = tk.Menu(self)
        file_menu = tk.Menu(menu, tearoff=False)
        file_menu.add_command(label="Choose source ISO…", command=self._browse_source)
        file_menu.add_command(label="Open job folder", command=self._open_selected_job_folder)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self._close_window)
        menu.add_cascade(label="File", menu=file_menu)
        queue_menu = tk.Menu(menu, tearoff=False)
        queue_menu.add_command(label="Pause all", command=lambda: self._set_queue_paused(True))
        queue_menu.add_command(label="Resume all", command=lambda: self._set_queue_paused(False))
        queue_menu.add_command(label="Cancel all...", command=self._cancel_all)
        queue_menu.add_command(label="Refresh", command=self._refresh_jobs)
        menu.add_cascade(label="Queue", menu=queue_menu)
        help_menu = tk.Menu(menu, tearoff=False)
        help_menu.add_command(label="Quick start (F1)", command=self._show_quick_help)
        help_menu.add_command(label="HEVC VLC setup guide", command=self._open_vlc_guide)
        help_menu.add_separator()
        help_menu.add_command(label="Run dependency check", command=self._run_diagnostics)
        help_menu.add_command(label="Open documentation", command=lambda: os.startfile(str(ROOT / "README.md")))
        help_menu.add_separator()
        help_menu.add_command(label="About DVD2HEVC", command=self._show_about)
        menu.add_cascade(label="Help", menu=help_menu)
        self.config(menu=menu)

    def _build_header(self) -> None:
        header = ttk.Frame(self, style="Header.TFrame", padding=(24, 16, 24, 15))
        header.pack(fill="x")
        title = ttk.Frame(header, style="Header.TFrame")
        title.pack(side="left", fill="x", expand=True)
        ttk.Label(title, text="DVD2HEVC", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            title,
            text="Compact, menu-preserving HEVC backups with resumable verification  •  Hover over a control for help; press F1 for a quick start",
            style="HeaderMuted.TLabel",
        ).pack(anchor="w", pady=(2, 0))
        self.queue_state = ttk.Label(header, text="Queue: checking…", style="Queue.TLabel")
        self.queue_state.pack(side="right", anchor="e")
        tk.Frame(self, bg=ACCENT, height=4).pack(fill="x")

    def _build_tabs(self) -> None:
        self.busy_label = ttk.Label(self, text="Ready", foreground=MUTED, padding=(20, 4), wraplength=1000)
        self.busy_label.pack(side="bottom", fill="x")
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=20, pady=(0, 18))
        self.convert_tab = ttk.Frame(self.notebook, padding=12)
        self.batch_tab = ttk.Frame(self.notebook, padding=12)
        self.jobs_tab = ttk.Frame(self.notebook, padding=12)
        self.presets_tab = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(self.convert_tab, text="Convert")
        self.notebook.add(self.batch_tab, text="Batch queue")
        self.notebook.add(self.jobs_tab, text="Jobs & progress")
        self.notebook.add(self.presets_tab, text="Presets & tools")
        self._build_convert_tab()
        self._build_batch_tab()
        self._build_jobs_tab()
        self._build_presets_tab()

    @staticmethod
    def _panel(parent: tk.Misc, title: str, description: str = "") -> ttk.Frame:
        outer = ttk.Frame(parent, style="Card.TFrame", padding=16)
        ttk.Label(outer, text=title, style="Section.TLabel").pack(anchor="w")
        if description:
            ttk.Label(outer, text=description, style="Muted.TLabel", wraplength=920).pack(anchor="w", pady=(2, 12))
        return outer

    def _attach_help(self, widget: tk.Misc, text: str) -> tk.Misc:
        self._tooltips.append(ToolTip(widget, text))
        return widget

    def _button(self, parent: tk.Misc, *, tooltip: str, **kwargs: Any) -> ttk.Button:
        button = ttk.Button(parent, **kwargs)
        command = getattr(kwargs.get("command"), "__name__", "")
        self._buttons.setdefault(command, []).append(button)
        return self._attach_help(button, tooltip)

    def _build_convert_tab(self) -> None:
        actions = ttk.Frame(self.convert_tab, padding=(2, 8))
        actions.pack(side="bottom", fill="x")
        canvas = tk.Canvas(self.convert_tab, bg=APP_BG, highlightthickness=0)
        self._convert_canvas = canvas
        scrollbar = ttk.Scrollbar(self.convert_tab, orient="vertical", command=canvas.yview)
        body = ttk.Frame(canvas)
        body.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        window = canvas.create_window((0, 0), window=body, anchor="nw")
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window, width=event.width))
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        source = self._panel(body, "Source and destination", "Paste or browse a decrypted DVD ISO and output path. Check the final destination below.")
        source.pack(fill="x", pady=(0, 10))
        self.source_var = tk.StringVar()
        self.output_var = tk.StringVar()
        self.filename_tags_var = tk.BooleanVar(value=True)
        self.name_var = tk.StringVar()
        self.label_var = tk.StringVar()
        self._path_row(source, "Source ISO", self.source_var, self._browse_source)
        self._path_row(source, "Output path", self.output_var, self._browse_output)
        self._destination_controls(source)
        self.disc_format_var = tk.StringVar(value="UHD-BD (stock VLC)")
        format_row = ttk.Frame(source, style="Panel.TFrame")
        format_row.pack(fill="x")
        self._combo_field(format_row, 0, 0, "Disc output", self.disc_format_var,
                          ("UHD-BD (stock VLC)", "HEVC DVD (legacy player)"))
        self.disc_format_var.trace_add('write', self._disc_output_changed)
        ttk.Label(source, text="UHD-BD output plays in stock VLC with Java. Patching VLC is not necessary.",
                  style="Muted.TLabel", wraplength=760).pack(anchor="w", pady=(3,4))
        filename_tags = ttk.Checkbutton(
            source,
            text="Add source and output format tags to filenames",
            variable=self.filename_tags_var,
            command=self._filename_tags_changed,
        )
        filename_tags.pack(anchor="w", padx=(116, 0), pady=(3, 2))
        self._attach_help(filename_tags, FIELD_HELP["Filename tags"])
        meta = ttk.Frame(source, style="Panel.TFrame")
        meta.pack(fill="x", pady=(8, 0))
        job_label = ttk.Label(meta, text="Job name", style="Panel.TLabel", width=14)
        job_label.pack(side="left")
        job_entry = ttk.Entry(meta, textvariable=self.name_var, width=32)
        job_entry.pack(side="left", fill="x", expand=True, padx=(0, 16))
        disc_label = ttk.Label(meta, text="Disc label", style="Panel.TLabel", width=10)
        disc_label.pack(side="left")
        disc_entry = ttk.Entry(meta, textvariable=self.label_var, width=28)
        disc_entry.pack(side="left")
        for widget in (job_label, job_entry):
            self._attach_help(widget, FIELD_HELP["Job name"])
        for widget in (disc_label, disc_entry):
            self._attach_help(widget, FIELD_HELP["Disc label"])

        video = self._panel(body, "Video", "Target bitrate uses a measured MPEG-2 ratio directly. Shared title cells are encoded once.")
        video.pack(fill="x", pady=10)
        grid = ttk.Frame(video, style="Panel.TFrame")
        grid.pack(fill="x")
        self.preset_var = tk.StringVar(value="balanced")
        self.quality_var = tk.StringVar(value="target-bitrate")
        self.multiplier_var = tk.DoubleVar(value=1.0)
        self.bitrate_mode_var = tk.StringVar(value="vbr")
        self.encoder_var = tk.StringVar(value=_friendly_encoder("hevc_nvenc"))
        self.encoder_preset_var = tk.StringVar(value="p6")
        self.deinterlace_var = tk.StringVar(value="auto")
        self.override_mode_var = tk.StringVar(value="None")
        self.override_quality_var = tk.StringVar(value="cq:20")
        self.top_n_var = tk.IntVar(value=3)
        self._combo_field(grid, 0, 0, "Preset", self.preset_var, sorted(all_presets()), self._preset_changed)
        self._combo_field(grid, 0, 2, "Video mode", self.quality_var, ("target-bitrate", "cq:20", "cq:22", "cq:24", "cq:26", "cq:27"))
        self._spin_field(grid, 1, 0, "Target multiplier", self.multiplier_var, 0.25, 4.0, 0.05, suffix="×")
        self._combo_field(grid, 1, 2, "Bitrate control", self.bitrate_mode_var, ("vbr", "cbr"))
        self._combo_field(grid, 2, 0, "HEVC encoder", self.encoder_var, tuple(_friendly_encoder(value) for value in HEVC_ENCODERS))
        self._combo_field(grid, 2, 2, "Efficiency", self.encoder_preset_var, tuple(f"p{i}" for i in range(1, 8)))
        self._combo_field(grid, 3, 0, "Deinterlace", self.deinterlace_var, ("auto", "always", "off"))
        self._combo_field(grid, 3, 2, "Title override", self.override_mode_var, ("None", "Main title", "Top N titles"))
        self._combo_field(grid, 4, 0, "Override quality", self.override_quality_var, ("cq:18", "cq:20", "cq:22", "cq:24"))
        top_n_label = ttk.Label(grid, text="Top N", style="Panel.TLabel")
        top_n_label.grid(row=4, column=2, sticky="w", pady=6)
        top_n_spin = ttk.Spinbox(grid, textvariable=self.top_n_var, from_=1, to=99, width=8)
        self._fields["Top N"] = top_n_spin
        top_n_spin.bind("<MouseWheel>", self._safe_wheel)
        top_n_spin.grid(row=4, column=3, sticky="w", pady=6)
        self._attach_help(top_n_label, FIELD_HELP["Top N"])
        self._attach_help(top_n_spin, FIELD_HELP["Top N"])
        ttk.Label(
            grid,
            text="1.00× = 25% of source video bitrate. Lower CQ = higher quality and larger files.",
            style="Muted.TLabel",
        ).grid(row=5, column=0, columnspan=4, sticky="w", pady=(6, 0))

        audio = self._panel(body, "Audio", "The general default is passthrough. Language rules use the DVD IFO descriptors, not filename guesses.")
        audio.pack(fill="x", pady=10)
        top = ttk.Frame(audio, style="Panel.TFrame")
        top.pack(fill="x")
        self.audio_mode_var = tk.StringVar(value="passthrough")
        self.stereo_rate_var = tk.StringVar(value="256k")
        self.mono_rate_var = tk.StringVar(value="128k")
        self.audio_workers_var = tk.IntVar(value=2)
        self.pipeline_depth_var = tk.IntVar(value=2)
        self._combo_field(top, 0, 0, "General audio", self.audio_mode_var, ("passthrough", "compact-stereo"))
        self._combo_field(top, 0, 2, "Stereo AC-3", self.stereo_rate_var, ("192k", "224k", "256k", "320k", "384k"))
        self._combo_field(top, 1, 0, "Mono AC-3", self.mono_rate_var, ("96k", "112k", "128k", "160k", "192k"))
        self._spin_field(top, 1, 2, "Audio workers", self.audio_workers_var, 1, 8, 1)
        self._spin_field(top, 2, 0, "Pipeline depth", self.pipeline_depth_var, 1, 8, 1)
        rules = ttk.Frame(audio, style="Panel.TFrame")
        rules.pack(fill="x", pady=(12, 0))
        self.language_tree = ttk.Treeview(rules, columns=("language", "action"), show="headings", height=4)
        self.language_tree.heading("language", text="Language / fallback")
        self.language_tree.heading("action", text="Action")
        self.language_tree.column("language", width=210)
        self.language_tree.column("action", width=210)
        self.language_tree.pack(side="left", fill="x", expand=True)
        self._attach_help(
            self.language_tree,
            "Optional per-language audio rules. For example, keep English unchanged while compacting other languages to stereo. Rules use DVD language metadata.",
        )
        buttons = ttk.Frame(rules, style="Panel.TFrame")
        buttons.pack(side="left", padx=(10, 0), anchor="n")
        self._button(
            buttons, text="Add rule…", command=self._add_language_rule,
            tooltip="Add an audio action for one DVD language code, or a fallback rule for tracks without a matching language.",
        ).pack(fill="x")
        self._button(
            buttons, text="Remove", command=self._remove_language_rule,
            tooltip="Remove the selected language-specific audio rule.",
        ).pack(fill="x", pady=(6, 0))

        self._button(
            actions, text="Plan only", command=self._plan_only,
            tooltip="Inspect the disc and validate every setting, then write a plan report without starting a conversion.",
        ).pack(side="right", padx=(8, 0))
        self._button(
            actions, text="Add to queue", style="Accent.TButton", command=self._queue_one,
            tooltip="Validate this conversion and add it to the background queue. The source ISO is never modified.",
        ).pack(side="right")


    def _apply_windows_icons(self) -> None:
        """Give the Python GUI a real Windows identity and big/small HWND icons."""
        if sys.platform != "win32":
            return
        icon_path = str(ROOT / "assets" / "DVD2HEVC.ico")
        try:
            if self._icon_photo is not None:
                self.iconphoto(True, self._icon_photo)
            self.iconbitmap(default=icon_path)
            user32 = ctypes.windll.user32
            image_icon, load_from_file = 1, 0x0010
            user32.LoadImageW.restype = ctypes.c_void_p
            user32.LoadImageW.argtypes = (
                ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint,
                ctypes.c_int, ctypes.c_int, ctypes.c_uint,
            )
            big = int(user32.LoadImageW(None, icon_path, image_icon, 64, 64, load_from_file) or 0)
            small = int(user32.LoadImageW(None, icon_path, image_icon, 32, 32, load_from_file) or 0)
            if big and small:
                client_hwnd = int(self.winfo_id())
                user32.GetParent.restype = ctypes.c_void_p
                user32.GetParent.argtypes = (ctypes.c_void_p,)
                wrapper_hwnd = int(user32.GetParent(ctypes.c_void_p(client_hwnd)) or 0)
                user32.SendMessageW.restype = ctypes.c_ssize_t
                user32.SendMessageW.argtypes = (
                    ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t,
                )
                for hwnd in {client_hwnd, wrapper_hwnd} - {0}:
                    user32.SendMessageW(ctypes.c_void_p(hwnd), 0x0080, 1, big)  # WM_SETICON / ICON_BIG
                    user32.SendMessageW(ctypes.c_void_p(hwnd), 0x0080, 0, small)  # WM_SETICON / ICON_SMALL
                # Tk's taskbar/titlebar owner is the native wrapper, not the
                # client handle returned by winfo_id(). Set its class icons too.
                if wrapper_hwnd:
                    set_class_icon = getattr(user32, "SetClassLongPtrW", user32.SetClassLongW)
                    set_class_icon.restype = ctypes.c_void_p
                    set_class_icon.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p)
                    set_class_icon(ctypes.c_void_p(wrapper_hwnd), -14, ctypes.c_void_p(big))  # GCLP_HICON
                    set_class_icon(ctypes.c_void_p(wrapper_hwnd), -34, ctypes.c_void_p(small))  # GCLP_HICONSM
                self._native_icons = (big, small)
        except (AttributeError, OSError, tk.TclError):
            pass

    def _path_row(self, parent: ttk.Frame, label: str, variable: tk.StringVar, command: Callable[[], None]) -> None:
        row = ttk.Frame(parent, style="Panel.TFrame")
        row.pack(fill="x", pady=4)
        help_text = FIELD_HELP.get(label, f"Choose the {label.lower()} used by this operation.")
        field_label = ttk.Label(row, text=label, style="Panel.TLabel", width=14)
        field_label.pack(side="left")
        entry = ttk.Entry(row, textvariable=variable)
        entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        browse = self._button(row, text="Browse…", command=command, tooltip=help_text)
        browse.pack(side="left")
        self._attach_help(field_label, help_text)
        self._attach_help(entry, help_text)

    def _combo_field(self, parent: ttk.Frame, row: int, column: int, label: str, variable: tk.Variable, values: tuple[str, ...] | list[str], callback: Callable[..., None] | None = None) -> None:
        help_text = FIELD_HELP.get(label, f"Choose the {label.lower()} setting.")
        field_label = ttk.Label(parent, text=label, style="Panel.TLabel")
        field_label.grid(row=row, column=column, sticky="w", pady=6, padx=(0, 8))
        box = ttk.Combobox(parent, textvariable=variable, values=values, state="readonly", width=27)
        self._fields[label] = box
        box.bind("<MouseWheel>", self._safe_wheel)
        box.grid(row=row, column=column + 1, sticky="ew", pady=6, padx=(0, 24))
        self._attach_help(field_label, help_text)
        self._attach_help(box, help_text)
        if callback:
            box.bind("<<ComboboxSelected>>", callback)
        parent.columnconfigure(column + 1, weight=1)

    def _spin_field(self, parent: ttk.Frame, row: int, column: int, label: str, variable: tk.Variable, start: float, end: float, increment: float, suffix: str = "") -> None:
        help_text = FIELD_HELP.get(label, f"Adjust the {label.lower()} setting.")
        field_label = ttk.Label(parent, text=label, style="Panel.TLabel")
        field_label.grid(row=row, column=column, sticky="w", pady=6, padx=(0, 8))
        holder = ttk.Frame(parent, style="Panel.TFrame")
        holder.grid(row=row, column=column + 1, sticky="w", pady=6, padx=(0, 24))
        spinbox = ttk.Spinbox(holder, textvariable=variable, from_=start, to=end, increment=increment, width=9)
        self._fields[label] = spinbox
        spinbox.bind("<MouseWheel>", self._safe_wheel)
        spinbox.pack(side="left")
        self._attach_help(field_label, help_text)
        self._attach_help(spinbox, help_text)
        if suffix:
            ttk.Label(holder, text=suffix, style="Panel.TLabel").pack(side="left", padx=(3, 0))

    def _build_batch_tab(self) -> None:
        panel = self._panel(
            self._scrollable_tab(self.batch_tab),
            "Build or watch a conversion queue",
            "Watching continues after closing this GUI. Output is a parent folder (blank for Queue all = beside each source). Planning and conversion run one disc at a time.",
        )
        panel.pack(fill="both", expand=True)
        self.batch_source_var = tk.StringVar()
        self.batch_output_var = tk.StringVar()
        self.batch_recursive_var = tk.BooleanVar(value=False)
        self._path_row(panel, "Source folder", self.batch_source_var, self._browse_batch_source)
        self._path_row(panel, "Output folder", self.batch_output_var, self._browse_batch_output)
        options = ttk.Frame(panel, style="Panel.TFrame")
        options.pack(fill="x", pady=8)
        recursive = ttk.Checkbutton(options, text="Include subfolders", variable=self.batch_recursive_var)
        recursive.pack(side="left")
        self._attach_help(recursive, "Also look for ISO backups inside folders below the selected source folder.")
        batch_filename_tags = ttk.Checkbutton(
            options,
            text="Add (DVD) (HEVC) filename tags",
            variable=self.filename_tags_var,
            command=self._filename_tags_changed,
        )
        batch_filename_tags.pack(side="left", padx=(18, 0))
        self._attach_help(batch_filename_tags, FIELD_HELP["Filename tags"])
        self._button(
            options, text="Scan folder", command=self._scan_batch,
            tooltip="Find ISO backups and preview their planned output names. Scanning does not queue or modify anything.",
        ).pack(side="right")
        self.batch_tree = ttk.Treeview(
            panel, columns=("status", "source", "output"), show="headings", height=4,
        )
        self.batch_tree.heading("status", text="Status")
        self.batch_tree.column("status", width=110)
        self.batch_tree.heading("source", text="Source ISO")
        self.batch_tree.heading("output", text="Final output path")
        self.batch_tree.column("source", width=600)
        self.batch_tree.column("output", width=260)
        pack_scrollable(self.batch_tree, fill="both", expand=True, pady=(6, 10))
        self._attach_help(self.batch_tree, "Review the discs found by the last scan and the output name planned for each one.")
        footer = ttk.Frame(panel, style="Panel.TFrame")
        footer.pack(fill="x")
        self.batch_summary = ttk.Label(footer, text="No folder scanned", style="Muted.TLabel")
        self.batch_summary.pack(side="left")
        self._button(
            footer, text="Start watched batch", style="Accent.TButton", command=self._start_watched_batch,
            tooltip="Start monitoring this source folder and resume the global queue if it is paused. New ISOs are queued only after remaining unchanged for 60 seconds and no backup process still has them open for writing.",
        ).pack(side="right")
        self._button(
            footer, text="Queue all shown (0)", command=self._queue_batch,
            tooltip="Validate and queue only the discs shown by the current scan. Later files are not discovered automatically.",
        ).pack(side="right", padx=(0, 8))

        watch_panel = ttk.Frame(panel, style="Panel.TFrame")
        watch_panel.pack(fill="x", pady=(14, 0))
        ttk.Separator(watch_panel).pack(fill="x", pady=(0, 9))
        watch_header = ttk.Frame(watch_panel, style="Panel.TFrame")
        watch_header.pack(fill="x")
        ttk.Label(watch_header, text="Watched batches", style="Section.TLabel").pack(side="left")
        self._button(
            watch_header, text="Reset selected", command=self._reset_selected_watch,
            tooltip="Stop the selected watcher and start a new one with the same folders and settings but an empty discovery history. Existing output ISOs are never overwritten.",
        ).pack(side="right")
        self._button(
            watch_header, text="Stop selected", command=self._stop_selected_watch,
            tooltip="Stop discovering new ISOs. Jobs already queued or running continue normally.",
        ).pack(side="right", padx=(0, 6))
        self._button(
            watch_header, text="Resume selected", command=self._resume_selected_watch,
            tooltip="Restart a stopped, interrupted, or failed watcher using its existing discovery history. Completed outputs are not queued again.",
        ).pack(side="right", padx=(0, 6))
        watch_columns = ("status", "source", "found", "active", "waiting", "done", "attention")
        self.watch_tree = ttk.Treeview(watch_panel, columns=watch_columns, show="headings", height=2)
        watch_widths = {
            "status": 82, "source": 360, "found": 58, "active": 58,
            "waiting": 62, "done": 54, "attention": 72,
        }
        for column in watch_columns:
            self.watch_tree.heading(column, text=column.title())
            self.watch_tree.column(column, width=watch_widths[column], stretch=column == "source")
        pack_scrollable(self.watch_tree, fill="x", pady=(7, 0))
        self.watch_tree.bind("<<TreeviewSelect>>", lambda _event: self._show_watch_attention())
        self.watch_detail = tk.Text(watch_panel, height=3, wrap="word", relief="flat", bg="#f7f9fc", font=("Consolas", 9))
        pack_scrollable(self.watch_detail, fill="x", pady=(4, 0))
        self.watch_detail.configure(state="disabled")
        self._attach_help(
            self.watch_tree,
            "Found is every ISO remembered by the watcher. Active is the one disc being planned or converted; Waiting discs are retained and admitted one at a time. Done includes successful conversions and outputs that already existed. Attention counts discs needing intervention.",
        )

    def _build_jobs_tab(self) -> None:
        panel = self._panel(self.jobs_tab, "Conversion jobs", "Conversions run one disc at a time in the background; the computer remains usable.")
        panel.pack(fill="both", expand=True)
        toolbar = ttk.Frame(panel, style="Panel.TFrame")
        toolbar.pack(fill="x", pady=(0, 8))
        self._button(
            toolbar, text="Refresh", command=self._refresh_jobs,
            tooltip="Reload job state and progress reports from disk now.",
        ).pack(side="left")
        self.pause_button = self._button(
            toolbar, text="Pause after current", command=self._toggle_queue,
            tooltip="Stop starting later discs and pause watched-folder discovery. The active conversion is not interrupted.",
        )
        self.pause_button.pack(side="left", padx=6)
        self._button(
            toolbar, text="Cancel selected", command=self._cancel_selected,
            tooltip="Withdraw a queued job, or immediately stop a running job and all of its helper processes. Reusable completed work is retained.",
        ).pack(side="left")
        self._button(
            toolbar, text="Cancel all...", command=self._cancel_all,
            tooltip="Stop every watched batch, immediately terminate the active conversion tree, cancel all waiting jobs, and leave the queue paused.",
        ).pack(side="left", padx=(6, 0))
        self._button(
            toolbar, text="Resume selected", command=self._resume_selected,
            tooltip="Requeue a retained failed or canceled job so it can continue from reusable completed work.",
        ).pack(side="left", padx=6)
        self._button(
            toolbar, text="Play output", command=self._play_selected,
            tooltip="Open UHD-BD in stock VLC with Java, or legacy HEVC DVD in its verified private player.",
        ).pack(side="right")
        self._button(
            toolbar, text="Open folder", command=self._open_selected_job_folder,
            tooltip="Open the selected job's reports and working folder in File Explorer.",
        ).pack(side="right", padx=6)
        columns = ("status", "progress", "source", "quality", "audio", "output")
        self.jobs_tree = ttk.Treeview(panel, columns=columns, show="headings", height=5)
        widths = {"status": 82, "progress": 80, "source": 220, "quality": 88, "audio": 125, "output": 330}
        for column in columns:
            self.jobs_tree.heading(column, text=column.title())
            self.jobs_tree.column(column, width=widths[column], stretch=column in {"source", "output"})
        jobs_holder = pack_scrollable(self.jobs_tree, fill="both", expand=True)
        self._attach_help(self.jobs_tree, "Select a conversion to see its overall progress, simultaneous task lanes, and detailed status below.")
        self.jobs_tree.bind("<<TreeviewSelect>>", lambda _event: self._show_job_detail())

        progress = ttk.Frame(panel, style="Panel.TFrame")
        progress.pack(fill="x", pady=(12, 0))
        self.overall_label = ttk.Label(progress, text="Select a job", style="Panel.TLabel")
        self.overall_label.grid(row=0, column=0, sticky="w")
        self.overall_progress = ttk.Progressbar(progress, maximum=100)
        self.overall_progress.grid(row=0, column=1, sticky="ew", padx=(10, 0))
        self.lane_bars: dict[str, ttk.Progressbar] = {}
        self.lane_labels: dict[str, ttk.Label] = {}
        for index, lane in enumerate(("video", "audio", "mux"), start=1):
            label = ttk.Label(progress, text=f"{lane.title()}: waiting", style="Muted.TLabel")
            label.grid(row=index, column=0, sticky="w", pady=(5, 0))
            bar = ttk.Progressbar(progress, maximum=100, style=f"{lane.title()}.Horizontal.TProgressbar")
            bar.grid(row=index, column=1, sticky="ew", padx=(10, 0), pady=(5, 0))
            self.lane_labels[lane] = label
            self.lane_bars[lane] = bar
        progress.columnconfigure(1, weight=1)
        self.job_detail = tk.Text(panel, height=7, wrap="word", relief="flat", bg="#f7f9fc", fg=INK, font=("Consolas", 9))
        details_holder = pack_scrollable(self.job_detail, fill="x", pady=(10, 0))
        self.job_detail.configure(state="disabled")
        detail_actions = ttk.Frame(panel, style="Panel.TFrame")
        detail_actions.pack(fill="x", pady=(4, 0))
        self._button(detail_actions, text="Open log", command=self._open_selected_log, tooltip="Open the selected job's diagnostic log.").pack(side="left")
        self._button(detail_actions, text="Copy details", command=self._copy_job_details, tooltip="Copy full paths, settings and error details.").pack(side="left", padx=6)
        for footer in (detail_actions, details_holder, progress):
            footer.pack_configure(side="bottom", before=jobs_holder)
        for state, background in {
            "running": "#e2f5f2", "queued": "#fff4dd", "planned": "#edf3f6",
            "passed": "#e8f5e5", "failed": "#fde8e6", "canceled": "#f0edf2",
        }.items():
            self.jobs_tree.tag_configure(state, background=background)

    def _build_presets_tab(self) -> None:
        left = self._panel(self.presets_tab, "Reusable presets", "Built-ins are protected; saved presets live in your user configuration.")
        left.pack(side="left", fill="both", expand=True, padx=(0, 6))
        self.preset_tree = ttk.Treeview(left, columns=("name", "quality", "encoder", "audio", "kind"), show="headings")
        for column, width in (("name", 180), ("quality", 100), ("encoder", 130), ("audio", 140), ("kind", 90)):
            self.preset_tree.heading(column, text=column.title())
            self.preset_tree.column(column, width=width)
        pack_scrollable(self.preset_tree, fill="both", expand=True)
        buttons = ttk.Frame(left, style="Panel.TFrame")
        buttons.pack(fill="x", pady=(10, 0))
        self._button(
            buttons, text="Load selected", command=self._load_selected_preset,
            tooltip="Apply the selected preset to every configurable field on the Convert tab.",
        ).pack(side="left")
        self._button(
            buttons, text="Save current as…", command=self._save_current_preset,
            tooltip="Save the current Convert settings as a reusable personal preset.",
        ).pack(side="left", padx=6)
        self._button(
            buttons, text="Remove selected", command=self._remove_selected_preset,
            tooltip="Delete a selected personal preset. Built-in presets are protected.",
        ).pack(side="left")
        tools = self._panel(self.presets_tab, "Tools & playback", "UHD-BD needs stock VLC with Java; the private player is only for legacy HEVC DVD output.")
        tools.pack(side="left", fill="y", padx=(6, 0))
        self._button(
            tools, text="Check dependencies", command=self._run_diagnostics,
            tooltip="Check FFmpeg, HEVC encoders, authoring tools, the native inspector, and the private VLC player without changing anything.",
        ).pack(fill="x")
        self._button(
            tools, text="Open reports", command=lambda: self._open_path(REPORT_ROOT),
            tooltip="Open conversion plans, validation results, logs, and background job state in File Explorer.",
        ).pack(fill="x", pady=6)
        self._button(
            tools, text="Open project folder", command=lambda: self._open_path(ROOT),
            tooltip="Open the DVD2HEVC installation folder in File Explorer.",
        ).pack(fill="x")
        ttk.Separator(tools).pack(fill="x", pady=14)
        self.vlc_status_label = ttk.Label(
            tools, text="HEVC DVD playback: checking…", style="Muted.TLabel", wraplength=270,
        )
        self.vlc_status_label.pack(anchor="w", fill="x", pady=(0, 8))
        self._attach_help(
            self.vlc_status_label,
            "UHD-BD output does not require VLC patching. The private player below is optional for legacy HEVC DVD output.",
        )
        self.vlc_setup_button = self._button(
            tools, text="Legacy HEVC DVD player setup…", command=self._setup_vlc,
            tooltip="Safely verify or prepare a separate VLC 3.0.23 copy that can play HEVC DVD backups. Repeated clicks are harmless.",
        )
        self.vlc_setup_button.pack(fill="x")
        self._button(
            tools, text="Open VLC setup guide", command=self._open_vlc_guide,
            tooltip="Open the detailed requirements, source-build, verification, and licensing instructions.",
        ).pack(fill="x", pady=(6, 0))
        ttk.Label(
            tools,
            text="Hardware encoders are availability-tested before a job is accepted. libx265 is supported but much slower.",
            style="Muted.TLabel",
            wraplength=250,
        ).pack(anchor="w", pady=(16, 0))
        self._refresh_presets()
        self._refresh_vlc_status()

    def _browse_source(self) -> None:
        value = filedialog.askopenfilename(title="Choose decrypted DVD ISO", parent=self, filetypes=(("DVD ISO", "*.iso"),))
        if value:
            self.source_var.set(value)
            self._update_path_preview()

    def _filename_tags_changed(self, *_args: Any) -> None:
        self._paths_changed()

    def _browse_output(self) -> None:
        if self.destination_mode_var.get() != "Full output path":
            value = filedialog.askdirectory(title="Choose parent folder for converted backups", parent=self)
        elif self._output_format() == "iso":
            value = filedialog.asksaveasfilename(title="Name converted ISO", parent=self,
                defaultextension=".iso", filetypes=(("ISO image", "*.iso"),))
        else:
            value = filedialog.askdirectory(title="Choose the final output folder (its name receives enabled tags)", parent=self, mustexist=False)
        if value:
            self.output_var.set(value)
            self._update_path_preview()

    def _browse_batch_source(self) -> None:
        if value := filedialog.askdirectory(title="Choose folder containing DVD ISOs"):
            self.batch_source_var.set(value)

    def _browse_batch_output(self) -> None:
        if value := filedialog.askdirectory(title="Choose output folder"):
            self.batch_output_var.set(value)

    def _preset_changed(self, _event: Any = None) -> None:
        self._apply_preset(self.preset_var.get())

    def _apply_preset(self, name: str) -> None:
        try:
            preset = resolve_preset(name)
        except Exception as exc:
            messagebox.showerror("Load preset", str(exc), parent=self)
            return
        self._loading_preset = True
        self.preset_var.set(name)
        self.quality_var.set(str(preset.get("quality") or "target-bitrate"))
        self.multiplier_var.set(float(
            preset.get("target_bitrate_multiplier") or preset.get("auto_cq_multiplier") or 1.0
        ))
        self.bitrate_mode_var.set(str(preset.get("bitrate_mode") or "vbr"))
        self.encoder_var.set(_friendly_encoder(str(preset.get("encoder") or "hevc_nvenc")))
        self.encoder_preset_var.set(str(preset.get("encoder_preset") or "p6"))
        self.deinterlace_var.set(str(preset.get("deinterlace") or "auto"))
        self.audio_mode_var.set(str(preset.get("audio_mode") or "passthrough"))
        self.stereo_rate_var.set(str(preset.get("stereo_audio_bitrate") or "256k"))
        self.mono_rate_var.set(str(preset.get("mono_audio_bitrate") or "128k"))
        self.audio_workers_var.set(int(preset.get("audio_workers") or 2))
        self.pipeline_depth_var.set(int(preset.get("pipeline_depth") or 2))
        self.override_quality_var.set("cq:20")
        self.top_n_var.set(3)
        self.filename_tags_var.set(bool(preset.get("add_filename_tags", True)))
        self.disc_format_var.set("HEVC DVD (legacy player)" if preset.get('output_format') == 'dvd-hevc' else "UHD-BD (stock VLC)")
        if preset.get("main_title_quality"):
            self.override_mode_var.set("Main title")
            self.override_quality_var.set(str(preset["main_title_quality"]))
        elif preset.get("top_n_quality"):
            self.override_mode_var.set("Top N titles")
            self.override_quality_var.set(str(preset["top_n_quality"]))
            self.top_n_var.set(int(preset.get("top_n_count") or 3))
        else:
            self.override_mode_var.set("None")
        self._language_overrides = dict(preset.get("audio_language_overrides") or {})
        self._refresh_language_tree()
        self._loading_preset = False
        self.preset_var.set(name)
        self._update_control_states()
        self._update_path_preview()

    def _dvd_output(self):
        return 'dvd-hevc' if self.disc_format_var.get() == 'HEVC DVD (legacy player)' else 'uhd-bd'

    def _disc_output_changed(self,*args):
        self._paths_changed(*args)
        if hasattr(self,'vlc_status_label'):self._refresh_vlc_status()

    def _settings_namespace(self, source: Path, output: Path | None = None) -> argparse.Namespace:
        mode = self.override_mode_var.get()
        return argparse.Namespace(
            source=str(source), output=str(output) if output else None,
            output_format=self._dvd_output(), tsmuxer=None, udf_tool=None, java_home=None,
            preset=None, gui_preset_name=self.preset_var.get(), settings_are_explicit=True, quality=self.quality_var.get(),
            target_bitrate_multiplier=float(self.multiplier_var.get()),
            bitrate_mode=self.bitrate_mode_var.get(),
            main_title_quality=self.override_quality_var.get() if mode == "Main title" else None,
            top_n_quality=self.override_quality_var.get() if mode == "Top N titles" else None,
            top_n_count=int(self.top_n_var.get()) if mode == "Top N titles" else 0,
            encoder=_encoder_from_label(self.encoder_var.get()), encoder_preset=self.encoder_preset_var.get(),
            deinterlace=self.deinterlace_var.get(), audio_mode=self.audio_mode_var.get(),
            audio_language=[f"{key}={value}" for key, value in self._language_overrides.items()],
            stereo_audio_bitrate=self.stereo_rate_var.get(), mono_audio_bitrate=self.mono_rate_var.get(),
            audio_workers=int(self.audio_workers_var.get()), pipeline_depth=int(self.pipeline_depth_var.get()),
            label=self.label_var.get() or None, work_dir=None, vlc_root=None,
            name=self.name_var.get() or None, resume=False,
            add_filename_tags=bool(self.filename_tags_var.get()),
        )

    def _validate_paths(self):
        self._update_path_preview()
        source, output = self._resolved_paths()
        if not source.is_file() or source.suffix.lower() != ".iso":
            raise ValueError("Choose an existing decrypted DVD ISO first.")
        if output.exists():
            raise ValueError(f"Output already exists; choose another destination: {output}")
        return source, output

    def _prepare_with_status(self, args, **kwargs):
        def progress(text):
            self._post(lambda: self.busy_label.configure(text=text))
        return prepare_job(args, progress=progress, **kwargs)

    def _queue_one(self) -> None:
        try:
            source, output = self._validate_paths()
            args = self._settings_namespace(source, output)
            resolve_conversion_settings(args)
        except Exception as exc:
            messagebox.showerror("Invalid settings", str(exc), parent=self)
            return
        self._background("Planning and checking disc…", lambda: self._prepare_and_queue(args), key="submission")

    def _prepare_and_queue(self, args: argparse.Namespace) -> str:
        job_path, job = self._prepare_with_status(args)
        if not queue_prepared_job(job_path, job):
            return f"Canceled {job['id']} before it entered the queue"
        ensure_dispatcher()
        self._post(lambda: self.notebook.select(self.jobs_tab))
        return f"Queued {job['id']}"

    def _plan_only(self) -> None:
        try:
            source, output = self._validate_paths()
            args = self._settings_namespace(source, output)
            resolve_conversion_settings(args)
        except Exception as exc:
            messagebox.showerror("Invalid settings", str(exc), parent=self)
            return
        def work() -> str:
            _path, job = self._prepare_with_status(args)
            return (
                f"Plan passed: {job['id']}\n"
                f"Titles: {job['plan_summary'].get('global_titles', 0)}; "
                f"physical cells: {job['plan_summary'].get('unique_physical_cells', 0)}\n"
                "The job remains planned and can be resumed or canceled from Jobs & progress."
            )
        self._background("Running preflight and compatibility plan…", work, show_result=True, key="submission")

    def _scan_batch(self) -> None:
        try:
            source = self._batch_source()
            recursive = bool(self.batch_recursive_var.get())
        except Exception as exc:
            messagebox.showerror("Scan folder", str(exc), parent=self)
            return
        def show(sources):
            if source != self._batch_source() or recursive != bool(self.batch_recursive_var.get()):
                return
            self._batch_sources = sources
            self._render_batch()
        def work() -> str:
            sources = collect_iso_sources([str(source)], recursive=recursive)
            self._post(lambda: show(sources))
            return f"Found {len(sources)} ISO backup(s)"
        self._background("Scanning folder…", work)

    def _render_batch(self) -> None:
        try:
            pairs = self._batch_paths()
            for source, output in pairs:
                update_row(self.batch_tree, str(source), ("output exists" if output.exists() else "ready", str(source), str(output)))
            prune_rows(self.batch_tree, {str(source) for source in self._batch_sources})
            self.batch_summary.configure(text=f"{len(pairs)} disc(s) shown; existing outputs are skipped")
            self._set_batch_count(len(pairs))
        except (OSError, ValueError) as exc:
            prune_rows(self.batch_tree, set())
            self.batch_summary.configure(text=str(exc))
            self._set_batch_count(0)

    def _queue_batch(self) -> None:
        if not self._batch_sources:
            messagebox.showinfo("Batch queue", "Scan a source folder first.", parent=self)
            return
        try:
            pairs = self._batch_paths()
            template = self._settings_namespace(pairs[0][0])
            resolve_conversion_settings(template)
        except Exception as exc:
            messagebox.showerror("Batch queue", str(exc), parent=self)
            return
        # Capture cancellation state at the click, before launching the worker.
        batch_started_paused = queue_is_paused()
        batch_generation = current_cancel_generation()
        def work() -> str:
            queued, issues = 0, []
            for source, output in pairs:
                if current_cancel_generation() != batch_generation or (not batch_started_paused and queue_is_paused()):
                    issues.append("Batch stopped; remaining discs were not submitted.")
                    break
                try:
                    if output.exists():
                        issues.append(f"{source.name}: skipped; output exists ({output})")
                        continue
                    args = copy.deepcopy(template)
                    args.source, args.output, args.name = str(source), str(output), source.stem
                    job_path, job = self._prepare_with_status(args, source=source, output=output, requested_name=source.stem)
                    if queue_prepared_job(job_path, job):
                        queued += 1
                    else:
                        issues.append(f"{source.name}: canceled before entering the queue")
                except Exception as exc:
                    issues.append(f"{source.name}: {exc}")
            if queued:
                ensure_dispatcher()
            self._post(lambda: self.notebook.select(self.jobs_tab))
            return f"Queued {queued} of {len(pairs)} discs." + ("\n\n" + "\n".join(issues) if issues else "")
        self._background("Planning batch…", work, show_result=True, key="submission")

    def _start_watched_batch(self) -> None:
        if not self.batch_source_var.get().strip() or not self.batch_output_var.get().strip():
            messagebox.showerror(
                "Watched batch", "Choose both a source folder and an output folder first.", parent=self,
            )
            return
        try:
            source_dir = self._batch_source()
            output_dir = Path(path_text(self.batch_output_var.get())).expanduser().resolve()
            if output_dir.suffix.lower() == ".iso" or (output_dir.exists() and not output_dir.is_dir()):
                raise ValueError("Choose an output parent folder, not an ISO filename.")
            args = self._settings_namespace(source_dir / "watched-source.iso")
            resolve_conversion_settings(args)
        except Exception as exc:
            messagebox.showerror("Watched batch", str(exc), parent=self)
            return
        args.source_dir = str(source_dir)
        args.output_dir = str(output_dir)
        args.recursive = bool(self.batch_recursive_var.get())
        args.poll_seconds = 15.0
        args.settle_seconds = 60.0
        args.name_prefix = None

        def work() -> str:
            _path, watch = create_watched_batch(args)
            self._post(self._refresh_watches)
            message = (
                f"Watching {Path(watch['source_dir']).name}: new ISOs will be queued "
                "after remaining unchanged for 60 seconds"
            )
            if watch.get("queue_resumed_at_start"):
                message += "; the paused queue was resumed"
            return message

        self._background("Starting persistent folder watch…", work, show_result=True, key="submission")

    def _selected_watch_id(self) -> str | None:
        selected = self.watch_tree.selection() if hasattr(self, "watch_tree") else ()
        return str(selected[0]) if selected else None

    def _stop_selected_watch(self) -> None:
        identifier = self._selected_watch_id()
        if not identifier:
            messagebox.showinfo("Watched batch", "Select a watched batch first.", parent=self)
            return
        if not messagebox.askyesno(
            "Stop watched batch?",
            "Stop discovering new ISOs? Jobs already queued or running will continue.",
            parent=self,
        ):
            return
        self._background(
            "Stopping watched batch…",
            lambda: (stop_watched_batch(identifier), "Watched batch stopped; existing jobs continue")[1],
        )

    def _reset_selected_watch(self) -> None:
        identifier = self._selected_watch_id()
        if not identifier:
            messagebox.showinfo("Watched batch", "Select a watched batch first.", parent=self)
            return
        if not messagebox.askyesno(
            "Start a fresh watch?",
            "This stops the selected watcher and starts a new one with an empty discovery history.\n\n"
            "Existing output ISOs are kept and marked as already handled; they are never overwritten. "
            "Delete or move an output first only if you intentionally want that source queued again.",
            parent=self,
        ):
            return
        self._background(
            "Resetting watched batch history…",
            lambda: (reset_watched_batch(identifier), "Fresh watched batch started with an empty history")[1],
            show_result=True,
        )

    def _resume_selected_watch(self) -> None:
        identifier = self._selected_watch_id()
        if not identifier:
            messagebox.showinfo("Watched batch", "Select a watched batch first.", parent=self)
            return
        self._background(
            "Resuming watched batch…",
            lambda: (
                resume_watched_batch(identifier),
                "Watched batch resumed with its existing discovery history",
            )[1],
            show_result=True,
        )

    def _refresh_watches(self) -> None:
        if not hasattr(self, "watch_tree"):
            return
        selected = self._selected_watch_id()
        self._watch_rows.clear()
        for path in watched_batch_files():
            watch = try_read_watched_batch(path)
            if not watch:
                continue
            watch_id = str(watch.get("id"))
            summary = watched_batch_summary(watch)
            displayed_status = str(watch.get("status", "unknown"))
            if displayed_status == "active" and queue_is_paused():
                displayed_status = "paused"
            self._watch_rows[watch_id] = (path, watch)
            update_row(self.watch_tree, watch_id,
                values=(
                    displayed_status, watch.get("source_dir", ""),
                    summary["detected"], summary["active"], summary["pending"],
                    summary["done"], summary["attention"],
                ),
            )
        prune_rows(self.watch_tree, self._watch_rows)
        self._show_watch_attention()

    def _show_watch_attention(self):
        identifier = self._selected_watch_id()
        if identifier not in self._watch_rows:
            set_detail(self.watch_detail, "Select a watched batch to see its destination and files needing attention.")
            return
        _path, watch = self._watch_rows[identifier]
        lines = [f"Output folder: {watch.get('output_dir')}"]
        if watch.get("error"):
            lines.append(f"Watch error: {watch['error']}")
        for key, entry in (watch.get("ledger") or {}).items():
            if entry.get("error") or entry.get("status") in {"failed", "canceled", "planning-failed", "output-name-conflict", "source-changed"}:
                lines.append(f"{entry.get('source', key)}: {entry.get('status')} — {entry.get('error') or 'Select the associated job for details.'}")
        if len(lines) == 1:
            lines.append("No files currently need attention.")
        set_detail(self.watch_detail, "\n".join(lines))

    def _add_language_rule(self) -> None:
        dialog = tk.Toplevel(self)
        dialog.title("Audio language rule")
        dialog.transient(self)
        dialog.resizable(False, False)
        panel = ttk.Frame(dialog, padding=16)
        panel.pack(fill="both", expand=True)
        language = tk.StringVar(value="eng")
        action = tk.StringVar(value="compact-stereo")
        ttk.Label(panel, text="Language code (2–3 letters), und or other:").grid(row=0, column=0, sticky="w")
        selector = ttk.Combobox(panel, textvariable=language, values=("eng", "fra", "deu", "spa", "ita", "jpn", "zho", "und", "other"))
        selector.grid(row=1, column=0, sticky="ew", pady=(4, 10))
        ttk.Label(panel, text="Action:").grid(row=2, column=0, sticky="w")
        ttk.Combobox(panel, textvariable=action, values=("passthrough", "compact-stereo"), state="readonly").grid(row=3, column=0, sticky="ew", pady=4)
        def save():
            try:
                rule = parse_audio_language_overrides([f"{language.get().strip()}={action.get()}"])
            except Exception as exc:
                messagebox.showerror("Audio rule", str(exc), parent=dialog)
                return
            self._language_overrides.update(rule)
            self._refresh_language_tree()
            self._settings_changed()
            dialog.destroy()
        self._button(panel, text="Add rule", command=save, tooltip="Validate and add this language rule.").grid(row=4, column=0, sticky="e", pady=(10, 0))
        self._button(panel, text="Cancel", command=dialog.destroy, tooltip="Close without changing audio rules.").grid(row=4, column=0, sticky="w", pady=(10, 0))
        dialog.bind("<Escape>", lambda _event: dialog.destroy())
        selector.focus_set()
        dialog.grab_set()

    def _remove_language_rule(self) -> None:
        for item in self.language_tree.selection():
            language = str(self.language_tree.item(item, "values")[0])
            self._language_overrides.pop(language, None)
        self._refresh_language_tree()
        self._settings_changed()

    def _refresh_language_tree(self) -> None:
        self.language_tree.delete(*self.language_tree.get_children())
        for language, action in sorted(self._language_overrides.items()):
            self.language_tree.insert("", "end", values=(language, action))

    def _refresh_jobs(self) -> None:
        if self._jobs_after_id is not None:
            try:
                self.after_cancel(self._jobs_after_id)
            except tk.TclError:
                pass
            self._jobs_after_id = None
        selected = self._selected_job_id()
        self._job_rows.clear()
        for path in known_job_files():
            job = try_read_job(path)
            if not job:
                continue
            job = refresh_job(path, job)
            status = pipeline_status(job)
            percent = pipeline_percent(job, status)
            job_id = str(job.get("id"))
            settings = job.get("settings") or {}
            self._job_rows[job_id] = (path, job)
            job_state = str(job.get("status", "unknown"))
            update_row(self.jobs_tree, job_id,
                values=(
                    job_state, f"{percent:.1f}%", Path(str(job.get("source"))).stem,
                    settings.get("quality", ""), settings.get("audio_mode", "passthrough"), job.get("output", ""),
                ),
                tags=(job_state,),
            )
        prune_rows(self.jobs_tree, self._job_rows)
        states = [str(job.get("status") or "") for _path, job in self._job_rows.values()]
        running_count = states.count("running")
        waiting_count = sum(state == "queued" for state in states)
        watched_waiting_count = 0
        for watch_path in watched_batch_files():
            watched = try_read_watched_batch(watch_path)
            if watched and watched.get("status") == "active":
                watched_waiting_count += watched_batch_summary(watched)["pending"]
        paused = queue_is_paused()
        pause_reason = queue_pause_reason()
        pause_text = (
            f"paused: {pause_reason}"
            if paused and pause_reason and pause_reason != "pause-all"
            else ("paused" if paused else "active")
        )
        self.queue_state.configure(
            text=(
                f"Queue: {pause_text} · "
                f"{running_count} running · {waiting_count} queued · {states.count('planned')} planned only · "
                f"{watched_waiting_count} watched waiting"
            )
        )
        self.pause_button.configure(text="Resume queue" if paused else "Pause after current")
        self._show_job_detail()
        self._refresh_watches()
        self._jobs_after_id = self.after(1500, self._refresh_jobs)

    def _selected_job_id(self) -> str | None:
        selected = self.jobs_tree.selection() if hasattr(self, "jobs_tree") else ()
        return str(selected[0]) if selected else None

    def _show_job_detail(self) -> None:
        identifier = self._selected_job_id()
        if not identifier or identifier not in self._job_rows:
            self._clear_job_detail()
            return
        _path, job = self._job_rows[identifier]
        status = pipeline_status(job)
        percent = pipeline_percent(job, status)
        self.overall_progress["value"] = percent
        self.overall_label.configure(text=f"Overall {percent:.1f}% · {job.get('status')} · {status.get('stage', 'waiting')}")
        lanes = lane_progress_snapshot(job, status)
        for lane in ("video", "audio", "mux"):
            value, detail = lanes[lane]
            self.lane_bars[lane]["value"] = value
            self.lane_labels[lane].configure(text=f"{lane.title()}: {detail}")
        settings = job.get("settings") or {}
        detail = (
            f"Job: {identifier}\nSource: {job.get('source')}\nOutput: {job.get('output')}\n"
            f"Video: {settings.get('quality')} · {settings.get('bitrate_mode', 'vbr').upper()} · "
            f"multiplier {settings.get('target_bitrate_multiplier', settings.get('auto_cq_multiplier', 1.0))}× · "
            f"{settings.get('encoder', 'hevc_nvenc')} {settings.get('encoder_preset', 'p6')}\n"
            f"Audio: {settings.get('audio_mode', 'passthrough')} · rules {settings.get('audio_language_overrides') or 'none'}\n"
            f"Log: {job.get('log')}"
        )
        error = job_error(job, job.get("status"))
        planned = "Planned only: use Resume selected to queue this job.\n" if job.get("status") == "planned" else ""
        set_detail(self.job_detail, (f"Error: {error}\n" if error else "") + planned + detail)
        self._update_job_actions(job, str(job.get("status")))

    def _toggle_queue(self) -> None:
        self._set_queue_paused(not queue_is_paused())

    def _set_queue_paused(self, paused: bool) -> None:
        if paused:
            cmd_pause_queue(argparse.Namespace())
        else:
            cmd_resume_queue(argparse.Namespace())
        self._refresh_jobs()

    def _cancel_selected(self) -> None:
        identifier = self._selected_job_id()
        if identifier and messagebox.askyesno("Cancel job", f"Cancel {identifier}? Resumable work will be retained.", parent=self):
            self._background("Requesting cancellation…", lambda: (cmd_cancel(argparse.Namespace(job=identifier)), "Cancellation requested")[1])

    def _resume_selected(self) -> None:
        if identifier := self._selected_job_id():
            self._background("Resuming job…", lambda: (cmd_resume_job(argparse.Namespace(job=identifier)), "Job queued to resume")[1])

    def _cancel_all(self) -> None:
        if not messagebox.askyesno(
            "Cancel everything?",
            "This stops all watched batches, cancels every waiting conversion, and immediately terminates the active conversion and its helper processes.\n\n"
            "The queue remains paused. Completed outputs and resumable work are retained.",
            parent=self,
        ):
            return
        self._background(
            "Canceling all DVD2HEVC work...",
            lambda: (cmd_cancel_all(argparse.Namespace()), "All conversion work canceled; queue paused")[1],
            show_result=True,
        )

    def _play_selected(self) -> None:
        if identifier := self._selected_job_id():
            self._background("Opening output in VLC…", lambda: (cmd_play(argparse.Namespace(target=identifier, vlc_root=None)), "Opened output")[1])

    def _open_selected_job_folder(self) -> None:
        identifier = self._selected_job_id()
        if identifier and identifier in self._job_rows:
            self._open_path(Path(self._job_rows[identifier][1]["work_root"]))



    def _refresh_presets(self) -> None:
        presets = all_presets()
        if "Preset" in self._fields:
            self._fields["Preset"].configure(values=sorted(presets))
        prune_rows(self.preset_tree, presets)
        for name, value in sorted(presets.items()):
            update_row(self.preset_tree, name, values=(name, value.get("quality"), value.get("encoder", "hevc_nvenc"), value.get("audio_mode", "passthrough"), "Built-in" if value.get("builtin") else "Saved"))

    def _load_selected_preset(self) -> None:
        selected = self.preset_tree.selection()
        if selected:
            self._apply_preset(str(selected[0]))
            self.notebook.select(self.convert_tab)

    def _save_current_preset(self) -> None:
        name = simpledialog.askstring("Save preset", "Preset name:", parent=self)
        if not name:
            return
        name = name.strip()
        existing = all_presets().get(name)
        if existing and not existing.get("builtin") and not messagebox.askyesno("Replace preset", f"Replace {name}?", parent=self):
            return
        mode = self.override_mode_var.get()
        try:
            resolve_conversion_settings(self._settings_namespace(Path("preset-check.iso")))
            save_named_preset(
                name,
                quality=self.quality_var.get(), encoder=_encoder_from_label(self.encoder_var.get()),
                encoder_preset=self.encoder_preset_var.get(), deinterlace=self.deinterlace_var.get(),
                audio_mode=self.audio_mode_var.get(), stereo_audio_bitrate=self.stereo_rate_var.get(),
                mono_audio_bitrate=self.mono_rate_var.get(), audio_workers=int(self.audio_workers_var.get()),
                pipeline_depth=int(self.pipeline_depth_var.get()),
                target_bitrate_multiplier=float(self.multiplier_var.get()),
                bitrate_mode=self.bitrate_mode_var.get(),
                main_title_quality=self.override_quality_var.get() if mode == "Main title" else None,
                top_n_quality=self.override_quality_var.get() if mode == "Top N titles" else None,
                top_n_count=int(self.top_n_var.get()) if mode == "Top N titles" else 0,
                audio_language_overrides=self._language_overrides,
                add_filename_tags=bool(self.filename_tags_var.get()),
                output_format=self._dvd_output(),
            )
        except Exception as exc:
            messagebox.showerror("Save preset", str(exc), parent=self)
            return
        self.preset_var.set(name)
        self._refresh_presets()

    def _remove_selected_preset(self) -> None:
        selected = self.preset_tree.selection()
        if not selected:
            return
        name = str(selected[0])
        if not all_presets().get(name, {}).get("builtin") and not messagebox.askyesno("Remove preset", f"Delete {name}?", parent=self):
            return
        try:
            remove_named_preset(name)
        except Exception as exc:
            messagebox.showerror("Remove preset", str(exc), parent=self)
            return
        if self.preset_var.get() == name:
            self.preset_var.set("Custom")
        self._refresh_presets()

    def _show_quick_help(self) -> None:
        messagebox.showinfo(
            "DVD2HEVC quick start",
            "1. On Convert, choose a decrypted DVD ISO and check the output name.\n\n"
            "2. Leave Disc output at UHD-BD (stock VLC). Start with balanced quality, a working encoder and Auto deinterlace. "
            "Choose compact-stereo when you want DVD AC-3, DTS, LPCM, or MPEG audio converted to compact stereo AC-3.\n\n"
            "3. Use Plan only for a read-only compatibility check, or Add to queue to convert in the background.\n\n"
            "On Batch queue, choose Queue once for a fixed set or Start watched batch to keep adding new completed ISOs from that folder later.\n\n"
            "4. Watch simultaneous video, audio, and mux/staging work under Jobs & progress. "
            "UHD-BD outputs open in stock VLC with Java; patching VLC is unnecessary.\n\n"
            "Hover over any control for a more detailed explanation. Your source ISO and normal VLC installation are never modified.",
            parent=self,
        )

    def _show_about(self) -> None:
        messagebox.showinfo(
            "About DVD2HEVC",
            "DVD2HEVC creates compact, menu-preserving HEVC backups from decrypted DVD ISOs.\n\n"
            "The default UHD-BD output runs the original DVD navigation in BD-J and plays in stock VLC with Java. "
            "Patching VLC is not necessary. The optional legacy HEVC DVD output uses a private patched player.\n\n"
            "Project status: alpha / open-source preparation.",
            parent=self,
        )

    @staticmethod
    def _open_vlc_guide() -> None:
        guide = ROOT / "docs" / "VLC_BUILD.md"
        if guide.is_file():
            os.startfile(str(guide))

    def _refresh_vlc_status(self) -> None:
        if self._dvd_output() == 'uhd-bd':
            from .uhd import discover_uhd_tools
            tools = discover_uhd_tools()
            ready = bool(tools['stock_vlc'] and tools['java'])
            self.vlc_status_label.configure(text="UHD-BD playback: " + ("Ready ✓" if ready else "Install stock VLC with BD-J and Java 11") +
                                             "\nNo VLC patch is required.", foreground=ACCENT_DARK if ready else WARN)
            return
        ready_root = find_ready_vlc_root()
        if ready_root is not None:
            status = inspect_private_vlc(ready_root)
            self.vlc_status_label.configure(
                text=(
                    "HEVC DVD playback: Ready ✓\n"
                    f"Private VLC 3.0.23 verified by {status['verified_by']}."
                ),
                foreground=ACCENT_DARK,
            )
            return
        managed = inspect_private_vlc(managed_vlc_root())
        detail = managed["reasons"][0] if managed["reasons"] else "No verified private player was found."
        self.vlc_status_label.configure(
            text=f"HEVC DVD playback: Not ready\n{detail}",
            foreground=WARN,
        )

    def _setup_vlc(self) -> None:
        if self._vlc_setup_in_progress:
            messagebox.showinfo("HEVC VLC setup", "Setup is already running.", parent=self)
            return
        ready_root = find_ready_vlc_root()
        if ready_root is not None:
            status = inspect_private_vlc(ready_root)
            self._refresh_vlc_status()
            messagebox.showinfo(
                "HEVC VLC is already ready",
                "A compatible private player has already been verified. No files were changed.\n\n"
                f"Location: {ready_root}\n"
                f"Verification: {status['verified_by']}\n\n"
                "It is safe to press this button again whenever you want to recheck it.",
                parent=self,
            )
            return

        base = discover_vlc_base()
        if base is None:
            messagebox.showinfo(
                "Choose unmodified VLC 3.0.23",
                "DVD2HEVC could not find the supported normal VLC runtime. Choose the folder containing VLC 3.0.23's vlc.exe. "
                "It will only be copied; this installation will not be changed.",
                parent=self,
            )
            chosen = filedialog.askdirectory(title="Choose unmodified VLC 3.0.23 folder", parent=self)
            if not chosen:
                return
            base = Path(chosen)

        source = discover_vlc_source()
        if source is None or not validate_vlc_source(source)["ready"]:
            reason = ""
            if source is not None:
                reason = "\n\nThe automatically found checkout was not usable:\n" + "\n".join(validate_vlc_source(source)["reasons"])
            messagebox.showinfo(
                "Choose prepared VLC source",
                "Choose the pinned VLC 3.0.23 source checkout prepared through VLC's Windows/MSYS2 build workflow. "
                "See Help → HEVC VLC setup guide for the exact commit and requirements." + reason,
                parent=self,
            )
            chosen = filedialog.askdirectory(title="Choose prepared VLC 3.0.23 source checkout", parent=self)
            if not chosen:
                return
            source = Path(chosen)

        destination = managed_vlc_root()
        if not messagebox.askyesno(
            "Prepare private HEVC VLC?",
            "DVD2HEVC will build and verify its DVD navigation plugin inside a separate private copy.\n\n"
            f"Unmodified runtime (read only):\n{base}\n\n"
            f"Prepared source checkout:\n{source}\n\n"
            f"Private destination:\n{destination}\n\n"
            "Your normal VLC installation will not be changed. A failed setup is staged separately and will not replace a working private player. Continue?",
            parent=self,
        ):
            return

        self._vlc_setup_in_progress = True
        self.vlc_setup_button.configure(state="disabled", text="Preparing HEVC VLC…")

        def work() -> str:
            try:
                result = prepare_private_vlc(source, base, destination)
                return str(result["message"])
            finally:
                self._post(self._vlc_setup_finished)

        self._background("Preparing private HEVC VLC…", work, show_result=True)

    def _vlc_setup_finished(self) -> None:
        self._vlc_setup_in_progress = False
        self.vlc_setup_button.configure(state="normal", text="Verify / set up HEVC VLC…")
        self._refresh_vlc_status()

    def _run_diagnostics(self) -> None:
        def work() -> str:
            completed = subprocess.run(
                [sys.executable, str(ROOT / "dvd2hevc.py"), "tools"],
                cwd=ROOT, text=True, capture_output=True, check=False,
                **hidden_subprocess_kwargs(),
            )
            return (completed.stdout + completed.stderr).strip() or "Dependency check completed."
        self._background("Checking dependencies…", work, show_result=True)






def launch_gui() -> int:
    if sys.platform == "win32":
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
        except (AttributeError, OSError):
            pass
    app = DVD2HEVCApp()
    app.mainloop()
    return 0
