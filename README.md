# tmux-cast

Stream your tmux sessions to Chromecast devices.

## Installation

### From Source (Development)

**Using uv (Recommended):**
```bash
# Clone the repository
git clone <repository-url>
cd tmux-cast

# Install with uv
uv sync

# Or install in development mode
uv pip install -e .
```

**Using pip:**
```bash
# Clone the repository
git clone <repository-url>
cd tmux-cast

# Install in development mode
pip install -e .

# Or install dependencies manually
pip install pychromecast pyte Pillow
```

### From PyPI (when published)

```bash
pip install tmux-cast
```

**Requirements:**
- Python 3.10+
- ffmpeg (must be in PATH)
- tmux (for capturing sessions)

**Installing ffmpeg:**
- macOS: `brew install ffmpeg`
- Ubuntu/Debian: `sudo apt install ffmpeg`
- Fedora: `sudo dnf install ffmpeg`

## Quick Start

### `tcast` - Simple CLI Tool (Recommended)

The simplest way to stream a tmux session to Chromecast:

```bash
# Scan for Chromecast devices
tcast --scan
# or with uv: uv run tcast --scan

# Stream a tmux session (uses window 0, pane 0 automatically)
tcast --source mysession --target-device "Office TV"
# or with uv: uv run tcast --source mysession --target-device "Office TV"

# Using short flags
tcast -s mysession -t "Office TV"

# With custom video settings
tcast -s mysession -t "Office TV" --width 1280 --height 720 --fps 15
```

**Note:** If using `uv`, prefix commands with `uv run`:
```bash
uv run tcast --scan
uv run tcast -s mysession -t "Office TV"
```

**Or activate the virtual environment:**
```bash
source .venv/bin/activate  # After uv sync
tcast --scan
tcast -s mysession -t "Office TV"
deactivate
```

### Interactive Demo App

The interactive demo app guides you through selection:

```bash
python interactive_demo.py
```

This will guide you through:
1. Selecting a tmux session/window/pane to stream from
2. Selecting a Chromecast device to stream to
3. Starting the stream automatically

### Full Command Line (`tmux-cast`)

```bash
# Interactive session selection (will prompt you to choose session/window/pane)
tmux-cast

# List all available tmux sessions, windows, and panes
tmux-cast --list-sessions

# List available Chromecast devices
tmux-cast --list-devices

# Stream with interactive selection to a specific Chromecast device
tmux-cast -d "Living Room TV"

# Stream a specific tmux target (no prompts)
tmux-cast -t mysession:0.1 -d "Office TV"

# Use current pane without interactive selection
tmux-cast --no-interactive

# Just get the stream URL (don't cast automatically)
tmux-cast --url-only

# Run the demo (doesn't require tmux or Chromecast)
python demo.py

# Interactive demo app (choose both tmux session and Chromecast)
python interactive_demo.py
```

**Interactive Selection:**
When you run `tmux-cast` without a target, it will:
1. Show all available tmux sessions
2. Let you choose a session
3. Show all windows in that session
4. Let you choose a window  
5. Show all panes in that window (or auto-select if only one)
6. Start streaming from the selected pane

**Difference between `tcast` and `tmux-cast`:**
- `tcast`: Simple CLI - just specify session name and Chromecast device (always uses window 0, pane 0)
- `tmux-cast`: Full-featured CLI with interactive selection and more options

### Python API

```python
from tmuxcast import TmuxCast, TmuxCastConfig

# Simple usage with context manager
with TmuxCast() as caster:
    caster.cast_to("Living Room TV")
    input("Press Enter to stop...")

# More control
config = TmuxCastConfig(
    tmux_target="dev:0.0",
    output_width=1920,
    output_height=1080,
    fps=15,
    font_size=24,
)

caster = TmuxCast(config)
url = caster.start()
print(f"Stream available at: {url}")

# Discover devices
devices = caster.discover_devices()
for d in devices:
    print(f"  {d.name} ({d.model})")

# Cast to one
caster.cast_to("Living Room")

# ... do stuff ...

caster.stop()
```

### Individual Components

The library exposes its components for custom pipelines:

```python
from tmuxcast import TmuxCapture, TerminalRenderer, VideoStreamer

# Capture tmux content
capture = TmuxCapture("session:window.pane")
content = capture.capture_ansi()
cols, rows = capture.get_pane_size()

# Render to images
renderer = TerminalRenderer(cols=cols, rows=rows)
renderer.feed(content)
image = renderer.render()
image.save("terminal.png")

# Stream video
streamer = VideoStreamer(width=1280, height=720, fps=10)
url = streamer.start()
streamer.write_frame(image.tobytes())
```

## Configuration Options

| Option | Default | Description |
|--------|---------|-------------|
| `tmux_target` | `""` | tmux target (session:window.pane) |
| `output_width` | 1920 | Video output width |
| `output_height` | 1080 | Video output height |
| `fps` | 10 | Frames per second |
| `bitrate` | "2M" | Video bitrate |
| `font_size` | 20 | Terminal font size |
| `padding` | 40 | Padding around terminal |
| `bg_color` | "#1e1e1e" | Background color |
| `fg_color` | "#d4d4d4" | Foreground color |
| `port` | 0 | HTTP server port (0=auto) |

## How It Works

1. **Capture**: Reads tmux pane content including ANSI escape sequences
2. **Render**: Uses `pyte` to parse ANSI codes and `Pillow` to rasterize
3. **Encode**: Pipes frames to `ffmpeg` for H.264 encoding
4. **Stream**: Serves fragmented MP4 over HTTP
5. **Cast**: Uses Google Cast protocol to play stream on Chromecast

## Latency

Typical latency is 1-3 seconds due to:
- Video encoding buffer requirements
- Chromecast buffering
- Network latency

For lower latency, try:
- Higher FPS (reduces keyframe interval)
- Lower resolution
- Local network only

## Troubleshooting

**"No Chromecast devices found"**
- Ensure your computer and Chromecast are on the same network
- Check that multicast/mDNS traffic is allowed
- Try increasing discovery timeout

**"ffmpeg not found"**
- Install ffmpeg: `apt install ffmpeg` or `brew install ffmpeg`

**Stream URL works but Chromecast won't play**
- Check firewall allows incoming connections on the stream port
- Ensure stream URL uses your LAN IP, not localhost

**Poor video quality**
- Increase `bitrate` (e.g., "4M")
- Increase `font_size` for readability

## License

MIT
