#!/usr/bin/env python3
"""
Demo script for tmux-cast that doesn't require tmux or Chromecast.

Generates fake terminal content and streams it.
Open the printed URL in a browser or media player to view.
"""

import time
import sys
from tmuxcast.terminal import TerminalRenderer
from tmuxcast.stream import VideoStreamer
from PIL import Image


def fake_tmux_content(frame: int) -> str:
    """Generate fake terminal content that changes each frame."""
    bars = ["▏", "▎", "▍", "▌", "▋", "▊", "▉", "█"]
    cpu_bar = bars[frame % 8] * (3 + frame % 5)
    mem_bar = bars[(frame + 3) % 8] * (2 + frame % 4)
    
    return f"""\033[32muser@host\033[0m:\033[34m~/projects/tmux-cast\033[0m$ htop
\033[1;37m  PID USER      PR  NI    VIRT    RES    SHR S  %CPU  %MEM     TIME+ COMMAND\033[0m
 1234 user      20   0  \033[33m{1000 + frame*10:5}M\033[0m   512M   128M S  \033[31m{12.5 + (frame % 20):.1f}\033[0m   4.2   1:23.45 python
 5678 user      20   0   256M    64M    32M S   2.1   0.5   0:45.67 bash
 9012 root      20   0   128M    16M     8M S   0.3   0.1   0:12.34 systemd

\033[44;37m CPU: [{cpu_bar:<8}] {12 + frame % 30:3}%    MEM: [{mem_bar:<6}] {45 + frame % 15:3}% \033[0m

\033[32m$\033[0m echo "Frame {frame}"
Frame {frame}

\033[33m$\033[0m \033[7m \033[0m
"""


def main():
    print("tmux-cast demo")
    print("=" * 40)
    
    # Settings
    cols, rows = 80, 24
    width, height = 1280, 720
    fps = 10
    
    # Initialize
    renderer = TerminalRenderer(cols=cols, rows=rows)
    streamer = VideoStreamer(width=width, height=height, fps=fps)
    
    url = streamer.start()
    print(f"\n🎬 Stream URL: {url}")
    print(f"   Open this URL in VLC or mpv:")
    print(f"   - VLC: Media > Open Network Stream > paste URL")
    print(f"   - mpv: mpv {url}")
    print(f"   - Browser: Open URL (may need to wait a few seconds)")
    print(f"\nPress Ctrl+C to stop\n")
    
    frame = 0
    last_status = time.time()
    
    try:
        while True:
            # Generate content
            content = fake_tmux_content(frame)
            
            # Render
            renderer.feed(content)
            img = renderer.render()
            
            # Resize to output dimensions
            if img.size != (width, height):
                img = img.resize((width, height), Image.Resampling.LANCZOS)
            
            # Stream
            streamer.write_frame(img.tobytes())
            
            frame += 1
            
            # Print status every 5 seconds
            now = time.time()
            if now - last_status >= 5.0:
                print(f"  [Streaming] Frame {frame}, {streamer.frames_written} frames sent")
                last_status = now
            
            time.sleep(1.0 / fps)
            
    except KeyboardInterrupt:
        print("\n\nStopping...")
    finally:
        streamer.stop()
        print("Done")


if __name__ == "__main__":
    main()
