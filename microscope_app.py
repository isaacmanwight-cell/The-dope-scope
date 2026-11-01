import json
import io
import csv
import base64
import html
import math
import queue
import threading
import uuid
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

EDS_REFERENCE_LINES = (
    ("C", "Kα", 0.277),
    ("N", "Kα", 0.392),
    ("O", "Kα", 0.525),
    ("F", "Kα", 0.677),
    ("Na", "Kα", 1.041),
    ("Mg", "Kα", 1.254),
    ("Al", "Kα", 1.487),
    ("Si", "Kα", 1.740),
    ("P", "Kα", 2.014),
    ("S", "Kα", 2.308),
    ("Cl", "Kα", 2.622),
    ("K", "Kα", 3.312),
    ("Ca", "Kα", 3.690),
    ("Ti", "Kα", 4.511),
    ("Cr", "Kα", 5.415),
    ("Mn", "Kα", 5.899),
    ("Fe", "Kα", 6.404),
    ("Co", "Kα", 6.930),
    ("Ni", "Kα", 7.478),
    ("Cu", "Kα", 8.048),
    ("Zn", "Kα", 8.638),
    ("Au", "Lα", 9.713),
    ("Ag", "Kα", 22.163),
)


def load_spectrum_csv(path):
    with open(path, "r", encoding="utf-8-sig", newline="") as spectrum_file:
        sample = spectrum_file.read(4096)
        spectrum_file.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",\t;")
        except csv.Error:
            dialect = csv.excel
        rows = csv.reader(spectrum_file, dialect)
        points = []
        for row in rows:
            if len(row) < 2:
                continue
            try:
                x_value = float(row[0].strip())
                intensity = float(row[1].strip())
            except ValueError:
                continue
            if math.isfinite(x_value) and math.isfinite(intensity):
                points.append((x_value, intensity))

    if len(points) < 3:
        raise ValueError(
            "The file must contain at least three rows with numeric values in "
            "the first two columns (x value and intensity)."
        )
    points.sort(key=lambda point: point[0])
    return tuple(points)


def detect_spectrum_peaks(points, minimum_percent=5):
    if not 0 <= minimum_percent <= 100:
        raise ValueError("Minimum peak level must be between 0 and 100 percent.")
    if len(points) < 3:
        return ()
    intensities = [point[1] for point in points]
    minimum = min(intensities)
    maximum = max(intensities)
    if maximum <= minimum:
        return ()
    cutoff = minimum + (maximum - minimum) * minimum_percent / 100
    separation = max(1, len(points) // 100)
    candidates = [
        index
        for index in range(1, len(points) - 1)
        if intensities[index] >= cutoff
        and intensities[index] > intensities[index - 1]
        and intensities[index] >= intensities[index + 1]
    ]
    selected = []
    for index in sorted(candidates, key=lambda item: intensities[item], reverse=True):
        if all(abs(index - other) >= separation for other in selected):
            selected.append(index)
    return tuple(
        (points[index][0], points[index][1])
        for index in sorted(selected, key=lambda item: points[item][0])
    )


try:
    import cv2
except ImportError:
    cv2 = None

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    serial = None
    list_ports = None

try:
    from PIL import Image
except ImportError:
    Image = None

try:
    from PIL import ImageTk
except ImportError:
    ImageTk = None

try:
    import numpy as np
except ImportError:
    np = None


def analyze_particles(frame, threshold, minimum_area, dark_particles):
    if cv2 is None:
        raise RuntimeError("OpenCV is required for particle analysis.")
    gray = cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY)
    threshold_mode = cv2.THRESH_BINARY_INV if dark_particles else cv2.THRESH_BINARY
    _, mask = cv2.threshold(gray, threshold, 255, threshold_mode)
    count, _, stats, _ = cv2.connectedComponentsWithStats(
        mask, connectivity=8
    )
    areas = [
        int(stats[index, cv2.CC_STAT_AREA])
        for index in range(1, count)
        if int(stats[index, cv2.CC_STAT_AREA]) >= minimum_area
    ]
    mean_diameter = (
        sum(math.sqrt(4 * area / math.pi) for area in areas) / len(areas)
        if areas
        else 0.0
    )
    return len(areas), sum(areas), mean_diameter


class MicroscopeApp:
    def __init__(self, root):
        self.root = root
        self.root.title("DIY Microscope")
        self.root.geometry("1700x950")
        self.root.minsize(1300, 720)
        self._configure_dark_theme()

        self.camera_stop = threading.Event()
        self.camera_frames = queue.Queue(maxsize=1)
        self.camera_events = queue.Queue()
        self.camera_thread = None
        self.latest_frame = None
        self.imported_frame = None
        self.imported_image_path = None
        self.frozen_frame = None
        self.photo = None
        self.placeholder_photo = None
        self.background_native_photo = None
        self.header_source = None
        self.header_native_photo = None
        self.background_image_path = Path(__file__).with_name("microscope_background.png")
        self.image_item = None
        self.transform = None
        self.drag_start = None
        self.drag_current = None
        self.measurement = None
        self.pixels_per_unit = None

        self.serial_stop = threading.Event()
        self.serial_writes = queue.Queue()
        self.serial_events = queue.Queue()
        self.serial_thread = None
        self.serial_connected = False
        self.control_config_path = Path(__file__).with_name("microscope_controls.json")
        self.controls = []
        self.lab_project = {
            "title": "Untitled project",
            "operator": "",
            "notes": "",
            "samples": [],
            "experiments": [],
            "images": [],
        }
        self.lab_project_path = None
        self.current_sample_id = None
        self.current_experiment_id = None

        self.camera_index = tk.StringVar(value="0")
        self.zoom = tk.DoubleVar(value=1.0)
        self.processing = tk.StringVar(value="Original")
        self.processing_index = tk.DoubleVar(value=0)
        self.threshold = tk.DoubleVar(value=128)
        self.invert_particles = tk.BooleanVar(value=True)
        self.min_particle_area = tk.StringVar(value="8")
        self.measure_tool = tk.StringVar(value="Pan")
        self.measure_tool_index = tk.DoubleVar(value=0)
        self.known_distance = tk.StringVar(value="1")
        self.distance_unit = tk.StringVar(value="mm")
        self.frame_frozen = False

        self.port = tk.StringVar()
        self.baud = tk.StringVar(value="115200")
        self.line_ending = tk.StringVar(value="Newline (\\n)")

        self._build_ui()
        self._refresh_ports()
        self._check_dependencies()
        self.root.after(30, self._process_events)
        self.root.protocol("WM_DELETE_WINDOW", self._close)

    def _configure_dark_theme(self):
        self.colors = {
            "background": "#171a21",
            "surface": "#20242d",
            "surface_alt": "#292e39",
            "field": "#11151b",
            "foreground": "#e6e9ef",
            "muted": "#aab2c0",
            "border": "#3a4250",
            "accent": "#4c8bf5",
            "accent_active": "#3a72d4",
            "warning": "#f0b45a",
            "disabled": "#777f8c",
            "selection": "#355b91",
        }
        self.root.configure(background=self.colors["background"])
        style = ttk.Style(self.root)
        if "clam" in style.theme_names():
            style.theme_use("clam")

        style.configure(
            ".",
            background=self.colors["background"],
            foreground=self.colors["foreground"],
            font=("Sans", 10),
        )
        style.configure("TFrame", background=self.colors["background"])
        style.configure("TLabel", background=self.colors["background"], foreground=self.colors["foreground"])
        style.configure(
            "TButton",
            background=self.colors["surface_alt"],
            foreground=self.colors["foreground"],
            bordercolor=self.colors["border"],
            padding=(9, 5),
        )
        style.map(
            "TButton",
            background=[
                ("disabled", self.colors["surface"]),
                ("pressed", self.colors["accent_active"]),
                ("active", "#343b49"),
            ],
            foreground=[("disabled", self.colors["disabled"])],
        )
        style.configure(
            "TEntry",
            fieldbackground=self.colors["field"],
            foreground=self.colors["foreground"],
            insertcolor=self.colors["foreground"],
            bordercolor=self.colors["border"],
            lightcolor=self.colors["border"],
            darkcolor=self.colors["border"],
        )
        style.configure(
            "TCombobox",
            fieldbackground=self.colors["field"],
            background=self.colors["surface_alt"],
            foreground=self.colors["foreground"],
            arrowcolor=self.colors["foreground"],
            bordercolor=self.colors["border"],
            lightcolor=self.colors["border"],
            darkcolor=self.colors["border"],
        )
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", self.colors["field"])],
            foreground=[("readonly", self.colors["foreground"])],
            selectbackground=[("readonly", self.colors["selection"])],
            selectforeground=[("readonly", self.colors["foreground"])],
        )
        style.configure(
            "TSpinbox",
            fieldbackground=self.colors["field"],
            background=self.colors["surface_alt"],
            foreground=self.colors["foreground"],
            arrowcolor=self.colors["foreground"],
            bordercolor=self.colors["border"],
        )
        style.configure(
            "TNotebook",
            background=self.colors["background"],
            bordercolor=self.colors["border"],
            tabmargins=(2, 4, 2, 0),
        )
        style.configure(
            "TNotebook.Tab",
            background=self.colors["surface"],
            foreground=self.colors["muted"],
            padding=(12, 7),
        )
        style.map(
            "TNotebook.Tab",
            background=[
                ("selected", self.colors["surface_alt"]),
                ("active", "#303744"),
            ],
            foreground=[("selected", self.colors["foreground"])],
        )
        style.configure(
            "TLabelframe",
            background=self.colors["background"],
            bordercolor=self.colors["border"],
        )
        style.configure(
            "TLabelframe.Label",
            background=self.colors["background"],
            foreground=self.colors["foreground"],
        )
        style.configure(
            "TCheckbutton",
            background=self.colors["background"],
            foreground=self.colors["foreground"],
        )
        style.map(
            "TCheckbutton",
            background=[("active", self.colors["background"])],
            foreground=[("disabled", self.colors["disabled"])],
        )
        style.configure(
            "Horizontal.TScale",
            background=self.colors["background"],
            troughcolor=self.colors["field"],
            bordercolor=self.colors["border"],
            lightcolor=self.colors["accent"],
            darkcolor=self.colors["accent"],
        )
        style.configure(
            "TScrollbar",
            background=self.colors["surface_alt"],
            troughcolor=self.colors["field"],
            bordercolor=self.colors["background"],
            arrowcolor=self.colors["foreground"],
        )
        style.configure("TSeparator", background=self.colors["border"])

    def _build_ui(self):
        self.root.columnconfigure(0, weight=4)
        self.root.columnconfigure(1, weight=7)
        self.root.columnconfigure(2, weight=5)
        self.root.rowconfigure(0, weight=1)

        camera_panel = ttk.Frame(self.root, padding=12)
        camera_panel.grid(row=0, column=1, sticky="nsew")
        camera_panel.columnconfigure(0, weight=1)
        camera_panel.rowconfigure(1, weight=1)
        ttk.Label(camera_panel, text="DIY Microscope", font=("", 18, "bold")).grid(
            row=0, column=0, sticky="w", pady=(0, 10)
        )

        self.preview = tk.Canvas(
            camera_panel,
            background=self.colors["field"],
            highlightthickness=1,
            highlightbackground=self.colors["border"],
        )
        self.preview.grid(row=1, column=0, sticky="nsew")
        self.preview.bind("<ButtonPress-1>", self._begin_measurement)
        self.preview.bind("<B1-Motion>", self._update_measurement)
        self.preview.bind("<ButtonRelease-1>", self._finish_measurement)
        self.preview.bind("<Configure>", lambda _event: self._render_frame())
        self._draw_placeholder()

        controls = ttk.Frame(camera_panel)
        controls.grid(row=2, column=0, sticky="ew", pady=(9, 0))
        ttk.Label(controls, text="Camera").pack(side="left")
        ttk.Spinbox(controls, from_=0, to=20, width=4, textvariable=self.camera_index).pack(
            side="left", padx=(4, 8)
        )
        self.camera_button = ttk.Button(
            controls, text="Start camera", command=self._toggle_camera
        )
        self.camera_button.pack(side="left")
        self.import_image_button = ttk.Button(
            controls, text="Open image…", command=self._open_image
        )
        self.import_image_button.pack(side="left", padx=(6, 0))
        self.freeze_button = ttk.Button(
            controls, text="Freeze frame", command=self._toggle_freeze, state="disabled"
        )
        self.freeze_button.pack(side="left", padx=(6, 12))
        ttk.Label(controls, text="Zoom").pack(side="left")
        ttk.Scale(
            controls,
            from_=1.0,
            to=5.0,
            variable=self.zoom,
            command=lambda _value: self._render_frame(),
        ).pack(side="left", fill="x", expand=True, padx=6)
        self.zoom_label = ttk.Label(controls, text="1.0×", width=5)
        self.zoom_label.pack(side="left")
        self.camera_status = ttk.Label(camera_panel, text="Camera stopped")
        self.camera_status.grid(row=3, column=0, sticky="w", pady=(5, 0))
        ttk.Button(
            camera_panel,
            text="Samples, experiments and reports…",
            command=self._open_lab_project_manager,
        ).grid(row=4, column=0, sticky="e", pady=(6, 0))
        ttk.Button(
            camera_panel,
            text="Batch particle workflow…",
            command=self._open_batch_particle_workflow,
        ).grid(row=5, column=0, sticky="e", pady=(5, 0))

        analysis_panel = ttk.Frame(self.root, padding=10)
        analysis_panel.grid(row=0, column=0, sticky="nsew")
        self._build_analysis_panel(analysis_panel)

        hardware_column = ttk.Frame(self.root, padding=(0, 12, 12, 12))
        hardware_column.grid(row=0, column=2, sticky="nsew")
        hardware_column.columnconfigure(0, weight=1)
        hardware_column.rowconfigure(0, weight=1)
        hardware_column.rowconfigure(1, weight=1)

        controls_panel = ttk.Frame(hardware_column, padding=8)
        controls_panel.grid(row=0, column=0, sticky="nsew")
        controller_panel = ttk.Frame(hardware_column, padding=8)
        controller_panel.grid(row=1, column=0, sticky="nsew")
        self._build_controls_panel(controls_panel)
        self._build_controller_panel(controller_panel)

    def _build_analysis_panel(self, panel):
        panel.columnconfigure(0, weight=1)

        self._add_image_header(panel, "IMAGE TOOLS", row=0)
        processing_options = ("Original", "Grayscale", "Edges", "Threshold", "Intensity map")
        self._create_tool_slider(
            panel,
            row=1,
            title="Image view",
            options=processing_options,
            variable=self.processing_index,
            initial=self.processing.get(),
            on_change=self._set_processing,
            selection_attribute="processing_selection",
            scale_attribute="processing_scale",
        )
        ttk.Label(
            panel,
            text="Intensity map colors image brightness; it is not measured 3D height.",
            wraplength=350,
        ).grid(row=2, column=0, sticky="w", pady=(2, 0))

        threshold_row = ttk.Frame(panel)
        threshold_row.grid(row=3, column=0, sticky="ew", pady=(5, 9))
        ttk.Label(threshold_row, text="Threshold").pack(side="left")
        ttk.Scale(
            threshold_row,
            from_=0,
            to=255,
            variable=self.threshold,
            command=lambda _value: self._render_frame(),
        ).pack(side="left", fill="x", expand=True, padx=6)
        self.threshold_label = ttk.Label(threshold_row, text="128", width=4)
        self.threshold_label.pack(side="left")

        ttk.Separator(panel).grid(row=4, column=0, sticky="ew", pady=5)
        ttk.Label(panel, text="Measure and calibrate", font=("", 12, "bold")).grid(
            row=5, column=0, sticky="w", pady=(2, 5)
        )
        measurement_options = (
            "Pan",
            "Length",
            "Rectangle area",
            "Calibrate scale",
            "Surface profile",
        )
        tool_row = ttk.Frame(panel)
        tool_row.grid(row=6, column=0, sticky="ew")
        self._create_tool_slider(
            tool_row,
            row=0,
            title="Measurement tool",
            options=measurement_options,
            variable=self.measure_tool_index,
            initial=self.measure_tool.get(),
            on_change=self._set_measure_tool,
            selection_attribute="measure_tool_selection",
            scale_attribute="measure_tool_scale",
        )

        calibration = ttk.Frame(tool_row)
        calibration.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(7, 2))
        ttk.Label(calibration, text="Known line").pack(side="left")
        ttk.Entry(calibration, textvariable=self.known_distance, width=8).pack(
            side="left", padx=(5, 4)
        )
        ttk.Combobox(
            calibration,
            textvariable=self.distance_unit,
            values=("mm", "µm", "cm", "in"),
            state="readonly",
            width=5,
        ).pack(side="left")
        ttk.Label(
            tool_row,
            text="Choose Calibrate scale, then drag across a known ruler distance.",
            wraplength=350,
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(2, 7))

        particle_box = ttk.LabelFrame(panel, text="Particle count and diameter", padding=7)
        particle_box.grid(row=7, column=0, sticky="ew", pady=(6, 8))
        particle_box.columnconfigure(1, weight=1)
        ttk.Checkbutton(
            particle_box,
            text="Count dark particles (unchecked = bright particles)",
            variable=self.invert_particles,
        ).grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(particle_box, text="Minimum area (pixels)").grid(
            row=1, column=0, sticky="w", pady=(5, 0)
        )
        ttk.Entry(particle_box, textvariable=self.min_particle_area, width=8).grid(
            row=1, column=1, sticky="w", padx=(5, 0), pady=(5, 0)
        )
        self.particle_button = ttk.Button(
            particle_box, text="Count particles in frame", command=self._count_particles
        )
        self.particle_button.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(7, 0))

        image_actions = ttk.Frame(panel)
        ttk.Button(
            panel,
            text="EDS / spectroscopy / diffraction…",
            command=self._open_spectroscopy_analysis,
        ).grid(row=8, column=0, sticky="ew", pady=(6, 7))
        image_actions.grid(row=9, column=0, sticky="ew", pady=(0, 7))
        ttk.Button(image_actions, text="Save image…", command=self._save_image).pack(
            side="left", fill="x", expand=True
        )
        ttk.Button(image_actions, text="Clear measurement", command=self._clear_measurement).pack(
            side="left", fill="x", expand=True, padx=(5, 0)
        )

        ttk.Label(panel, text="Results / analysis log", font=("", 10, "bold")).grid(
            row=10, column=0, sticky="w"
        )
        self.analysis_output = tk.Text(panel, height=8, wrap="word", state="disabled")
        self._style_text(self.analysis_output)
        self.analysis_output.grid(row=11, column=0, sticky="nsew", pady=(4, 0))
        panel.rowconfigure(11, weight=1)

    def _open_batch_particle_workflow(self):
        if cv2 is None or Image is None or np is None:
            messagebox.showerror(
                "Analysis dependencies missing",
                "Install the requirements to use batch image analysis.",
                parent=self.root,
            )
            return
        dialog = tk.Toplevel(self.root)
        dialog.title("Batch particle workflow")
        dialog.geometry("520x250")
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.grab_set()
        threshold = tk.IntVar(value=int(round(self.threshold.get())))
        minimum_area = tk.StringVar(value=self.min_particle_area.get())
        dark_particles = tk.BooleanVar(value=self.invert_particles.get())

        ttk.Label(
            dialog,
            text="Analyze every supported image in one folder using the same settings.",
            wraplength=480,
        ).grid(row=0, column=0, columnspan=3, sticky="w", padx=12, pady=(12, 8))
        ttk.Label(dialog, text="Threshold").grid(
            row=1, column=0, sticky="w", padx=12, pady=5
        )
        threshold_label = ttk.Label(dialog, text=str(threshold.get()), width=4)
        ttk.Scale(
            dialog,
            from_=0,
            to=255,
            variable=threshold,
            command=lambda _value: threshold_label.configure(text=str(threshold.get())),
        ).grid(row=1, column=1, sticky="ew", padx=6, pady=5)
        threshold_label.grid(row=1, column=2, sticky="w", padx=(0, 12), pady=5)
        ttk.Label(dialog, text="Minimum object area (pixels)").grid(
            row=2, column=0, sticky="w", padx=12, pady=5
        )
        ttk.Entry(dialog, textvariable=minimum_area, width=9).grid(
            row=2, column=1, sticky="w", padx=6, pady=5
        )
        ttk.Checkbutton(
            dialog,
            text="Count dark particles (unchecked = bright particles)",
            variable=dark_particles,
        ).grid(row=3, column=0, columnspan=3, sticky="w", padx=12, pady=5)
        status = ttk.Label(dialog, text="Results are pixel-based; physical calibration is not applied.")
        status.grid(row=4, column=0, columnspan=3, sticky="w", padx=12, pady=5)
        dialog.columnconfigure(1, weight=1)

        def run_workflow():
            try:
                min_area = int(minimum_area.get())
                if min_area < 1:
                    raise ValueError
            except ValueError:
                messagebox.showerror(
                    "Invalid area",
                    "Minimum object area must be a positive whole number.",
                    parent=dialog,
                )
                return
            folder = filedialog.askdirectory(
                parent=dialog, title="Select folder of microscope images"
            )
            if not folder:
                return
            try:
                image_paths = sorted(
                    path
                    for path in Path(folder).iterdir()
                    if path.is_file()
                    and path.suffix.lower()
                    in {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp", ".gif"}
                )
            except OSError as error:
                messagebox.showerror(
                    "Could not read folder", f"Could not list the selected folder:\n{error}", parent=dialog
                )
                return
            if not image_paths:
                messagebox.showinfo(
                    "No images found",
                    "The selected folder has no supported image files.",
                    parent=dialog,
                )
                return
            output_path = filedialog.asksaveasfilename(
                parent=dialog,
                title="Save batch analysis results",
                defaultextension=".csv",
                filetypes=(("CSV results", "*.csv"),),
            )
            if not output_path:
                return
            if Path(output_path).resolve() in {
                image_path.resolve() for image_path in image_paths
            }:
                messagebox.showerror(
                    "Output would overwrite an image",
                    "Choose a results file path that does not replace one of the source images.",
                    parent=dialog,
                )
                return

            results = []
            for image_path in image_paths:
                try:
                    with Image.open(image_path) as source:
                        frame = np.array(source.convert("RGB"))
                    count, area, mean_diameter = analyze_particles(
                        frame,
                        int(threshold.get()),
                        min_area,
                        dark_particles.get(),
                    )
                except (OSError, ValueError, RuntimeError) as error:
                    results.append((image_path.name, "", "", "", f"Error: {error}"))
                    continue
                except cv2.error as error:
                    results.append((image_path.name, "", "", "", f"OpenCV error: {error}"))
                    continue
                results.append(
                    (
                        image_path.name,
                        count,
                        area,
                        f"{mean_diameter:.4f}",
                        "OK",
                    )
                )

            try:
                with open(output_path, "w", encoding="utf-8", newline="") as output:
                    writer = csv.writer(output)
                    writer.writerow(
                        (
                            "image",
                            "threshold",
                            "minimum_area_px",
                            "polarity",
                            "particle_count",
                            "segmented_area_px2",
                            "mean_equivalent_diameter_px",
                            "status",
                        )
                    )
                    polarity = "dark" if dark_particles.get() else "bright"
                    for filename, count, area, diameter, result_status in results:
                        writer.writerow(
                            (
                                filename,
                                threshold.get(),
                                min_area,
                                polarity,
                                count,
                                area,
                                diameter,
                                result_status,
                            )
                        )
            except OSError as error:
                messagebox.showerror(
                    "Could not save results",
                    f"Could not write the results CSV:\n{error}",
                    parent=dialog,
                )
                return

            failed = sum(result[-1] != "OK" for result in results)
            status.configure(
                text=f"Processed {len(results)} image(s); {failed} failed. Results: {Path(output_path).name}"
            )
            self._log_analysis(
                f"Batch particle workflow: {len(results)} images processed, {failed} failed; "
                f"CSV saved to {output_path}. Measurements are in pixels."
            )
            messagebox.showinfo(
                "Workflow complete",
                f"Processed {len(results)} image(s); {failed} failed.\nResults saved to:\n{output_path}",
                parent=dialog,
            )

        ttk.Button(dialog, text="Run workflow…", command=run_workflow).grid(
            row=5, column=0, columnspan=3, sticky="e", padx=12, pady=(8, 12)
        )

    def _open_lab_project_manager(self):
        dialog = tk.Toplevel(self.root)
        dialog.title("Samples, experiments and reports")
        dialog.geometry("900x650")
        dialog.minsize(720, 500)
        dialog.transient(self.root)
        dialog.configure(background=self.colors["background"])
        dialog.columnconfigure(0, weight=1)
        dialog.columnconfigure(1, weight=2)
        dialog.rowconfigure(2, weight=1)

        title = tk.StringVar(value=self.lab_project["title"])
        operator = tk.StringVar(value=self.lab_project["operator"])
        status = ttk.Label(dialog, text=self._lab_project_status())
        ttk.Label(dialog, text="Project title").grid(
            row=0, column=0, sticky="w", padx=12, pady=(12, 2)
        )
        ttk.Entry(dialog, textvariable=title).grid(
            row=0, column=1, sticky="ew", padx=(5, 12), pady=(12, 2)
        )
        ttk.Label(dialog, text="Operator").grid(
            row=1, column=0, sticky="w", padx=12, pady=2
        )
        ttk.Entry(dialog, textvariable=operator).grid(
            row=1, column=1, sticky="ew", padx=(5, 12), pady=2
        )

        lists = ttk.Frame(dialog, padding=12)
        lists.grid(row=2, column=0, columnspan=2, sticky="nsew")
        lists.columnconfigure(0, weight=1)
        lists.columnconfigure(1, weight=1)
        lists.rowconfigure(1, weight=1)
        ttk.Label(lists, text="Samples", font=("", 11, "bold")).grid(
            row=0, column=0, sticky="w", pady=(0, 5)
        )
        ttk.Label(lists, text="Experiments", font=("", 11, "bold")).grid(
            row=0, column=1, sticky="w", padx=(10, 0), pady=(0, 5)
        )
        samples = ttk.Treeview(
            lists, columns=("material",), show="tree headings", selectmode="browse"
        )
        samples.heading("#0", text="Sample")
        samples.heading("material", text="Material")
        samples.column("#0", width=190)
        samples.column("material", width=120)
        samples.grid(row=1, column=0, sticky="nsew")
        experiments = ttk.Treeview(
            lists,
            columns=("date", "images"),
            show="tree headings",
            selectmode="browse",
        )
        experiments.heading("#0", text="Experiment")
        experiments.heading("date", text="Date")
        experiments.heading("images", text="Images")
        experiments.column("#0", width=180)
        experiments.column("date", width=95)
        experiments.column("images", width=55, anchor="center")
        experiments.grid(row=1, column=1, sticky="nsew", padx=(10, 0))

        notes_frame = ttk.Frame(dialog, padding=(12, 0, 12, 8))
        notes_frame.grid(row=3, column=0, columnspan=2, sticky="ew")
        notes_frame.columnconfigure(0, weight=1)
        ttk.Label(notes_frame, text="Project notes").grid(row=0, column=0, sticky="w")
        notes = tk.Text(notes_frame, height=4, wrap="word")
        self._style_text(notes)
        notes.grid(row=1, column=0, sticky="ew", pady=(3, 0))
        notes.insert("1.0", self.lab_project["notes"])

        actions = ttk.Frame(dialog, padding=(12, 4))
        actions.grid(row=4, column=0, columnspan=2, sticky="ew")
        actions.columnconfigure(3, weight=1)
        status.grid(row=5, column=0, columnspan=2, sticky="w", padx=12, pady=(2, 10))

        def persist_fields():
            self.lab_project["title"] = title.get().strip() or "Untitled project"
            self.lab_project["operator"] = operator.get().strip()
            self.lab_project["notes"] = notes.get("1.0", "end-1c").strip()

        def refresh_experiments():
            for item in experiments.get_children():
                experiments.delete(item)
            visible_experiments = [
                experiment
                for experiment in self.lab_project["experiments"]
                if self.current_sample_id is None
                or experiment["sample_id"] == self.current_sample_id
            ]
            for experiment in visible_experiments:
                image_count = sum(
                    image["experiment_id"] == experiment["id"]
                    for image in self.lab_project["images"]
                )
                experiments.insert(
                    "",
                    "end",
                    iid=experiment["id"],
                    text=experiment["name"],
                    values=(experiment.get("date", ""), image_count),
                )
            experiment_ids = {item["id"] for item in visible_experiments}
            if self.current_experiment_id not in experiment_ids:
                self.current_experiment_id = None
            if self.current_experiment_id:
                experiments.selection_set(self.current_experiment_id)
            status.configure(text=self._lab_project_status())

        def refresh():
            for item in samples.get_children():
                samples.delete(item)
            for sample in self.lab_project["samples"]:
                samples.insert(
                    "",
                    "end",
                    iid=sample["id"],
                    text=sample["name"],
                    values=(sample.get("material", ""),),
                )
            sample_ids = {sample["id"] for sample in self.lab_project["samples"]}
            if self.current_sample_id not in sample_ids:
                self.current_sample_id = None
            if self.current_sample_id:
                samples.selection_set(self.current_sample_id)
            refresh_experiments()

        def select_sample(_event=None):
            selection = samples.selection()
            selected_sample_id = selection[0] if selection else None
            if selected_sample_id == self.current_sample_id:
                return
            self.current_sample_id = selected_sample_id
            self.current_experiment_id = None
            refresh_experiments()

        def select_experiment(_event=None):
            selection = experiments.selection()
            self.current_experiment_id = selection[0] if selection else None

        def add_sample():
            persist_fields()
            self._add_lab_sample(dialog, refresh)

        def edit_sample():
            persist_fields()
            sample = next(
                (
                    item
                    for item in self.lab_project["samples"]
                    if item["id"] == self.current_sample_id
                ),
                None,
            )
            if sample is None:
                messagebox.showinfo(
                    "Select a sample", "Select a sample to edit.", parent=dialog
                )
                return
            self._add_lab_sample(dialog, refresh, sample)

        def add_experiment():
            persist_fields()
            if self.current_sample_id is None:
                messagebox.showinfo(
                    "Select a sample", "Select a sample before creating an experiment.", parent=dialog
                )
                return
            self._add_lab_experiment(dialog, refresh)

        def edit_experiment():
            persist_fields()
            experiment = next(
                (
                    item
                    for item in self.lab_project["experiments"]
                    if item["id"] == self.current_experiment_id
                ),
                None,
            )
            if experiment is None:
                messagebox.showinfo(
                    "Select an experiment", "Select an experiment to edit.", parent=dialog
                )
                return
            self._add_lab_experiment(dialog, refresh, experiment)

        def add_image():
            persist_fields()
            if self.current_experiment_id is None:
                messagebox.showinfo(
                    "Select an experiment",
                    "Select an experiment before adding the current image.",
                    parent=dialog,
                )
                return
            if self.lab_project_path is None:
                if not self._save_lab_project(dialog):
                    return
            self._add_current_image_to_experiment(dialog, refresh)

        def save_project():
            persist_fields()
            self._save_lab_project(dialog)
            status.configure(text=self._lab_project_status())

        def open_project():
            if self._load_lab_project(dialog):
                title.set(self.lab_project["title"])
                operator.set(self.lab_project["operator"])
                notes.delete("1.0", tk.END)
                notes.insert("1.0", self.lab_project["notes"])
                refresh()

        def new_project():
            if not messagebox.askyesno(
                "New project",
                "Start a new project? Save the current project first if you need to keep it.",
                parent=dialog,
            ):
                return
            self.lab_project = {
                "schema_version": 1,
                "title": "Untitled project",
                "operator": "",
                "notes": "",
                "samples": [],
                "experiments": [],
                "images": [],
            }
            self.lab_project_path = None
            self.current_sample_id = None
            self.current_experiment_id = None
            title.set(self.lab_project["title"])
            operator.set("")
            notes.delete("1.0", tk.END)
            refresh()

        def make_report():
            persist_fields()
            self._generate_lab_report(dialog)

        samples.bind("<<TreeviewSelect>>", select_sample)
        experiments.bind("<<TreeviewSelect>>", select_experiment)
        ttk.Button(actions, text="New project", command=new_project).grid(
            row=0, column=0, sticky="w"
        )
        ttk.Button(actions, text="Open…", command=open_project).grid(
            row=0, column=1, sticky="w", padx=(5, 0)
        )
        ttk.Button(actions, text="Save project", command=save_project).grid(
            row=0, column=2, sticky="w", padx=(5, 0)
        )
        ttk.Button(actions, text="Export HTML report…", command=make_report).grid(
            row=0, column=3, sticky="e"
        )
        ttk.Button(actions, text="Add sample…", command=add_sample).grid(
            row=1, column=0, sticky="w", pady=(5, 0)
        )
        ttk.Button(actions, text="Edit sample…", command=edit_sample).grid(
            row=1, column=1, sticky="w", padx=(5, 0), pady=(5, 0)
        )
        ttk.Button(actions, text="Add experiment…", command=add_experiment).grid(
            row=1, column=2, sticky="w", padx=(5, 0), pady=(5, 0)
        )
        ttk.Button(actions, text="Edit experiment…", command=edit_experiment).grid(
            row=1, column=3, sticky="w", padx=(5, 0), pady=(5, 0)
        )
        ttk.Button(actions, text="Add current image", command=add_image).grid(
            row=1, column=4, sticky="w", padx=(5, 0), pady=(5, 0)
        )
        refresh()

    def _lab_project_status(self):
        location = self.lab_project_path.name if self.lab_project_path else "Not saved"
        return (
            f"{location} · {len(self.lab_project['samples'])} samples · "
            f"{len(self.lab_project['experiments'])} experiments · "
            f"{len(self.lab_project['images'])} images"
        )

    def _add_lab_sample(self, parent, on_saved, sample=None):
        dialog = tk.Toplevel(parent)
        dialog.title("Edit sample" if sample else "Add sample")
        dialog.transient(parent)
        dialog.grab_set()
        fields = {}
        for row, (key, label) in enumerate(
            (("name", "Sample name"), ("material", "Material / description"), ("notes", "Notes"))
        ):
            ttk.Label(dialog, text=label).grid(
                row=row, column=0, sticky="w", padx=10, pady=5
            )
            entry = ttk.Entry(dialog, width=42)
            if sample is not None:
                entry.insert(0, sample.get(key, ""))
            entry.grid(row=row, column=1, sticky="ew", padx=(5, 10), pady=5)
            fields[key] = entry
        dialog.columnconfigure(1, weight=1)

        def save():
            name = fields["name"].get().strip()
            if not name:
                messagebox.showerror("Sample name required", "Enter a sample name.", parent=dialog)
                return
            updated_sample = {
                "id": sample["id"] if sample else uuid.uuid4().hex,
                "name": name,
                "material": fields["material"].get().strip(),
                "notes": fields["notes"].get().strip(),
            }
            if sample is None:
                self.lab_project["samples"].append(updated_sample)
                self.current_sample_id = updated_sample["id"]
                self.current_experiment_id = None
            else:
                index = self.lab_project["samples"].index(sample)
                self.lab_project["samples"][index] = updated_sample
            dialog.destroy()
            on_saved()

        ttk.Button(dialog, text="Cancel", command=dialog.destroy).grid(
            row=3, column=0, padx=10, pady=10, sticky="e"
        )
        ttk.Button(dialog, text="Save sample" if sample else "Add sample", command=save).grid(
            row=3, column=1, padx=10, pady=10, sticky="e"
        )
        fields["name"].focus_set()

    def _add_lab_experiment(self, parent, on_saved, experiment=None):
        dialog = tk.Toplevel(parent)
        dialog.title("Edit experiment" if experiment else "Add experiment")
        dialog.transient(parent)
        dialog.grab_set()
        fields = {}
        initial = {
            "name": experiment.get("name", "") if experiment else "",
            "date": (
                experiment.get("date", datetime.now().date().isoformat())
                if experiment
                else datetime.now().date().isoformat()
            ),
            "notes": experiment.get("notes", "") if experiment else "",
        }
        for row, (key, label) in enumerate(
            (("name", "Experiment name"), ("date", "Date (YYYY-MM-DD)"), ("notes", "Notes"))
        ):
            ttk.Label(dialog, text=label).grid(
                row=row, column=0, sticky="w", padx=10, pady=5
            )
            entry = ttk.Entry(dialog, width=42)
            entry.insert(0, initial[key])
            entry.grid(row=row, column=1, sticky="ew", padx=(5, 10), pady=5)
            fields[key] = entry
        dialog.columnconfigure(1, weight=1)

        def save():
            name = fields["name"].get().strip()
            experiment_date = fields["date"].get().strip()
            if not name:
                messagebox.showerror(
                    "Experiment name required", "Enter an experiment name.", parent=dialog
                )
                return
            try:
                datetime.strptime(experiment_date, "%Y-%m-%d")
            except ValueError:
                messagebox.showerror(
                    "Invalid date", "Use the date format YYYY-MM-DD.", parent=dialog
                )
                return
            updated_experiment = {
                "id": experiment["id"] if experiment else uuid.uuid4().hex,
                "sample_id": (
                    experiment["sample_id"] if experiment else self.current_sample_id
                ),
                "name": name,
                "date": experiment_date,
                "notes": fields["notes"].get().strip(),
            }
            if experiment is None:
                self.lab_project["experiments"].append(updated_experiment)
            else:
                index = self.lab_project["experiments"].index(experiment)
                self.lab_project["experiments"][index] = updated_experiment
            self.current_experiment_id = updated_experiment["id"]
            dialog.destroy()
            on_saved()

        ttk.Button(dialog, text="Cancel", command=dialog.destroy).grid(
            row=3, column=0, padx=10, pady=10, sticky="e"
        )
        ttk.Button(
            dialog,
            text="Save experiment" if experiment else "Add experiment",
            command=save,
        ).grid(
            row=3, column=1, padx=10, pady=10, sticky="e"
        )
        fields["name"].focus_set()

    def _save_lab_project(self, parent):
        if self.lab_project_path is None:
            path = filedialog.asksaveasfilename(
                parent=parent,
                title="Save microscope project",
                defaultextension=".json",
                filetypes=(("Microscope project", "*.json"),),
            )
            if not path:
                return False
            self.lab_project_path = Path(path)
        self.lab_project["schema_version"] = 1
        try:
            self.lab_project_path.write_text(
                json.dumps(self.lab_project, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        except (OSError, TypeError, ValueError) as error:
            messagebox.showerror(
                "Could not save project", f"Could not save project file:\n{error}", parent=parent
            )
            return False
        return True

    def _load_lab_project(self, parent):
        path = filedialog.askopenfilename(
            parent=parent,
            title="Open microscope project",
            filetypes=(("Microscope project", "*.json"), ("All files", "*")),
        )
        if not path:
            return False
        try:
            project = json.loads(Path(path).read_text(encoding="utf-8"))
            if (
                not isinstance(project, dict)
                or project.get("schema_version") != 1
                or not all(
                    isinstance(project.get(key), list)
                    for key in ("samples", "experiments", "images")
                )
                or not all(
                    isinstance(project.get(key, ""), str)
                    for key in ("title", "operator", "notes")
                )
            ):
                raise ValueError("This file is not a supported microscope project.")
            if not all(
                isinstance(item, dict)
                and isinstance(item.get("id"), str)
                and isinstance(item.get("name"), str)
                and isinstance(item.get("material", ""), str)
                and isinstance(item.get("notes", ""), str)
                for item in project["samples"]
            ):
                raise ValueError("The project contains an invalid sample record.")
            sample_ids = {item["id"] for item in project["samples"]}
            if len(sample_ids) != len(project["samples"]):
                raise ValueError("The project contains duplicate sample IDs.")
            if not all(
                isinstance(item, dict)
                and isinstance(item.get("id"), str)
                and isinstance(item.get("sample_id"), str)
                and item.get("sample_id") in sample_ids
                and isinstance(item.get("name"), str)
                and isinstance(item.get("date"), str)
                and isinstance(item.get("notes", ""), str)
                for item in project["experiments"]
            ):
                raise ValueError("The project contains an invalid experiment record.")
            experiment_ids = {item["id"] for item in project["experiments"]}
            if len(experiment_ids) != len(project["experiments"]):
                raise ValueError("The project contains duplicate experiment IDs.")
            if not all(
                isinstance(item, dict)
                and isinstance(item.get("experiment_id"), str)
                and item.get("experiment_id") in experiment_ids
                and isinstance(item.get("file"), str)
                and isinstance(item.get("caption"), str)
                for item in project["images"]
            ):
                raise ValueError("The project contains an invalid image record.")
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
            messagebox.showerror(
                "Could not open project", f"Could not read the project:\n{error}", parent=parent
            )
            return False
        self.lab_project = {
            "schema_version": 1,
            "title": project.get("title", "Untitled project"),
            "operator": project.get("operator", ""),
            "notes": project.get("notes", ""),
            "samples": project["samples"],
            "experiments": project["experiments"],
            "images": project["images"],
        }
        self.lab_project_path = Path(path)
        self.current_sample_id = None
        self.current_experiment_id = None
        return True

    def _add_current_image_to_experiment(self, parent, refresh):
        frame = self._active_frame()
        if frame is None or Image is None:
            messagebox.showinfo(
                "No image", "Start the camera or open an image before adding a figure.", parent=parent
            )
            return
        if self.lab_project_path is None or self.current_experiment_id is None:
            messagebox.showerror(
                "Project required",
                "Save the project and select an experiment before adding an image.",
                parent=parent,
            )
            return
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        filename = f"figure-{timestamp}.png"
        asset_directory = self.lab_project_path.parent / f"{self.lab_project_path.stem}_assets"
        try:
            asset_directory.mkdir(parents=True, exist_ok=True)
            Image.fromarray(frame).save(asset_directory / filename, format="PNG")
        except (OSError, ValueError) as error:
            messagebox.showerror(
                "Could not save image", f"Could not add the image to the project:\n{error}", parent=parent
            )
            return
        self.lab_project["images"].append(
            {
                "experiment_id": self.current_experiment_id,
                "file": f"{asset_directory.name}/{filename}",
                "caption": self.imported_image_path.name
                if self.imported_image_path is not None and self.latest_frame is None
                else f"Microscope image captured {datetime.now().isoformat(timespec='seconds')}",
            }
        )
        if not self._save_lab_project(parent):
            self.lab_project["images"].pop()
            return
        refresh()

    def _generate_lab_report(self, parent):
        path = filedialog.asksaveasfilename(
            parent=parent,
            title="Export scientific report",
            defaultextension=".html",
            filetypes=(("HTML report", "*.html"),),
        )
        if not path:
            return
        project = self.lab_project
        sections = [
            "<!doctype html><html><head><meta charset=\"utf-8\">",
            f"<title>{html.escape(project['title'])}</title>",
            "<style>body{font:16px sans-serif;max-width:1000px;margin:2em auto;padding:0 1em;color:#20242d}"
            "h1,h2{color:#18355f}table{border-collapse:collapse;width:100%}"
            "th,td{border:1px solid #ccd2dc;padding:.5em;text-align:left}"
            "figure{margin:1.5em 0}img{max-width:100%;height:auto}pre{white-space:pre-wrap}"
            "small{color:#555}</style></head><body>",
            f"<h1>{html.escape(project['title'])}</h1>",
            f"<p><strong>Operator:</strong> {html.escape(project['operator'] or 'Not specified')}</p>",
            f"<p><strong>Generated:</strong> {datetime.now().isoformat(timespec='seconds')}</p>",
            f"<h2>Project notes</h2><p>{html.escape(project['notes'] or 'No notes recorded.').replace(chr(10), '<br>')}</p>",
            "<h2>Samples</h2><table><thead><tr><th>Sample</th><th>Material</th><th>Notes</th></tr></thead><tbody>",
        ]
        for sample in project["samples"]:
            sections.append(
                "<tr>"
                f"<td>{html.escape(sample['name'])}</td>"
                f"<td>{html.escape(sample.get('material', ''))}</td>"
                f"<td>{html.escape(sample.get('notes', ''))}</td>"
                "</tr>"
            )
        sections.append("</tbody></table><h2>Experiments and figures</h2>")
        samples_by_id = {sample["id"]: sample for sample in project["samples"]}
        for experiment in project["experiments"]:
            sample = samples_by_id.get(experiment["sample_id"], {})
            sections.extend(
                (
                    f"<h3>{html.escape(experiment['name'])}</h3>",
                    f"<p><strong>Sample:</strong> {html.escape(sample.get('name', 'Unknown'))} · "
                    f"<strong>Date:</strong> {html.escape(experiment.get('date', ''))}</p>",
                    f"<p>{html.escape(experiment.get('notes', '')).replace(chr(10), '<br>')}</p>",
                )
            )
            for image in project["images"]:
                if image["experiment_id"] != experiment["id"]:
                    continue
                image_path = (
                    self.lab_project_path.parent / image["file"]
                    if self.lab_project_path is not None
                    else None
                )
                try:
                    if image_path is None:
                        raise OSError("Project image path is unavailable.")
                    image_path.resolve().relative_to(self.lab_project_path.parent.resolve())
                    encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
                except (OSError, ValueError):
                    sections.append(
                        f"<p><em>Figure unavailable: {html.escape(image['file'])}</em></p>"
                    )
                else:
                    sections.append(
                        f"<figure><img src=\"data:image/png;base64,{encoded}\" "
                        f"alt=\"{html.escape(image['caption'])}\">"
                        f"<figcaption>{html.escape(image['caption'])}</figcaption></figure>"
                    )
        log_text = self.analysis_output.get("1.0", "end-1c").strip()
        sections.extend(
            (
                "<h2>Analysis log</h2>",
                f"<pre>{html.escape(log_text or 'No analysis log recorded.')}</pre>",
                "<p><small>This report contains observations and figures recorded by the user. "
                "It is not a validated scientific or regulatory report.</small></p></body></html>",
            )
        )
        try:
            Path(path).write_text("\n".join(sections), encoding="utf-8")
        except OSError as error:
            messagebox.showerror(
                "Could not export report", f"Could not write the report:\n{error}", parent=parent
            )
            return
        messagebox.showinfo(
            "Report exported", f"Scientific report saved to:\n{path}", parent=parent
        )

    def _open_spectroscopy_analysis(self):
        dialog = tk.Toplevel(self.root)
        dialog.title("EDS, spectroscopy and diffraction analysis")
        dialog.geometry("1050x760")
        dialog.minsize(760, 560)
        dialog.configure(background=self.colors["background"])
        dialog.columnconfigure(0, weight=1)
        dialog.rowconfigure(2, weight=3)
        dialog.rowconfigure(4, weight=2)

        mode = tk.StringVar(value="EDS / X-ray microanalysis")
        x_unit = tk.StringVar(value="keV")
        peak_level = tk.DoubleVar(value=5)
        tolerance = tk.StringVar(value="0.10")
        data = {"points": (), "peaks": (), "path": None, "report": ""}

        ttk.Label(
            dialog,
            text="Analyze exported instrument data (not ordinary camera images).",
            wraplength=950,
        ).grid(row=0, column=0, sticky="w", padx=12, pady=(12, 6))

        settings = ttk.Frame(dialog, padding=(12, 4))
        settings.grid(row=1, column=0, sticky="ew")
        settings.columnconfigure(5, weight=1)
        ttk.Label(settings, text="Analysis").grid(row=0, column=0, sticky="w")
        mode_box = ttk.Combobox(
            settings,
            textvariable=mode,
            values=(
                "EDS / X-ray microanalysis",
                "X-ray spectroscopy",
                "Electron diffraction",
            ),
            state="readonly",
            width=28,
        )
        mode_box.grid(row=0, column=1, sticky="w", padx=(5, 14))
        ttk.Label(settings, text="X-axis unit").grid(row=0, column=2, sticky="w")
        unit_box = ttk.Combobox(
            settings,
            textvariable=x_unit,
            values=("keV", "eV", "degrees", "Å", "Å⁻¹", "pixels"),
            state="readonly",
            width=10,
        )
        unit_box.grid(row=0, column=3, sticky="w", padx=(5, 14))
        ttk.Label(settings, text="Peak level (%)").grid(row=0, column=4, sticky="w")
        level_scale = ttk.Scale(
            settings,
            from_=1,
            to=50,
            variable=peak_level,
            command=lambda _value: level_label.configure(
                text=f"{peak_level.get():.0f}%"
            ),
        )
        level_scale.grid(row=0, column=5, sticky="ew", padx=6)
        level_label = ttk.Label(settings, text="5%", width=5)
        level_label.grid(row=0, column=6, sticky="w")

        ttk.Label(settings, text="EDS line tolerance (keV)").grid(
            row=1, column=0, sticky="w", pady=(7, 0)
        )
        tolerance_entry = ttk.Entry(settings, textvariable=tolerance, width=8)
        tolerance_entry.grid(row=1, column=1, sticky="w", padx=(5, 0), pady=(7, 0))
        status = ttk.Label(settings, text="Load a CSV or tab-delimited text file.")
        status.grid(row=1, column=2, columnspan=5, sticky="w", padx=(12, 0), pady=(7, 0))

        plot = tk.Canvas(
            dialog,
            background=self.colors["field"],
            highlightthickness=1,
            highlightbackground=self.colors["border"],
        )
        plot.grid(row=2, column=0, sticky="nsew", padx=12, pady=(8, 6))

        actions = ttk.Frame(dialog, padding=(12, 4))
        actions.grid(row=3, column=0, sticky="ew")
        actions.columnconfigure(2, weight=1)

        results = tk.Text(dialog, height=10, wrap="word", state="disabled")
        self._style_text(results)
        results.grid(row=4, column=0, sticky="nsew", padx=12, pady=(6, 12))

        def set_results(text):
            results.configure(state="normal")
            results.delete("1.0", tk.END)
            results.insert("1.0", text)
            results.configure(state="disabled")
            data["report"] = text

        def draw_plot(_event=None):
            plot.delete("all")
            points = data["points"]
            if not points:
                plot.create_text(
                    20,
                    20,
                    anchor="nw",
                    text="Load a two-column CSV: x value, intensity.",
                    fill=self.colors["muted"],
                )
                return

            width = max(plot.winfo_width(), 1)
            height = max(plot.winfo_height(), 1)
            left, right, top, bottom = 64, width - 18, 18, height - 48
            if right <= left or bottom <= top:
                return
            x_values = [point[0] for point in points]
            y_values = [point[1] for point in points]
            x_min, x_max = min(x_values), max(x_values)
            y_min, y_max = min(0.0, min(y_values)), max(y_values)
            if x_min == x_max:
                x_max = x_min + 1
            if y_min == y_max:
                y_max = y_min + 1

            for step in range(5):
                fraction = step / 4
                y = bottom - fraction * (bottom - top)
                value = y_min + fraction * (y_max - y_min)
                plot.create_line(left, y, right, y, fill=self.colors["border"])
                plot.create_text(
                    left - 8,
                    y,
                    anchor="e",
                    text=f"{value:.3g}",
                    fill=self.colors["muted"],
                )
            coords = []
            for x_value, intensity in points:
                px = left + (x_value - x_min) / (x_max - x_min) * (right - left)
                py = bottom - (intensity - y_min) / (y_max - y_min) * (bottom - top)
                coords.extend((px, py))
            plot.create_line(*coords, fill=self.colors["accent"], width=2)
            plot.create_text(
                left,
                height - 22,
                anchor="w",
                text=f"{x_min:.4g} {x_unit.get()}",
                fill=self.colors["muted"],
            )
            plot.create_text(
                right,
                height - 22,
                anchor="e",
                text=f"{x_max:.4g} {x_unit.get()}",
                fill=self.colors["muted"],
            )
            plot.create_text(
                (left + right) / 2,
                height - 22,
                text=x_unit.get(),
                fill=self.colors["foreground"],
            )
            for x_value, _intensity in data["peaks"][:30]:
                px = left + (x_value - x_min) / (x_max - x_min) * (right - left)
                plot.create_line(
                    px, top, px, bottom, fill=self.colors["warning"], dash=(3, 3)
                )

        def update_mode(_event=None):
            if mode.get() == "Electron diffraction":
                x_unit.set("Å")
            else:
                x_unit.set("keV")
            draw_plot()

        def load_data():
            path = filedialog.askopenfilename(
                parent=dialog,
                title="Open exported spectrum or diffraction profile",
                filetypes=(
                    ("CSV / text data", "*.csv *.txt *.tsv"),
                    ("All files", "*"),
                ),
            )
            if not path:
                return
            try:
                points = load_spectrum_csv(path)
            except (OSError, UnicodeError, ValueError, csv.Error) as error:
                messagebox.showerror("Could not load data", str(error), parent=dialog)
                return
            data["points"] = points
            data["peaks"] = ()
            data["path"] = path
            status.configure(
                text=f"{Path(path).name}: {len(points)} data points"
            )
            set_results(
                "Data loaded. Select the correct x-axis unit, then choose Analyze peaks."
            )
            draw_plot()

        def analyze():
            points = data["points"]
            if not points:
                messagebox.showinfo(
                    "No data loaded", "Load a spectrum/profile CSV first.", parent=dialog
                )
                return
            analysis_mode = mode.get()
            if (
                analysis_mode == "EDS / X-ray microanalysis"
                and x_unit.get() != "keV"
            ):
                messagebox.showerror(
                    "EDS requires keV",
                    "Set the EDS spectrum x-axis to keV before identifying lines.",
                    parent=dialog,
                )
                return
            try:
                peaks = detect_spectrum_peaks(points, peak_level.get())
                line_tolerance = 0.10
                if analysis_mode == "EDS / X-ray microanalysis":
                    line_tolerance = float(tolerance.get())
                    if not math.isfinite(line_tolerance) or line_tolerance <= 0:
                        raise ValueError
            except ValueError:
                messagebox.showerror(
                    "Invalid setting",
                    "Peak level must be between 0 and 100%, and EDS line tolerance "
                    "must be a positive number in keV.",
                    parent=dialog,
                )
                return

            data["peaks"] = peaks
            report_lines = [
                f"File: {Path(data['path']).name}",
                f"Analysis: {analysis_mode}",
                f"Data points: {len(points)}",
                f"Detected peaks: {len(peaks)} (minimum peak level "
                f"{peak_level.get():.0f}% of the data range)",
            ]
            if analysis_mode == "EDS / X-ray microanalysis":
                report_lines.extend(
                    (
                        "",
                        "Possible characteristic X-ray lines near detected peaks:",
                        "Reference energies are approximate; only a limited set of lines "
                        "is included.",
                    )
                )
                matches = 0
                for peak_x, peak_y in peaks:
                    nearby = [
                        (symbol, line, energy)
                        for symbol, line, energy in EDS_REFERENCE_LINES
                        if abs(peak_x - energy) <= line_tolerance
                    ]
                    if nearby:
                        matches += 1
                        labels = ", ".join(
                            f"{symbol} {line} ({energy:.3f} keV)"
                            for symbol, line, energy in nearby
                        )
                    else:
                        labels = "no line in the small built-in reference list"
                    report_lines.append(
                        f"  {peak_x:.4g} keV, intensity {peak_y:.4g}: {labels}"
                    )
                if not peaks:
                    report_lines.append("  No peaks passed the selected peak-level setting.")
                report_lines.extend(
                    (
                        "",
                        f"Matched {matches} peak(s) within ±{line_tolerance:g} keV.",
                        "These are tentative line matches, not a confirmed element list or "
                        "quantitative composition. Peak overlap, detector calibration, "
                        "background, standards, and instrument-specific corrections are "
                        "not modeled.",
                    )
                )
            elif analysis_mode == "Electron diffraction":
                report_lines.extend(("", "Detected profile peak positions:"))
                report_lines.extend(
                    f"  {x_value:.6g} {x_unit.get()} (intensity {intensity:.4g})"
                    for x_value, intensity in peaks
                )
                report_lines.extend(
                    (
                        "",
                        "This reports peaks in the imported one-dimensional profile only. "
                        "It does not identify a crystal phase; that requires calibrated "
                        "camera length/wavelength and reference diffraction data.",
                    )
                )
            else:
                report_lines.extend(("", "Detected spectral peak positions:"))
                report_lines.extend(
                    f"  {x_value:.6g} {x_unit.get()} (intensity {intensity:.4g})"
                    for x_value, intensity in peaks
                )
                report_lines.extend(
                    (
                        "",
                        "This is peak finding on imported data, not a calibrated or "
                        "quantitative X-ray analysis.",
                    )
                )
            set_results("\n".join(report_lines))
            draw_plot()

        def save_results():
            if not data["report"]:
                messagebox.showinfo(
                    "No results", "Analyze the loaded data before saving.", parent=dialog
                )
                return
            path = filedialog.asksaveasfilename(
                parent=dialog,
                title="Save analysis results",
                defaultextension=".txt",
                filetypes=(("Text file", "*.txt"), ("CSV file", "*.csv")),
            )
            if not path:
                return
            try:
                Path(path).write_text(data["report"] + "\n", encoding="utf-8")
            except OSError as error:
                messagebox.showerror(
                    "Save failed", f"Could not save analysis results: {error}", parent=dialog
                )
                return
            status.configure(text=f"Results saved to {Path(path).name}")

        ttk.Button(actions, text="Load CSV…", command=load_data).pack(side="left")
        ttk.Button(actions, text="Analyze peaks", command=analyze).pack(
            side="left", padx=(6, 0)
        )
        ttk.Button(actions, text="Save results…", command=save_results).pack(
            side="left", padx=(6, 0)
        )
        mode_box.bind("<<ComboboxSelected>>", update_mode)
        unit_box.bind("<<ComboboxSelected>>", lambda _event: draw_plot())
        plot.bind("<Configure>", draw_plot)
        draw_plot()

    def _build_controller_panel(self, panel):
        panel.columnconfigure(0, weight=1)
        panel.rowconfigure(3, weight=1)
        self._add_image_header(panel, "USB SERIAL / UART", row=0)
        connection = ttk.Frame(panel)
        connection.grid(row=1, column=0, sticky="ew")
        connection.columnconfigure(1, weight=1)
        ttk.Label(connection, text="Port").grid(row=0, column=0, sticky="w", padx=(0, 6))
        self.port_box = ttk.Combobox(connection, textvariable=self.port, state="readonly")
        self.port_box.grid(row=0, column=1, sticky="ew")
        ttk.Button(connection, text="Scan", command=self._refresh_ports).grid(
            row=0, column=2, padx=(5, 0)
        )
        ttk.Label(connection, text="Baud").grid(row=1, column=0, sticky="w", pady=(7, 0))
        ttk.Entry(connection, textvariable=self.baud, width=10).grid(
            row=1, column=1, sticky="w", pady=(7, 0)
        )
        self.serial_button = ttk.Button(
            connection, text="Connect", command=self._toggle_serial
        )
        self.serial_button.grid(row=1, column=2, padx=(5, 0), pady=(7, 0))
        self.serial_status = ttk.Label(connection, text="Disconnected")
        self.serial_status.grid(row=2, column=0, columnspan=3, sticky="w", pady=(7, 8))

        self.output = tk.Text(panel, height=12, wrap="word", state="disabled")
        self._style_text(self.output)
        self.output.grid(row=3, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(panel, orient="vertical", command=self.output.yview)
        scroll.grid(row=3, column=1, sticky="ns")
        self.output.configure(yscrollcommand=scroll.set)

        command_frame = ttk.Frame(panel)
        command_frame.grid(row=4, column=0, sticky="ew", pady=(8, 0))
        command_frame.columnconfigure(0, weight=1)
        self.command = ttk.Entry(command_frame)
        self.command.grid(row=0, column=0, sticky="ew")
        self.command.bind("<Return>", lambda _event: self._send_command())
        ttk.Button(command_frame, text="Send", command=self._send_command).grid(
            row=0, column=1, padx=(5, 0)
        )
        ttk.Label(command_frame, text="Line ending").grid(
            row=1, column=0, sticky="w", pady=(6, 0)
        )
        ttk.Combobox(
            command_frame,
            textvariable=self.line_ending,
            values=("Newline (\\n)", "CRLF (\\r\\n)", "None"),
            state="readonly",
            width=16,
        ).grid(row=1, column=1, sticky="e", pady=(6, 0))
        ttk.Label(
            panel,
            text="Commands must match the firmware on your controller.",
            wraplength=350,
        ).grid(row=5, column=0, sticky="w", pady=(10, 0))

    def _build_controls_panel(self, panel):
        panel.columnconfigure(0, weight=1)
        panel.rowconfigure(3, weight=1)
        self._add_image_header(panel, "HARDWARE CONTROLS", row=0)
        ttk.Label(
            panel,
            text=(
                "Buttons send fixed commands. Sliders substitute {value}. Configure "
                "commands to match your controller firmware."
            ),
            wraplength=360,
            justify="left",
        ).grid(row=1, column=0, sticky="w", pady=(5, 10))

        controls_header = ttk.Frame(panel)
        controls_header.grid(row=2, column=0, sticky="new")
        controls_header.columnconfigure(0, weight=1)
        ttk.Label(controls_header, text="Your controls", font=("", 10, "bold")).grid(
            row=0, column=0, sticky="w"
        )
        ttk.Button(controls_header, text="Add control…", command=self._edit_control).grid(
            row=0, column=1, sticky="e"
        )
        self.controls_frame = ttk.Frame(panel)
        self.controls_frame.grid(row=3, column=0, sticky="new", pady=(8, 0))
        self.controls_frame.columnconfigure(0, weight=1)
        self._load_controls()
        self._render_controls()

        ttk.Label(
            panel,
            text="Connect below before using these controls.",
            wraplength=360,
        ).grid(row=4, column=0, sticky="w", pady=(12, 0))

    def _add_image_header(self, panel, title, row):
        header = tk.Canvas(
            panel,
            height=64,
            background=self.colors["surface"],
            highlightthickness=1,
            highlightbackground=self.colors["border"],
            borderwidth=0,
        )
        header.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        header.bind(
            "<Configure>",
            lambda event, target=header, label=title: self._render_image_header(
                target, label, event.width, event.height
            ),
        )
        self._render_image_header(header, title, 360, 64)

    def _create_tool_slider(
        self,
        parent,
        row,
        title,
        options,
        variable,
        initial,
        on_change,
        selection_attribute,
        scale_attribute,
    ):
        panel = ttk.Frame(parent)
        panel.grid(row=row, column=0, sticky="ew", pady=(3, 5))
        panel.columnconfigure(0, weight=1)

        selected = initial if initial in options else options[0]
        selected_index = options.index(selected)
        variable.set(selected_index)

        title_row = ttk.Frame(panel)
        title_row.grid(row=0, column=0, sticky="ew")
        title_row.columnconfigure(1, weight=1)
        ttk.Label(title_row, text=title).grid(row=0, column=0, sticky="w")
        selection_label = ttk.Label(
            title_row,
            text=selected,
            foreground=self.colors["accent"],
            font=("", 10, "bold"),
        )
        selection_label.grid(row=0, column=1, sticky="e")
        setattr(self, selection_attribute, selection_label)

        scale = ttk.Scale(
            panel,
            from_=0,
            to=len(options) - 1,
            variable=variable,
            command=lambda raw: self._select_slider_option(
                raw, options, variable, on_change, selection_label
            ),
        )
        scale.grid(row=1, column=0, sticky="ew", pady=(3, 0))
        setattr(self, scale_attribute, scale)

        labels = ttk.Frame(panel)
        labels.grid(row=2, column=0, sticky="ew")
        for index, option in enumerate(options):
            labels.columnconfigure(index, weight=1, uniform=f"{title}-options")
            ttk.Label(
                labels,
                text=option,
                foreground=self.colors["muted"],
                font=("", 8),
                anchor=("w" if index == 0 else "e" if index == len(options) - 1 else "center"),
            ).grid(row=0, column=index, sticky="ew")

    def _select_slider_option(self, raw_value, options, variable, on_change, label):
        index = min(max(round(float(raw_value)), 0), len(options) - 1)
        if variable.get() != index:
            variable.set(index)
        selected = options[index]
        changed = label.cget("text") != selected
        label.configure(text=selected)
        if changed:
            on_change(selected)

    def _render_image_header(self, canvas, title, width, height):
        canvas.delete("header-art")
        if self.background_image_path.is_file():
            if Image is not None:
                if self.header_source is None:
                    with Image.open(self.background_image_path) as source:
                        self.header_source = source.convert("RGB")
                image = self.header_source.copy()
                scale = max(width / image.width, height / image.height)
                image = image.resize(
                    (max(1, int(image.width * scale)), max(1, int(image.height * scale))),
                    Image.Resampling.LANCZOS,
                )
                left = max(0, (image.width - width) // 2)
                top = max(0, (image.height - height) // 2)
                image = image.crop((left, top, left + width, top + height)                )
                photo = self._to_tk_photo(image)
                canvas.header_photo = photo
                canvas.create_image(
                    0, 0, image=photo, anchor="nw", tags="header-art"
                )
            else:
                if self.header_native_photo is None:
                    self.header_native_photo = tk.PhotoImage(
                        master=self.root, file=str(self.background_image_path)
                    )
                factor = max(
                    1,
                    (self.header_native_photo.width() + width - 1) // width,
                    (self.header_native_photo.height() + height - 1) // height,
                )
                photo = self.header_native_photo.subsample(factor, factor)
                canvas.header_photo = photo
                image_x = width // 2 - 950 // factor
                image_y = height // 2 - 330 // factor
                canvas.create_image(
                    image_x,
                    image_y,
                    image=photo,
                    anchor="nw",
                    tags="header-art",
                )
        canvas.create_rectangle(
            0,
            0,
            width,
            height,
            fill=self.colors["field"],
            stipple="gray50",
            outline="",
            tags="header-art",
        )
        canvas.create_text(
            14,
            height // 2,
            text=title,
            fill="#f2f6fb",
            font=("Sans", 11, "bold"),
            anchor="w",
            tags="header-art",
        )

    def _load_controls(self):
        try:
            data = json.loads(self.control_config_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self.controls = []
            return
        except (OSError, json.JSONDecodeError) as error:
            messagebox.showerror(
                "Could not load controls",
                f"Could not read {self.control_config_path}:\n{error}",
            )
            self.controls = []
            return

        if not isinstance(data, list):
            messagebox.showerror(
                "Invalid control settings",
                f"{self.control_config_path} must contain a list of controls.",
            )
            self.controls = []
            return

        valid = []
        for item in data:
            if (
                isinstance(item, dict)
                and isinstance(item.get("name"), str)
                and item.get("kind") in ("Button", "Slider")
                and isinstance(item.get("command"), str)
                and (item["kind"] == "Button" or "{value}" in item["command"])
                and (
                    item["kind"] == "Button"
                    or (
                        isinstance(item.get("minimum"), int)
                        and isinstance(item.get("maximum"), int)
                        and item["minimum"] < item["maximum"]
                    )
                )
            ):
                valid.append(item)
        if len(valid) != len(data):
            messagebox.showerror(
                "Invalid control settings",
                "One or more saved controls are invalid. They were not loaded; "
                "your settings file has been left unchanged.",
            )
            self.controls = []
            return
        self.controls = valid

    def _save_controls(self, controls):
        try:
            self.control_config_path.write_text(
                json.dumps(controls, indent=2) + "\n", encoding="utf-8"
            )
        except OSError as error:
            messagebox.showerror(
                "Could not save controls",
                f"Could not write {self.control_config_path}:\n{error}",
            )
            return False
        self.controls = controls
        self._render_controls()
        return True

    def _edit_control(self, index=None):
        editing = index is not None
        current = self.controls[index] if editing else None
        dialog = tk.Toplevel(self.root)
        dialog.title("Edit hardware control" if editing else "Add hardware control")
        dialog.transient(self.root)
        dialog.configure(background=self.colors["background"])
        dialog.grab_set()
        dialog.columnconfigure(1, weight=1)

        name = tk.StringVar(value=current["name"] if current else "")
        kind = tk.StringVar(value=current["kind"] if current else "Slider")
        command = tk.StringVar(value=current["command"] if current else "")
        minimum = tk.StringVar(value=str(current.get("minimum", 0)) if current else "0")
        maximum = tk.StringVar(value=str(current.get("maximum", 255)) if current else "255")

        ttk.Label(dialog, text="Control name").grid(
            row=0, column=0, sticky="w", padx=10, pady=(10, 5)
        )
        ttk.Entry(dialog, textvariable=name, width=28).grid(
            row=0, column=1, sticky="ew", padx=(5, 10), pady=(10, 5)
        )
        ttk.Label(dialog, text="Type").grid(row=1, column=0, sticky="w", padx=10, pady=5)
        kind_box = ttk.Combobox(
            dialog, textvariable=kind, values=("Button", "Slider"), state="readonly"
        )
        kind_box.grid(row=1, column=1, sticky="ew", padx=(5, 10), pady=5)
        ttk.Label(dialog, text="Serial command").grid(
            row=2, column=0, sticky="w", padx=10, pady=5
        )
        command_entry = ttk.Entry(dialog, textvariable=command, width=28)
        command_entry.grid(row=2, column=1, sticky="ew", padx=(5, 10), pady=5)
        range_frame = ttk.Frame(dialog)
        range_frame.grid(row=3, column=0, columnspan=2, sticky="ew", padx=10, pady=5)
        ttk.Label(range_frame, text="Slider min").pack(side="left")
        ttk.Entry(range_frame, textvariable=minimum, width=7).pack(side="left", padx=(5, 12))
        ttk.Label(range_frame, text="max").pack(side="left")
        ttk.Entry(range_frame, textvariable=maximum, width=7).pack(side="left", padx=5)
        hint = ttk.Label(
            dialog,
            text='For a slider, include {value} in the command, e.g. "LED {value}".',
            wraplength=360,
        )
        hint.grid(row=4, column=0, columnspan=2, sticky="w", padx=10, pady=(2, 8))

        def update_kind(_event=None):
            is_slider = kind.get() == "Slider"
            for child in range_frame.winfo_children():
                child.configure(state="normal" if is_slider else "disabled")
            hint.configure(
                text=(
                    'For a slider, include {value} in the command, e.g. "LED {value}".'
                    if is_slider
                    else "A button sends the command exactly as entered."
                )
            )

        kind_box.bind("<<ComboboxSelected>>", update_kind)
        update_kind()

        actions = ttk.Frame(dialog)
        actions.grid(row=5, column=0, columnspan=2, sticky="e", padx=10, pady=(3, 10))

        def save():
            label = name.get().strip()
            command_text = command.get().strip()
            if not label or not command_text:
                messagebox.showerror(
                    "Missing information", "Enter both a control name and a serial command.",
                    parent=dialog,
                )
                return
            item = {"name": label, "kind": kind.get(), "command": command_text}
            if kind.get() == "Slider":
                try:
                    low = int(minimum.get())
                    high = int(maximum.get())
                    if low >= high:
                        raise ValueError
                except ValueError:
                    messagebox.showerror(
                        "Invalid range",
                        "Slider minimum and maximum must be integers, with minimum less than maximum.",
                        parent=dialog,
                    )
                    return
                if "{value}" not in command_text:
                    messagebox.showerror(
                        "Missing value placeholder",
                        'Slider commands must contain the placeholder "{value}".',
                        parent=dialog,
                    )
                    return
                item["minimum"] = low
                item["maximum"] = high

            updated = list(self.controls)
            if editing:
                updated[index] = item
            else:
                updated.append(item)
            if self._save_controls(updated):
                dialog.destroy()

        ttk.Button(actions, text="Cancel", command=dialog.destroy).pack(side="right")
        ttk.Button(actions, text="Save control", command=save).pack(
            side="right", padx=(0, 6)
        )
        dialog.bind("<Return>", lambda _event: save())
        dialog.bind("<Escape>", lambda _event: dialog.destroy())

    def _render_controls(self):
        if not hasattr(self, "controls_frame"):
            return
        for child in self.controls_frame.winfo_children():
            child.destroy()
        if not self.controls:
            ttk.Label(
                self.controls_frame,
                text="No controls configured yet. Add one and enter a command supported by your firmware.",
                wraplength=360,
            ).grid(row=0, column=0, sticky="w")
            return
        for index, control in enumerate(self.controls):
            row = ttk.LabelFrame(self.controls_frame, text=control["name"], padding=7)
            row.grid(row=index, column=0, sticky="ew", pady=(0, 7))
            row.columnconfigure(0, weight=1)
            if control["kind"] == "Button":
                ttk.Button(
                    row,
                    text="Send command",
                    command=lambda item=control: self._send_control(item["command"]),
                ).grid(row=0, column=0, sticky="ew")
            else:
                value = tk.DoubleVar(value=control["minimum"])
                value_label = ttk.Label(row, text=str(value.get()), width=5)
                value_label.grid(row=0, column=1, sticky="e")
                slider = ttk.Scale(
                    row,
                    from_=control["minimum"],
                    to=control["maximum"],
                    variable=value,
                    command=lambda raw, label=value_label: label.configure(
                        text=str(round(float(raw)))
                    ),
                )
                slider.grid(row=0, column=0, sticky="ew", padx=(0, 5))
                ttk.Button(
                    row,
                    text="Send value",
                    command=lambda item=control, val=value: self._send_control(
                        item["command"].replace("{value}", str(round(val.get())))
                    ),
                ).grid(row=1, column=0, columnspan=2, sticky="ew", pady=(5, 0))
            actions = ttk.Frame(row)
            actions.grid(row=2, column=0, columnspan=2, sticky="e", pady=(5, 0))
            ttk.Button(
                actions,
                text="Edit",
                command=lambda selected=index: self._edit_control(selected),
            ).pack(side="left")
            ttk.Button(
                actions,
                text="Remove",
                command=lambda selected=index: self._remove_control(selected),
            ).pack(side="left", padx=(5, 0))

    def _remove_control(self, index):
        control = self.controls[index]
        if not messagebox.askyesno(
            "Remove control", f"Remove '{control['name']}' from the saved controls?"
        ):
            return
        updated = list(self.controls)
        updated.pop(index)
        self._save_controls(updated)

    def _draw_placeholder(self):
        self.preview.delete("all")
        self.image_item = None
        self.transform = None
        width = max(self.preview.winfo_width(), 320)
        height = max(self.preview.winfo_height(), 240)
        self.preview.configure(background=self.colors["field"])
        if self.background_image_path.is_file():
            if Image is not None:
                with Image.open(self.background_image_path) as source:
                    image = source.convert("RGB")
                scale = min(width / image.width, height / image.height)
                image = image.resize(
                    (max(1, int(image.width * scale)), max(1, int(image.height * scale))),
                    Image.Resampling.LANCZOS,
                )
                self.placeholder_photo = self._to_tk_photo(image)
            else:
                if self.background_native_photo is None:
                    self.background_native_photo = tk.PhotoImage(
                        master=self.root, file=str(self.background_image_path)
                    )
                factor = max(
                    1,
                    (self.background_native_photo.width() + width - 1) // width,
                    (self.background_native_photo.height() + height - 1) // height,
                )
                self.placeholder_photo = self.background_native_photo.subsample(factor, factor)
            self.preview.create_image(
                width // 2,
                height // 2,
                image=self.placeholder_photo,
                anchor="center",
                tags="placeholder",
            )
        title_x = 18
        title_y = max(25, height - 42)
        self.preview.create_text(
            title_x + 1,
            title_y + 1,
            text="Connect a camera and press Start camera",
            fill="#07111d",
            font=("Sans", 10, "bold"),
            anchor="w",
            tags="placeholder",
        )
        self.preview.create_text(
            title_x,
            title_y,
            text="Connect a camera and press Start camera",
            fill="#e6f4fb",
            font=("Sans", 10, "bold"),
            anchor="w",
            tags="placeholder",
        )

    def _check_dependencies(self):
        if cv2 is None:
            self._log_analysis("OpenCV is missing; install requirements to use camera analysis.")
            self.camera_button.configure(state="disabled")
            self.freeze_button.configure(state="disabled")
            self.processing_scale.configure(state="disabled")
            self.measure_tool_scale.configure(state="disabled")
            self.particle_button.configure(state="disabled")
        elif Image is None:
            self._log_analysis("Pillow is missing; install requirements to display camera frames.")
            self.camera_button.configure(state="disabled")
            self.freeze_button.configure(state="disabled")
        if Image is None or np is None:
            missing = "Pillow" if Image is None else "NumPy"
            self._log_analysis(f"{missing} is missing; install requirements to import images.")
            self.import_image_button.configure(state="disabled")
        if serial is None:
            self._log_serial("pyserial is missing; install requirements to use UART/USB serial.")
            self.serial_button.configure(state="disabled")

    def _open_image(self):
        path = filedialog.askopenfilename(
            title="Open image for viewing and analysis",
            filetypes=(
                ("Image files", "*.png *.jpg *.jpeg *.bmp *.tif *.tiff *.webp *.gif"),
                ("All files", "*"),
            ),
        )
        if not path:
            return
        try:
            with Image.open(path) as source:
                image = source.convert("RGB")
                frame = np.array(image)
        except (OSError, ValueError) as error:
            messagebox.showerror("Could not open image", f"Could not read the selected image:\n{error}")
            return

        self.imported_frame = frame
        self.imported_image_path = Path(path)
        self.measurement = None
        self.pixels_per_unit = None
        self.frame_frozen = False
        self.frozen_frame = None
        self.freeze_button.configure(text="Freeze frame", state="disabled")
        if self.camera_thread and self.camera_thread.is_alive():
            self.camera_stop.set()
            self.camera_status.configure(text="Stopping camera to open image…")
        else:
            self.latest_frame = None
            while True:
                try:
                    self.camera_frames.get_nowait()
                except queue.Empty:
                    break
            self.camera_status.configure(text=f"Image: {self.imported_image_path.name}")
        height, width = frame.shape[:2]
        self._log_analysis(
            f"Opened image: {self.imported_image_path.name} ({width} × {height} px). "
            "Calibrate scale again before taking physical measurements."
        )
        self._render_frame()

    def _refresh_ports(self):
        if list_ports is None:
            self.port_box.configure(values=())
            return
        values = [item.device for item in list_ports.comports()]
        self.port_box.configure(values=values)
        if values and self.port.get() not in values:
            self.port.set(values[0])
        elif not values:
            self.port.set("")

    def _toggle_camera(self):
        if self.camera_thread and self.camera_thread.is_alive():
            self.camera_stop.set()
            self.camera_button.configure(state="disabled")
            return
        try:
            index = int(self.camera_index.get())
        except ValueError:
            messagebox.showerror("Invalid camera", "Camera index must be a whole number.")
            return
        self.camera_stop.clear()
        self.camera_button.configure(text="Stop camera")
        self.camera_status.configure(text="Starting…")
        self.camera_thread = threading.Thread(
            target=self._camera_worker, args=(index,), daemon=True
        )
        self.camera_thread.start()

    def _camera_worker(self, index):
        capture = cv2.VideoCapture(index)
        try:
            if not capture.isOpened():
                self.camera_events.put(("error", f"Could not open camera index {index}."))
                return
            self.camera_events.put(("status", "Camera running"))
            while not self.camera_stop.is_set():
                ok, frame = capture.read()
                if not ok:
                    self.camera_events.put(("error", "Camera stopped sending frames."))
                    break
                frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                if self.camera_frames.full():
                    try:
                        self.camera_frames.get_nowait()
                    except queue.Empty:
                        pass
                self.camera_frames.put(frame)
        except cv2.error as error:
            self.camera_events.put(("error", f"Camera error: {error}"))
        finally:
            capture.release()
            self.camera_events.put(("stopped", "Camera stopped"))

    def _toggle_freeze(self):
        if self.frame_frozen:
            self.frozen_frame = None
            self.frame_frozen = False
            self.freeze_button.configure(text="Freeze frame")
            camera_running = self.camera_thread and self.camera_thread.is_alive()
            self.camera_status.configure(
                text="Live camera" if camera_running else "Camera stopped"
            )
            if not camera_running:
                self.freeze_button.configure(state="disabled")
        elif self.latest_frame is not None:
            self.frozen_frame = self.latest_frame.copy()
            self.frame_frozen = True
            self.freeze_button.configure(text="Resume live")
            self.camera_status.configure(text="Frame frozen")
        self._render_frame()

    def _active_frame(self):
        if self.frame_frozen and self.frozen_frame is not None:
            return self.frozen_frame
        if self.latest_frame is not None:
            return self.latest_frame
        return self.imported_frame

    def _processed_frame(self, frame):
        if cv2 is None:
            return frame
        gray = cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY)
        mode = self.processing.get()
        if mode == "Grayscale":
            return cv2.cvtColor(gray, cv2.COLOR_GRAY2RGB)
        if mode == "Edges":
            edges = cv2.Canny(gray, 60, 150)
            return cv2.cvtColor(edges, cv2.COLOR_GRAY2RGB)
        if mode == "Threshold":
            _, binary = cv2.threshold(
                gray, int(round(self.threshold.get())), 255, cv2.THRESH_BINARY
            )
            return cv2.cvtColor(binary, cv2.COLOR_GRAY2RGB)
        if mode == "Intensity map":
            mapped = cv2.applyColorMap(gray, cv2.COLORMAP_TURBO)
            return cv2.cvtColor(mapped, cv2.COLOR_BGR2RGB)
        return frame

    def _set_processing(self, mode):
        self.processing.set(mode)
        self._render_frame()

    def _set_measure_tool(self, tool):
        self.measure_tool.set(tool)

    def _render_frame(self):
        self.zoom_label.configure(text=f"{self.zoom.get():.1f}×")
        threshold = int(round(self.threshold.get()))
        self.threshold_label.configure(text=str(threshold))
        frame = self._active_frame()
        if frame is None or Image is None:
            if frame is None:
                self._draw_placeholder()
            return
        canvas_width = max(self.preview.winfo_width(), 1)
        canvas_height = max(self.preview.winfo_height(), 1)
        frame_height, frame_width = frame.shape[:2]
        base_scale = min(canvas_width / frame_width, canvas_height / frame_height)
        scale = base_scale * max(1.0, float(self.zoom.get()))
        crop_width = min(frame_width, max(1, int(canvas_width / scale)))
        crop_height = min(frame_height, max(1, int(canvas_height / scale)))
        crop_x = max(0, (frame_width - crop_width) // 2)
        crop_y = max(0, (frame_height - crop_height) // 2)
        processed = self._processed_frame(frame)
        crop = processed[crop_y : crop_y + crop_height, crop_x : crop_x + crop_width]
        display_width = max(1, int(crop_width * scale))
        display_height = max(1, int(crop_height * scale))
        image = Image.fromarray(crop).resize((display_width, display_height), Image.Resampling.BILINEAR)
        self.photo = self._to_tk_photo(image)
        offset_x = (canvas_width - display_width) / 2 - crop_x * scale
        offset_y = (canvas_height - display_height) / 2 - crop_y * scale
        self.transform = (scale, offset_x, offset_y, frame_width, frame_height)
        if self.image_item is None:
            self.image_item = self.preview.create_image(
                offset_x + crop_x * scale,
                offset_y + crop_y * scale,
                image=self.photo,
                anchor="nw",
            )
        else:
            self.preview.coords(
                self.image_item,
                offset_x + crop_x * scale,
                offset_y + crop_y * scale,
            )
            self.preview.itemconfigure(self.image_item, image=self.photo)
        self.preview.delete("placeholder")
        self._draw_selection()

    def _canvas_to_image(self, x, y):
        if self.transform is None:
            return None
        scale, offset_x, offset_y, width, height = self.transform
        image_x = (x - offset_x) / scale
        image_y = (y - offset_y) / scale
        if not (0 <= image_x < width and 0 <= image_y < height):
            return None
        return image_x, image_y

    def _draw_selection(self):
        self.preview.delete("selection")
        if self.transform is None:
            return
        points = self.drag_current if self.drag_start is not None else self.measurement
        if points is None:
            return
        scale, offset_x, offset_y, _width, _height = self.transform
        x1 = points[0][0] * scale + offset_x
        y1 = points[0][1] * scale + offset_y
        x2 = points[1][0] * scale + offset_x
        y2 = points[1][1] * scale + offset_y
        tool = self.measure_tool.get()
        if tool == "Rectangle area":
            self.preview.create_rectangle(
                x1, y1, x2, y2, outline="#00ff7f", width=2, tags="selection"
            )
        else:
            self.preview.create_line(
                x1, y1, x2, y2, fill="#ff5555", width=2, tags="selection"
            )

    def _begin_measurement(self, event):
        if self.measure_tool.get() == "Pan":
            return
        point = self._canvas_to_image(event.x, event.y)
        if point is None:
            return
        self.drag_start = point
        self.drag_current = point
        self._draw_selection()

    def _update_measurement(self, event):
        if self.drag_start is None:
            return
        point = self._canvas_to_image(event.x, event.y)
        if point is None and self.transform is not None:
            scale, offset_x, offset_y, width, height = self.transform
            point = (
                min(max((event.x - offset_x) / scale, 0), width - 1),
                min(max((event.y - offset_y) / scale, 0), height - 1),
            )
        if point is not None:
            self.drag_current = point
            self._draw_selection()

    def _finish_measurement(self, event):
        if self.drag_start is None:
            return
        self._update_measurement(event)
        if self.drag_current is None:
            self.drag_start = None
            return
        start, end = self.drag_start, self.drag_current
        self.drag_start = None
        self.drag_current = None
        distance_px = math.hypot(end[0] - start[0], end[1] - start[1])
        tool = self.measure_tool.get()
        if distance_px < 1:
            self._draw_selection()
            return
        if tool == "Calibrate scale":
            self._calibrate(distance_px)
        elif tool == "Length":
            self.measurement = (start, end)
            self._report_length(distance_px)
        elif tool == "Rectangle area":
            self.measurement = (start, end)
            self._report_area(start, end)
        elif tool == "Surface profile":
            self.measurement = (start, end)
            self._report_profile(start, end)
        self._draw_selection()

    def _calibrate(self, distance_px):
        try:
            known_distance = float(self.known_distance.get())
            if known_distance <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("Invalid calibration", "Enter a known distance greater than zero.")
            return
        self.pixels_per_unit = distance_px / known_distance
        self._log_analysis(
            f"Scale set: {self.pixels_per_unit:.4g} pixels/{self.distance_unit.get()}. "
            "Calibration applies only at this camera zoom and optical setup."
        )

    def _format_length(self, distance_px):
        if self.pixels_per_unit is None:
            return f"{distance_px:.2f} px (not calibrated)"
        value = distance_px / self.pixels_per_unit
        return f"{value:.4g} {self.distance_unit.get()} ({distance_px:.2f} px)"

    def _report_length(self, distance_px):
        self._log_analysis(f"Length: {self._format_length(distance_px)}")

    def _report_area(self, start, end):
        width = abs(end[0] - start[0])
        height = abs(end[1] - start[1])
        area_px = width * height
        if self.pixels_per_unit is None:
            area = f"{area_px:.2f} px² (not calibrated)"
        else:
            unit = self.distance_unit.get()
            area = f"{area_px / self.pixels_per_unit**2:.4g} {unit}²"
        self._log_analysis(
            f"Rectangle: {self._format_length(width)} × {self._format_length(height)}; "
            f"area {area}."
        )

    def _report_profile(self, start, end):
        frame = self._active_frame()
        if frame is None:
            return
        gray = cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY)
        x0, y0 = (int(round(value)) for value in start)
        x1, y1 = (int(round(value)) for value in end)
        steps = max(abs(x1 - x0), abs(y1 - y0), 1)
        values = []
        for index in range(steps + 1):
            fraction = index / steps
            x = min(max(round(x0 + (x1 - x0) * fraction), 0), gray.shape[1] - 1)
            y = min(max(round(y0 + (y1 - y0) * fraction), 0), gray.shape[0] - 1)
            values.append(int(gray[y, x]))
        mean = sum(values) / len(values)
        ra = sum(abs(value - mean) for value in values) / len(values)
        self._log_analysis(
            f"Brightness profile: {len(values)} samples; mean {mean:.1f}/255, "
            f"average absolute deviation {ra:.2f}/255. This is image intensity, "
            "not physical surface roughness."
        )

    def _count_particles(self):
        frame = self._active_frame()
        if frame is None:
            messagebox.showinfo("No image", "Start the camera before counting particles.")
            return
        try:
            min_area = int(self.min_particle_area.get())
            if min_area < 1:
                raise ValueError
        except ValueError:
            messagebox.showerror("Invalid particle area", "Minimum area must be a positive integer.")
            return

        threshold = int(round(self.threshold.get()))
        try:
            count, area, mean_diameter = analyze_particles(
                frame, threshold, min_area, self.invert_particles.get()
            )
        except (RuntimeError, cv2.error) as error:
            messagebox.showerror("Particle analysis failed", str(error))
            return
        if not count:
            self._log_analysis(
                f"Particle count: 0 (threshold {threshold}, minimum "
                f"{min_area} px). Adjust threshold/polarity or minimum area."
            )
            return
        if self.pixels_per_unit is None:
            diameter_result = f"mean equivalent diameter {mean_diameter:.2f} px (not calibrated)"
        else:
            diameter_result = (
                f"mean equivalent diameter "
                f"{mean_diameter / self.pixels_per_unit:.4g} {self.distance_unit.get()}"
            )
        self._log_analysis(
            f"Particle count: {count}; {diameter_result}; "
            f"segmented area {area} px². Touching particles may merge; "
            "adjust threshold and minimum area for your sample."
        )

    def _save_image(self):
        frame = self._active_frame()
        if frame is None:
            messagebox.showinfo("No image", "Start the camera before saving an image.")
            return
        path = filedialog.asksaveasfilename(
            title="Save microscope image",
            defaultextension=".png",
            filetypes=(("PNG image", "*.png"), ("JPEG image", "*.jpg")),
        )
        if not path:
            return
        try:
            Image.fromarray(self._processed_frame(frame)).save(path)
        except (OSError, ValueError) as error:
            messagebox.showerror("Save failed", f"Could not save image: {error}")
            return
        self._log_analysis(f"Saved processed image: {path}")

    def _clear_measurement(self):
        self.measurement = None
        self._draw_selection()

    def _toggle_serial(self):
        if self.serial_thread and self.serial_thread.is_alive():
            self.serial_stop.set()
            self.serial_connected = False
            self.serial_status.configure(text="Disconnecting…")
            self.serial_button.configure(state="disabled")
            return
        selected_port = self.port.get()
        if not selected_port:
            messagebox.showerror("No serial port", "Select a connected serial port first.")
            return
        try:
            baud = int(self.baud.get())
            if baud <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("Invalid baud rate", "Baud rate must be a positive number.")
            return
        while True:
            try:
                self.serial_writes.get_nowait()
            except queue.Empty:
                break
        self.serial_stop.clear()
        self.serial_connected = False
        self.serial_button.configure(text="Disconnect")
        self.serial_status.configure(text="Connecting…")
        self.serial_thread = threading.Thread(
            target=self._serial_worker, args=(selected_port, baud), daemon=True
        )
        self.serial_thread.start()

    def _serial_worker(self, port, baud):
        try:
            connection = serial.Serial(port, baudrate=baud, timeout=0.1, write_timeout=1)
        except (serial.SerialException, OSError) as error:
            self.serial_events.put(("error", f"Could not open {port}: {error}"))
            self.serial_events.put(("stopped", "Disconnected"))
            return
        self.serial_events.put(("status", f"Connected: {port} at {baud} baud"))
        try:
            while not self.serial_stop.is_set():
                try:
                    outgoing = self.serial_writes.get(timeout=0.02)
                except queue.Empty:
                    outgoing = None
                if outgoing is not None:
                    connection.write(outgoing)
                incoming = connection.read(connection.in_waiting or 1)
                if incoming:
                    self.serial_events.put(("data", incoming.decode("utf-8", errors="replace")))
        except (serial.SerialException, OSError) as error:
            if not self.serial_stop.is_set():
                self.serial_events.put(("error", f"Serial connection failed: {error}"))
        finally:
            connection.close()
            self.serial_events.put(("stopped", "Disconnected"))

    def _send_command(self):
        command = self.command.get()
        if not command:
            return
        if self._send_control(command):
            self.command.delete(0, tk.END)

    def _send_control(self, command):
        if not self.serial_connected:
            messagebox.showinfo("Not connected", "Connect to the controller before sending.")
            return False
        endings = {
            "Newline (\\n)": "\n",
            "CRLF (\\r\\n)": "\r\n",
            "None": "",
        }
        payload = (command + endings[self.line_ending.get()]).encode("utf-8")
        self.serial_writes.put(payload)
        self._log_serial(f"> {command}")
        return True

    def _process_events(self):
        while True:
            try:
                kind, value = self.camera_events.get_nowait()
            except queue.Empty:
                break
            if kind == "status":
                self.camera_status.configure(text=value)
                self.freeze_button.configure(state="normal")
            elif kind == "error":
                self._log_analysis(value)
                messagebox.showerror("Camera error", value)
            elif kind == "stopped":
                self.camera_status.configure(
                    text=(
                        f"Image: {self.imported_image_path.name}"
                        if self.imported_frame is not None
                        else value
                    )
                )
                self.camera_button.configure(text="Start camera", state="normal")
                self.freeze_button.configure(text="Freeze frame", state="disabled")
                self.frame_frozen = False
                self.frozen_frame = None
                self.latest_frame = None
                while True:
                    try:
                        self.camera_frames.get_nowait()
                    except queue.Empty:
                        break
                self._render_frame()

        while True:
            try:
                kind, value = self.serial_events.get_nowait()
            except queue.Empty:
                break
            if kind == "status":
                self.serial_connected = True
                self.serial_status.configure(text=value)
            elif kind == "data":
                self._log_serial(value.rstrip())
            elif kind == "error":
                self.serial_connected = False
                self._log_serial(value)
                messagebox.showerror("Serial error", value)
            elif kind == "stopped":
                self.serial_connected = False
                self.serial_status.configure(text=value)
                self.serial_button.configure(text="Connect", state="normal")

        new_frame = False
        try:
            self.latest_frame = self.camera_frames.get_nowait()
            new_frame = True
        except queue.Empty:
            pass
        if new_frame and self.latest_frame is not None and not self.frame_frozen:
            self._render_frame()
        self.root.after(30, self._process_events)

    def _log_analysis(self, text):
        self.analysis_output.configure(state="normal")
        self.analysis_output.insert(tk.END, text + "\n")
        self.analysis_output.see(tk.END)
        self.analysis_output.configure(state="disabled")

    def _style_text(self, widget):
        widget.configure(
            background=self.colors["field"],
            foreground=self.colors["foreground"],
            insertbackground=self.colors["foreground"],
            selectbackground=self.colors["selection"],
            selectforeground=self.colors["foreground"],
            borderwidth=1,
            relief="solid",
            highlightthickness=1,
            highlightbackground=self.colors["border"],
            highlightcolor=self.colors["accent"],
        )

    def _to_tk_photo(self, image):
        if ImageTk is not None:
            return ImageTk.PhotoImage(image)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return tk.PhotoImage(master=self.root, data=buffer.getvalue(), format="png")

    def _log_serial(self, text):
        self.output.configure(state="normal")
        self.output.insert(tk.END, text + "\n")
        self.output.see(tk.END)
        self.output.configure(state="disabled")

    def _close(self):
        self.camera_stop.set()
        self.serial_stop.set()
        self.root.destroy()


def main():
    root = tk.Tk()
    MicroscopeApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
