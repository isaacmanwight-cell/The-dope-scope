# DIY Microscope App

An Ubuntu desktop app for viewing a USB/UVC camera, exploring image-processing views,
making basic calibrated 2D measurements, counting segmented particles, and exchanging
text commands with a microscope controller over USB serial/UART.

## Run on Ubuntu

The application uses a dark interface for the live preview, image tools, hardware
controls, and serial console, arranged together on one dashboard without tabs.
The anime-scientist illustration appears in each main panel's header and in the
viewing box as a placeholder until a camera frame arrives; the live feed replaces
the placeholder, and stopping the camera restores it. Keep `microscope_background.png`
beside `microscope_app.py`.

```bash
sudo apt update
sudo apt install python3-tk python3-venv
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install opencv-python Pillow numpy pyserial
python3 microscope_app.py
```

If Ubuntu reports that the serial port cannot be opened, your account may need serial
device access. On Ubuntu, add your account to the `dialout` group and then sign out
and back in:

```bash
sudo usermod -aG dialout "$USER"
```

## Hardware connections

- **Camera:** A UVC-compatible USB camera can provide the live image. Select its
  camera index (usually `0`; other cameras may be `1`, `2`, etc.).
- **Controller board choices:** Arduino Uno/Nano, ESP32, and Raspberry Pi Pico can
  act as microcontroller boards for LED control, switches, and stepper drivers,
  provided you write firmware that implements the commands configured in this
  app. ESP32 GPIO uses 3.3 V logic; check electrical compatibility before
  connecting signals. A Raspberry Pi 4/5 is a small Linux computer rather than a
  microcontroller; you can run the app on Ubuntu and use a Pi as an additional
  controller/computer, but it is usually simplest to connect a microcontroller
  directly to the Ubuntu computer over USB serial.
- **Controller:** Select the serial port shown after pressing **Scan**, choose the
  baud rate expected by your controller firmware, then connect. Type a command and
  press **Send**. The app can append a newline, CRLF, or no line ending. Received
  text is shown in the console.
- **Hardware controls:** Choose **Add control** in the Hardware controls panel.
  A button sends one fixed command. A slider substitutes its current integer value
  for `{value}` in a command template (for example `LED {value}`), then sends it
  when you press **Send value**. Set names, ranges, and commands to match your own
  firmware. Controls are saved in `microscope_controls.json` beside the app and
  can be edited or removed later. No default hardware commands are assumed.
- **Live viewing and zoom:** Start the camera in the viewing box and use the
  zoom slider. **Freeze frame** holds an image still for measurements; **Resume
  live** returns to the camera stream. The illustration is only shown when no live
  camera frame is available.
- **Import pictures:** Press **Open image…** to load a PNG, JPEG, BMP, TIFF, WebP,
  or GIF image into the viewing box. The imported picture can be zoomed, processed,
  measured, and used for particle counting just like a camera frame. Starting the
  camera shows its live feed; stopping it returns to the imported image. Importing
  a picture stops a running camera. Calibrate scale for each image before making
  physical measurements.
- **Image views and measurement tools:** Use the two labeled slider bars in the
  Image tools panel to choose Original, Grayscale, Edges, Threshold, Intensity map,
  Pan, Length, Rectangle area, Calibrate scale, or Surface profile. Each slider
  snaps between its labeled options. The threshold slider changes threshold-based
  views and particle segmentation. **Save image** saves the selected processed view.
- **Length and area:** In the Image tools panel, choose **Calibrate scale**, enter the known distance and
  unit, and drag over a known ruler/graticule interval in the image. Then choose
  **Length** or **Rectangle area** and drag on the image. Freeze the frame before
  measuring for repeatable results. Recalibrate if the optics, camera resolution,
  or microscope setup changes.
- **Particles:** Choose dark or bright objects, set a minimum pixel area, and press
  **Count particles in frame**. The app reports the count and mean equivalent
  circular diameter (calibrated when a scale has been set). Thresholding treats
  touching particles as one object, so this is a basic segmentation estimate.
- **Surface profile:** Choose **Surface profile** and drag across the image to get
  a grayscale-intensity profile summary. The Intensity map is false color based on
  brightness. Neither is a physical measurement of surface height or roughness.
- **EDS / spectroscopy / diffraction:** Open the separate analysis window and load
  exported instrument data as CSV, TSV, or text with x values in the first column
  and intensity in the second. Header rows are skipped. The window plots the data
  and finds local peaks above the selected relative peak level. Choose
  **EDS / X-ray microanalysis** for a spectrum whose energy axis is in keV; the app
  compares detected peaks with a small built-in list of approximate characteristic
  X-ray line energies and reports tentative matches. **X-ray spectroscopy** reports
  peak positions in the selected x-axis unit. **Electron diffraction** reports
  peaks in an imported one-dimensional intensity profile (for example,
  d-spacing in Å or calibrated radius in pixels).

  These functions analyze exported numeric profiles only. They do not acquire
  spectra, control an EDS/X-ray detector, process arbitrary 2D diffraction images,
  identify crystal phases, or calculate elemental concentrations. EDS matches are
  not confirmed element identifications: overlapping lines, detector calibration,
  background subtraction, standards, and instrument-specific corrections require
  appropriate equipment and validated analysis software. The ordinary USB camera
  cannot perform EDS, X-ray spectroscopy, or electron diffraction.
- **Samples, experiments, and reports:** Open **Samples, experiments and reports…**
  to create or open a JSON project, describe samples, organize experiments by
  sample, edit their notes, and attach the current image as a figure. Project
  images are stored beside the project file in a dedicated assets folder.
  **Export HTML report…** creates a portable report with sample and experiment
  notes, attached figures, and the current analysis log. Save the project after
  editing its records.
- **Batch workflow:** **Batch particle workflow…** applies one threshold, polarity,
  and minimum-pixel-area setting to supported images in a selected folder. It
  exports per-image counts and pixel-based size summaries to CSV, including
  per-file errors. This is repeatable threshold segmentation, not an AI classifier
  or validated identification of materials or defects.
- **USB is a connector, not a single protocol.** This first version supports USB
  cameras using UVC and controllers that expose a serial port (USB CDC or a
  USB-to-UART adapter). Proprietary USB devices and non-text serial protocols need
  device-specific support or firmware.

UART pins are logic-level signals, not USB. Use a USB-to-UART adapter with voltage
levels compatible with your controller, connect TX to RX and RX to TX, and share
ground. Do not connect a voltage supply pin unless you have checked the board's
requirements. The app does not discover or define your controller's command
protocol; implement matching commands in your firmware.

Do not drive motors directly from GPIO pins. Use a motor driver and a suitable
separate motor power supply, and verify the board, driver, and signal voltage
requirements before wiring. For example, you could configure a slider to send
`LED {value}` (such as `LED 80`) or a button to send `STAGE 1`; your firmware must
parse those exact commands and handle the hardware safely.

## Measurement limits

These are basic 2D image-analysis tools, not validated scientific metrology. Results
depend on focus, lighting, calibration, sample contrast, and camera geometry.
Equivalent particle diameter assumes a circular particle with the same segmented
area. True 3D topography or physical surface roughness requires depth information
from suitable hardware (for example, focus stacking or a calibrated profilometer);
a single 2D brightness image cannot determine height. HTML reports organize the
recorded data but are not validated scientific or regulatory reports.

## Hardware and analysis scope

The app is being extended incrementally. Physical stage/focus/illumination control
depends on the exact controller and firmware protocol; configurable serial controls
only send commands and cannot make an unsupported device move. Element matches
from EDS spectra are tentative and do not provide concentrations. This app does
not yet provide validated material/defect classification, 3D reconstruction,
electron-interaction simulation, plugin management, or reusable automated
workflows. Those require additional algorithms, compatible hardware or input data,
and their own validation; the ordinary USB camera alone cannot supply them.

The app is a viewer and controller console, not microscope firmware. USB devices
that do not expose a UVC camera or serial port need device-specific support.
The configurable controls only send serial text; your controller firmware must
implement the commands and translate them into safe motor, lighting, or camera
actions.
