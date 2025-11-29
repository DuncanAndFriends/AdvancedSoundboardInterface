import os
import sys
import json
import threading
import random
import time
import subprocess
import tkinter as tk
from tkinter import filedialog, messagebox, ttk, PhotoImage
from tkinter import font as tkfont

import numpy as np
import pygame
import pygame.sndarray as sndarray
import speech_recognition as sr

# Optional recording deps
try:
    import sounddevice as sd
    import soundfile as sf
    RECORDING_AVAILABLE = True
except ImportError:
    sd = None
    sf = None
    RECORDING_AVAILABLE = False

# ---------- PORTABLE BASE DIR ----------
def resource_path(rel: str) -> str:
    """
    Get a path to a bundled resource (duck_logo.png, etc.).
    When frozen by PyInstaller, use the temporary _MEIPASS folder.
    Otherwise, use the normal script directory.
    """
    if hasattr(sys, "_MEIPASS"):
        base_path = sys._MEIPASS
    else:
        if getattr(sys, "frozen", False):
            base_path = os.path.dirname(sys.executable)
        else:
            base_path = os.path.abspath(os.path.dirname(__file__))
    return os.path.join(base_path, rel)


# Use the real folder of the .exe when frozen, otherwise the script folder.
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.abspath(os.path.dirname(__file__))

CONFIG_FILE = os.path.join(BASE_DIR, "soundboard_config.json")

# ---------- COLORS / FONTS ----------
BG = "#4b4b4b"          # main background
SECTION_BG = "#5a5a5a"  # group boxes
BTN_BG = "#7a7a7a"      # sound buttons
BTN_FG = "white"
HEADER_FONT = ("Segoe UI", 18, "bold")   # thinner style than Arial bold

# ---------- AUDIO GLOBALS ----------
pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=512)
audio_lock = threading.Lock()
current_channel = pygame.mixer.Channel(0)
last_played = None
volume_factor = 1.0  # multiplier (1.0 = normal)


# ---------- UTILS ----------
def sanitize_filename_spaces(text: str) -> str:
    """Sanitize for filenames but allow spaces."""
    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 -_"
    cleaned = "".join(c for c in text if c in allowed).strip()
    return cleaned or "recording"


def next_unique_path(path: str) -> str:
    """If file exists, add (2), (3)... until unique."""
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    i = 2
    while True:
        candidate = f"{base} ({i}){ext}"
        if not os.path.exists(candidate):
            return candidate
        i += 1


def pitch_shift_array(arr: np.ndarray, semitones: float) -> np.ndarray:
    """Simple resampling pitch shift."""
    if semitones == 0:
        return arr
    factor = 2.0 ** (semitones / 12.0)
    length = arr.shape[0]
    new_length = max(1, int(length / factor))
    old_idx = np.arange(length)
    new_idx = np.linspace(0, length - 1, new_length)

    if arr.ndim == 1:
        return np.interp(new_idx, old_idx, arr).astype(arr.dtype)

    new_arr = np.zeros((new_length, arr.shape[1]), dtype=arr.dtype)
    for c in range(arr.shape[1]):
        new_arr[:, c] = np.interp(new_idx, old_idx, arr[:, c]).astype(arr.dtype)
    return new_arr


def play_audio_file(path: str, semitones: float = 0.0):
    """Play an audio file with pitch + volume."""
    global last_played, volume_factor
    path = os.path.normpath(path)
    if not os.path.exists(path):
        print("Missing:", path)
        return

    with audio_lock:
        if current_channel.get_busy():
            current_channel.stop()

        try:
            snd = pygame.mixer.Sound(path)
            arr = pygame.sndarray.array(snd)

            if semitones != 0:
                arr = pitch_shift_array(arr, semitones)

            if volume_factor != 1.0:
                arr_f = arr.astype(np.float32) * float(volume_factor)
                info = np.iinfo(arr.dtype)
                arr_f = np.clip(arr_f, info.min, info.max)
                arr = arr_f.astype(arr.dtype)

            snd2 = pygame.sndarray.make_sound(arr)
            current_channel.play(snd2)
            last_played = path
        except Exception as e:
            print("Playback error:", e)


# ---------- AUTO RENAMER (Recordings Folder) ----------
class AutoRenamer(threading.Thread):
    """
    Watches the recordings folder for new .wav/.mp3 files,
    transcribes them, and renames the file based on the text,
    using spaces.
    """
    def __init__(self, folder: str):
        super().__init__(daemon=True)
        self.folder = folder
        self.running = True
        self.seen = set()
        self.recognizer = sr.Recognizer()

    def set_folder(self, folder: str):
        self.folder = folder
        self.seen.clear()

    def stop(self):
        self.running = False

    def run(self):
        while self.running:
            try:
                if not self.folder or not os.path.isdir(self.folder):
                    time.sleep(2)
                    continue

                for name in os.listdir(self.folder):
                    full = os.path.join(self.folder, name)
                    if not os.path.isfile(full):
                        continue
                    if full in self.seen:
                        continue
                    ext = os.path.splitext(name)[1].lower()
                    if ext not in (".wav", ".mp3"):
                        self.seen.add(full)
                        continue

                    self.handle_file(full)
                    self.seen.add(full)

            except Exception as e:
                print("AutoRenamer error:", e)

            time.sleep(2)

    def handle_file(self, path: str):
        try:
            # transcribe
            with sr.AudioFile(path) as src:
                audio = self.recognizer.record(src)
            try:
                text = self.recognizer.recognize_google(audio)
            except Exception:
                text = "recording"

            cleaned = sanitize_filename_spaces(text)
            new_path = os.path.join(self.folder, cleaned + ".mp3")
            new_path = next_unique_path(new_path)
            base, ext = os.path.splitext(path)

            # If not mp3, just rename + change extension (simple; original data stays same)
            if ext.lower() != ".mp3":
                try:
                    os.rename(path, new_path)
                except Exception as e:
                    print("Rename error:", e)
            else:
                try:
                    os.rename(path, new_path)
                except Exception as e:
                    print("Rename error:", e)
        except Exception as e:
            print("handle_file error:", e)


# ---------- VOICE BOT ----------
class VoiceBot:
    def __init__(self, pitch_provider, audio_sources_provider):
        self.pitch_provider = pitch_provider
        self.get_audio_sources = audio_sources_provider
        self.recognizer = sr.Recognizer()
        self.mic = None
        self.stop_listening = None
        self.active = False

    def enable(self):
        if self.active:
            return
        try:
            self.mic = sr.Microphone()
            with self.mic as s:
                self.recognizer.adjust_for_ambient_noise(s, duration=0.5)
            self.stop_listening = self.recognizer.listen_in_background(
                self.mic, self._callback, phrase_time_limit=4
            )
            self.active = True
            print("Voice bot enabled.")
        except Exception as e:
            print("Bot enable error:", e)

    def disable(self):
        if not self.active:
            return
        try:
            if self.stop_listening:
                self.stop_listening(wait_for_stop=False)
        except Exception:
            pass
        self.active = False
        print("Voice bot disabled.")

    def _callback(self, recognizer, audio):
        global last_played
        try:
            text = recognizer.recognize_google(audio).lower()
        except Exception:
            return

        pitch = self.pitch_provider()

        if "what" in text and last_played:
            play_audio_file(last_played, pitch)
            return

        files = self.get_all_files_safe()
        if not files:
            return

        choice = random.choice(files)
        play_audio_file(choice, pitch)

    def get_all_files_safe(self):
        try:
            return self.get_audio_sources()
        except Exception as e:
            print("get_audio_sources error:", e)
            return []


# ---------- CONFIG ----------
def load_config():
    data = {
        "categories": {},             # "Category 1" -> folder path
        "all_sounds_folder": "",      # All Sounds (recursive)
        "recordings_folder": os.path.join(BASE_DIR, "recordings"),
        "selected_theme": "Dark"
    }
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                loaded = json.load(f)
            data.update(loaded)
        except Exception as e:
            print("Config load error:", e)

    # normalize 20 category slots
    cats = data.get("categories") or {}
    new_cats = {}
    i = 1
    for name, folder in cats.items():
        if folder:
            new_cats[f"Category {i}"] = folder
            i += 1
            if i > 20:
                break
    while i <= 20:
        new_cats[f"Category {i}"] = ""
        i += 1
    data["categories"] = new_cats

    # ensure recordings folder
    if not data.get("recordings_folder"):
        data["recordings_folder"] = os.path.join(BASE_DIR, "recordings")

    save_config(data)
    return data


def save_config(data):
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump(data, f, indent=4)
    except Exception as e:
        print("Config save error:", e)


# ---------- MAIN APP ----------
class SoundboardApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Advanced Soundboard Interface")
        self.root.configure(bg=BG)
        self.root.geometry("1100x700")

        # load logo
        self.logo_img = self.load_duck_logo()

        if self.logo_img:
            self.root.iconphoto(False, self.logo_img)

        # config
        self.config = load_config()
        self.categories = self.config["categories"]
        self.all_sounds_folder = self.config.get("all_sounds_folder", "")
        self.recordings_folder = self.config.get("recordings_folder", os.path.join(BASE_DIR, "recordings"))

        # state
        self.button_size_var = tk.IntVar(value=10)   # smaller height
        self.pitch_var = tk.IntVar(value=50)         # 0..100, 50 center
        self.volume_var = tk.IntVar(value=100)       # 0..200, 100 normal
        self.bot_enabled = tk.BooleanVar(value=False)

        self.is_recording = False
        self.record_stream = None
        self.record_frames = []

        # voice bot
        self.bot = VoiceBot(self.get_current_pitch_semitones, self.get_all_audio_files)

        # auto-renamer for recordings
        self.renamer = AutoRenamer(self.recordings_folder)
        self.renamer.start()

        # context menu for recordings
        self.rec_menu = tk.Menu(self.root, tearoff=0)
        self.rec_menu.add_command(label="Rename...", command=self._rec_rename)
        self.rec_menu.add_command(label="Delete...", command=self._rec_delete)
        self.rec_menu.add_command(label="Open Folder", command=self._rec_open_folder)
        self._rec_menu_target = None

        self.build_ui()
        self.refresh_soundboard()
        self.on_volume_change()

    # ---------- IMAGE / LABEL HELPERS ----------
    def load_duck_logo(self):
        logo_path = os.path.join(BASE_DIR, "duck_logo.png")
        if not os.path.exists(logo_path):
            print("Logo not found:", logo_path)
            return None

        try:
            img = PhotoImage(file=logo_path)
            title_font = tkfont.Font(font=HEADER_FONT)
            target_height = max(1, title_font.metrics("linespace"))
            h = img.height()
            if h > target_height:
                factor = max(1, int(round(h / float(target_height))))
                img = img.subsample(factor, factor)
            return img
        except Exception as e:
            print("Logo load error:", e)
            return None

    def shorten_label(self, text: str, max_chars: int = 24) -> str:
        """Keep labels on a single line and avoid wasting space."""
        text = text.strip()
        if len(text) <= max_chars:
            return text
        return text[: max_chars - 1] + "…"

    # ---------- UI ----------
    def build_ui(self):
        # Header
        header = tk.Frame(self.root, bg=BG)
        header.pack(fill="x", pady=10, padx=10)

        if self.logo_img:
            tk.Label(header, image=self.logo_img, bg=BG).pack(side="left", padx=(0, 10))

        tk.Label(
            header,
            text="Advanced Soundboard Interface",
            font=HEADER_FONT,
            fg="white",
            bg=BG
        ).pack(side="left")

        # Top controls
        top = tk.Frame(self.root, bg=BG)
        top.pack(fill="x", padx=10)

        # Presets (file based)
        tk.Button(
            top,
            text="Save Preset...",
            command=self.save_preset_file,
            bg=BTN_BG,
            fg=BTN_FG
        ).pack(side="left", padx=5)

        tk.Button(
            top,
            text="Load Preset...",
            command=self.load_preset_file,
            bg=BTN_BG,
            fg=BTN_FG
        ).pack(side="left", padx=5)

        # Bot toggle
        tk.Checkbutton(
            top,
            text="Enable Voice Bot",
            variable=self.bot_enabled,
            command=self.on_bot_toggle,
            bg=BG,
            fg="white",
            selectcolor=BG,
            activebackground=BG,
            activeforeground="white"
        ).pack(side="left", padx=10)

        # Pitch / Volume / Button size
        tk.Scale(
            top,
            from_=0,
            to=100,
            orient="horizontal",
            label="Pitch",
            variable=self.pitch_var,
            bg=BG,
            fg="white",
            troughcolor="#3c3c3c",
            highlightthickness=0,
        ).pack(side="left", padx=10)

        tk.Scale(
            top,
            from_=0,
            to=200,
            orient="horizontal",
            label="Volume",
            variable=self.volume_var,
            command=lambda v: self.on_volume_change(),
            bg=BG,
            fg="white",
            troughcolor="#3c3c3c",
            highlightthickness=0,
        ).pack(side="left", padx=10)

        tk.Scale(
            top,
            from_=8,
            to=18,
            orient="horizontal",
            label="Button Size",
            variable=self.button_size_var,
            command=lambda v: self.refresh_soundboard(),
            bg=BG,
            fg="white",
            troughcolor="#3c3c3c",
            highlightthickness=0,
        ).pack(side="left", padx=10)

        # Record + recordings folder
        self.record_btn = tk.Button(
            top,
            text="● Record",
            command=self.toggle_recording,
            bg="#aa3333",
            fg="white"
        )
        self.record_btn.pack(side="left", padx=10)

        tk.Button(
            top,
            text="Recordings Folder...",
            command=self.change_recordings_folder,
            bg=BTN_BG,
            fg=BTN_FG
        ).pack(side="left", padx=5)

        # Clear Soundboard (far right)
        tk.Button(
            top,
            text="Clear Soundboard",
            command=self.clear_soundboard,
            bg="#aa4444",
            fg="white"
        ).pack(side="right", padx=5)

        # Scrollable area
        outer = tk.Frame(self.root, bg=BG)
        outer.pack(fill="both", expand=True, padx=10, pady=10)

        self.canvas = tk.Canvas(outer, bg=BG, highlightthickness=0)
        self.scrollbar = tk.Scrollbar(outer, orient="vertical", command=self.canvas.yview)
        self.inner = tk.Frame(self.canvas, bg=BG)

        self.inner.bind(
            "<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        )
        self.canvas_frame = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")

        self.canvas.configure(yscrollcommand=self.scrollbar.set)

        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")

        def _resize(event):
            self.canvas.itemconfig(self.canvas_frame, width=event.width)
        self.canvas.bind("<Configure>", _resize)

        self.canvas.bind_all("<MouseWheel>", self._on_mousewheel)

    def _on_mousewheel(self, event):
        self.canvas.yview_scroll(-1 * int(event.delta / 120), "units")

    # ---------- SOUND BOARD BUILD ----------
    def refresh_soundboard(self):
        # clear
        for w in self.inner.winfo_children():
            w.destroy()

        # Recordings section
        self.add_recordings_section()

        # Category sections 1..20
        for i in range(1, 21):
            name = f"Category {i}"
            folder = self.categories.get(name, "")
            self.add_category_section(name, folder, recursive=False)

        # All Sounds (old RANDOM)
        folder = self.all_sounds_folder
        self.add_category_section("All Sounds", folder, recursive=True, show_description=True)

    def add_recordings_section(self):
        section = tk.LabelFrame(
            self.inner,
            text=f"Recordings → {self.recordings_folder}",
            bg=SECTION_BG,
            fg="white"
        )
        section.pack(fill="x", pady=5, padx=5, anchor="n")

        top_bar = tk.Frame(section, bg=SECTION_BG)
        top_bar.pack(fill="x", padx=5, pady=2)

        tk.Button(
            top_bar,
            text="Choose Folder",
            command=self.change_recordings_folder,
            bg=BTN_BG,
            fg=BTN_FG
        ).pack(side="left")

        # files
        if not os.path.isdir(self.recordings_folder):
            try:
                os.makedirs(self.recordings_folder, exist_ok=True)
            except OSError as e:
                print("Error creating recordings folder, falling back:", e)
                self.recordings_folder = os.path.join(BASE_DIR, "recordings")
                self.config["recordings_folder"] = self.recordings_folder
                save_config(self.config)
                os.makedirs(self.recordings_folder, exist_ok=True)

        files = [
            f for f in os.listdir(self.recordings_folder)
            if f.lower().endswith((".mp3", ".wav"))
        ]

        if not files:
            tk.Label(
                section,
                text="(No recordings yet)",
                bg=SECTION_BG,
                fg="white"
            ).pack(anchor="w", padx=10, pady=5)
            return

        grid = tk.Frame(section, bg=SECTION_BG)
        grid.pack(fill="x", padx=5, pady=5)

        max_cols = 6
        row = col = 0
        btn_font = ("Segoe UI", self.button_size_var.get(), "bold")

        for fname in sorted(files):
            full = os.path.join(self.recordings_folder, fname)
            label = os.path.splitext(fname)[0]
            short = self.shorten_label(label)

            b = tk.Button(
                grid,
                text=short,
                command=lambda p=full: self.play_button_clicked(p),
                bg=BTN_BG,
                fg=BTN_FG,
                relief="raised",
                bd=1,
                font=btn_font,
                height=1,
                width=18
            )
            b.grid(row=row, column=col, padx=3, pady=3, sticky="ew")

            # right-click menu
            b.bind("<Button-3>", lambda e, p=full: self.show_rec_menu(e, p))

            grid.columnconfigure(col, weight=1)
            col += 1
            if col >= max_cols:
                col = 0
                row += 1

    def add_category_section(self, name, folder, recursive=False, show_description=False):
        section = tk.LabelFrame(
            self.inner,
            text=f"{name} → {folder or '(no folder)'}",
            bg=SECTION_BG,
            fg="white"
        )
        section.pack(fill="x", pady=5, padx=5, anchor="n")

        top_bar = tk.Frame(section, bg=SECTION_BG)
        top_bar.pack(fill="x", padx=5, pady=2)

        tk.Button(
            top_bar,
            text="Choose Folder",
            command=lambda n=name: self.change_category_folder(n),
            bg=BTN_BG,
            fg=BTN_FG
        ).pack(side="left")

        if show_description:
            tk.Label(
                section,
                text="This category automatically includes all audio inside this folder and any of its subfolders.",
                bg=SECTION_BG,
                fg="white",
                wraplength=800,
                justify="left"
            ).pack(anchor="w", padx=10, pady=(0, 5))

        if not folder or not os.path.isdir(folder):
            tk.Label(
                section,
                text="(No folder selected)",
                bg=SECTION_BG,
                fg="white"
            ).pack(anchor="w", padx=10, pady=5)
            return

        # collect files
        files = []
        if recursive:
            for root, dirs, fns in os.walk(folder):
                for fn in fns:
                    if fn.lower().endswith((".mp3", ".wav")):
                        full = os.path.join(root, fn)
                        label = os.path.splitext(fn)[0]
                        files.append((label, full))
        else:
            for fn in os.listdir(folder):
                if fn.lower().endswith((".mp3", ".wav")):
                    full = os.path.join(folder, fn)
                    label = os.path.splitext(fn)[0]
                    files.append((label, full))

        if not files:
            tk.Label(
                section,
                text="(No audio files found)",
                bg=SECTION_BG,
                fg="white"
            ).pack(anchor="w", padx=10, pady=5)
            return

        grid = tk.Frame(section, bg=SECTION_BG)
        grid.pack(fill="x", padx=5, pady=5)

        max_cols = 6
        row = col = 0
        btn_font = ("Segoe UI", self.button_size_var.get(), "bold")

        for label, full in sorted(files, key=lambda x: x[0].lower()):
            short = self.shorten_label(label)
            b = tk.Button(
                grid,
                text=short,
                command=lambda p=full: self.play_button_clicked(p),
                bg=BTN_BG,
                fg=BTN_FG,
                relief="raised",
                bd=1,
                font=btn_font,
                height=1,
                width=18
            )
            b.grid(row=row, column=col, padx=3, pady=3, sticky="ew")
            grid.columnconfigure(col, weight=1)

            col += 1
            if col >= max_cols:
                col = 0
                row += 1

    # ---------- CATEGORY / FOLDER CHANGES ----------
    def change_category_folder(self, name: str):
        new_folder = filedialog.askdirectory(title=f"Select folder for {name}")
        if not new_folder:
            return

        if name == "All Sounds":
            self.all_sounds_folder = new_folder
            self.config["all_sounds_folder"] = new_folder
        else:
            self.categories[name] = new_folder
            self.config["categories"] = self.categories

        save_config(self.config)
        self.refresh_soundboard()

    def change_recordings_folder(self):
        new_folder = filedialog.askdirectory(title="Select Recordings Folder")
        if not new_folder:
            return
        self.recordings_folder = new_folder
        self.config["recordings_folder"] = new_folder
        save_config(self.config)
        self.renamer.set_folder(new_folder)
        self.refresh_soundboard()

    def clear_soundboard(self):
        if not messagebox.askyesno(
            "Clear Soundboard",
            "Are you sure you want to clear all category folders (but keep the recordings folder)?"
        ):
            return

        for i in range(1, 21):
            self.categories[f"Category {i}"] = ""
        self.all_sounds_folder = ""
        self.config["categories"] = self.categories
        self.config["all_sounds_folder"] = self.all_sounds_folder
        save_config(self.config)
        self.refresh_soundboard()

    # ---------- PRESETS (FILE-BASED) ----------
    def save_preset_file(self):
        path = filedialog.asksaveasfilename(
            defaultextension=".json",
            filetypes=[("JSON Preset", "*.json")],
            title="Save Preset As"
        )
        if not path:
            return

        data = {
            "categories": self.categories,
            "all_sounds_folder": self.all_sounds_folder,
            "recordings_folder": self.recordings_folder,
            "pitch": self.pitch_var.get(),
            "volume": self.volume_var.get(),
            "button_size": self.button_size_var.get()
        }
        try:
            with open(path, "w") as f:
                json.dump(data, f, indent=4)
        except Exception as e:
            print("Save preset error:", e)

    def load_preset_file(self):
        path = filedialog.askopenfilename(
            defaultextension=".json",
            filetypes=[("JSON Preset", "*.json")],
            title="Load Preset"
        )
        if not path:
            return
        try:
            with open(path, "r") as f:
                data = json.load(f)
        except Exception as e:
            print("Load preset error:", e)
            return

        self.categories = data.get("categories", self.categories)
        self.all_sounds_folder = data.get("all_sounds_folder", self.all_sounds_folder)
        self.recordings_folder = data.get("recordings_folder", self.recordings_folder)
        self.pitch_var.set(data.get("pitch", 50))
        self.volume_var.set(data.get("volume", 100))
        self.button_size_var.set(data.get("button_size", 10))

        self.config["categories"] = self.categories
        self.config["all_sounds_folder"] = self.all_sounds_folder
        self.config["recordings_folder"] = self.recordings_folder
        save_config(self.config)
        self.renamer.set_folder(self.recordings_folder)
        self.refresh_soundboard()
        self.on_volume_change()

    # ---------- BOT & SLIDERS ----------
    def on_bot_toggle(self):
        if self.bot_enabled.get():
            self.bot.enable()
        else:
            self.bot.disable()

    def get_current_pitch_semitones(self) -> float:
        raw = self.pitch_var.get()  # 0..100, 50 center
        return (raw - 50) * (12.0 / 50.0)  # +/- 12 semitones

    def on_volume_change(self):
        global volume_factor
        volume_factor = self.volume_var.get() / 100.0
        print("Volume factor:", volume_factor)

    def play_button_clicked(self, path: str):
        play_audio_file(path, self.get_current_pitch_semitones())

    def get_all_audio_files(self):
        files = []

        # categories
        for folder in self.categories.values():
            if folder and os.path.isdir(folder):
                for fn in os.listdir(folder):
                    if fn.lower().endswith((".mp3", ".wav")):
                        files.append(os.path.join(folder, fn))

        # All Sounds
        if self.all_sounds_folder and os.path.isdir(self.all_sounds_folder):
            for root, dirs, fns in os.walk(self.all_sounds_folder):
                for fn in fns:
                    if fn.lower().endswith((".mp3", ".wav")):
                        files.append(os.path.join(root, fn))

        # recordings
        if self.recordings_folder and os.path.isdir(self.recordings_folder):
            for fn in os.listdir(self.recordings_folder):
                if fn.lower().endswith((".mp3", ".wav")):
                    files.append(os.path.join(self.recordings_folder, fn))

        return files

    # ---------- RECORDING ----------
    def toggle_recording(self):
        if not RECORDING_AVAILABLE:
            messagebox.showerror(
                "Recording Not Available",
                "sounddevice/soundfile are not installed.\nRecording is disabled."
            )
            return

        if self.is_recording:
            self.stop_recording()
        else:
            self.start_recording()

    def start_recording(self):
        if self.is_recording:
            return

        self.record_frames = []

        def callback(indata, frames, time_, status):
            if status:
                print("Recording status:", status)
            self.record_frames.append(indata.copy())

        try:
            self.record_stream = sd.InputStream(
                samplerate=44100,
                channels=1,
                callback=callback
            )
            self.record_stream.start()
            self.is_recording = True
            self.record_btn.configure(text="■ Stop", bg="#cc5555")
        except Exception as e:
            messagebox.showerror("Recording Error", str(e))

    def stop_recording(self):
        if not self.is_recording:
            return
        try:
            self.record_stream.stop()
            self.record_stream.close()
        except Exception as e:
            print("Stop recording error:", e)
        self.record_stream = None
        self.is_recording = False
        self.record_btn.configure(text="● Record", bg="#aa3333")

        if not self.record_frames:
            return

        # Save as wav with temp name; auto-renamer will handle naming
        os.makedirs(self.recordings_folder, exist_ok=True)
        data = np.concatenate(self.record_frames, axis=0)
        temp_name = time.strftime("rec_%Y%m%d_%H%M%S.wav")
        temp_path = os.path.join(self.recordings_folder, temp_name)
        try:
            sf.write(temp_path, data, 44100)
        except Exception as e:
            print("Write recording error:", e)
        self.refresh_soundboard()

    # ---------- RECORDINGS CONTEXT MENU ----------
    def show_rec_menu(self, event, path: str):
        self._rec_menu_target = path
        try:
            self.rec_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.rec_menu.grab_release()

    def _rec_rename(self):
        if not self._rec_menu_target:
            return
        base_dir = os.path.dirname(self._rec_menu_target)
        current = os.path.basename(self._rec_menu_target)
        name_no_ext, ext = os.path.splitext(current)

        new_name = tk.simpledialog.askstring("Rename Recording", "New name:", initialvalue=name_no_ext)
        if not new_name:
            return

        cleaned = sanitize_filename_spaces(new_name)
        new_path = os.path.join(base_dir, cleaned + ext)
        new_path = next_unique_path(new_path)
        try:
            os.rename(self._rec_menu_target, new_path)
        except Exception as e:
            print("Rename error:", e)
        self.refresh_soundboard()

    def _rec_delete(self):
        if not self._rec_menu_target:
            return
        if not messagebox.askyesno("Delete Recording", "Are you sure you want to delete this recording?"):
            return
        try:
            os.remove(self._rec_menu_target)
        except Exception as e:
            print("Delete error:", e)
        self.refresh_soundboard()

    def _rec_open_folder(self):
        if not self._rec_menu_target:
            return
        folder = os.path.dirname(self._rec_menu_target)
        try:
            if sys.platform.startswith("win"):
                os.startfile(folder)
            elif sys.platform == "darwin":
                subprocess.call(["open", folder])
            else:
                subprocess.call(["xdg-open", folder])
        except Exception as e:
            print("Open folder error:", e)

    # ---------- CLOSE ----------
    def on_close(self):
        try:
            self.bot.disable()
        except Exception:
            pass
        try:
            self.renamer.stop()
        except Exception:
            pass
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    app = SoundboardApp(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()
