"""
TmuxCast - Stream tmux sessions to Chromecast devices.

Main orchestrator that ties together terminal capture, rendering,
video encoding, and Chromecast control.
"""

import time
import threading
import signal
import sys
from typing import Optional
from dataclasses import dataclass, field

from .terminal import TmuxCapture, TerminalRenderer, TerminalStyle, select_tmux_target
from .stream import VideoStreamer
from .cast import CastDiscovery, CastController, CastDevice


@dataclass
class TmuxCastConfig:
    """Configuration for TmuxCast."""
    # tmux target
    tmux_target: str = ""
    
    # Display settings
    output_width: int = 1920
    output_height: int = 1080
    fps: int = 10
    bitrate: str = "2M"
    
    # Terminal style
    font_size: int = 20
    padding: int = 40
    bg_color: str = "#1e1e1e"
    fg_color: str = "#d4d4d4"
    
    # Chromecast
    device_name: Optional[str] = None
    
    # Server
    port: int = 0  # 0 = auto-assign


class TmuxCast:
    """
    Main class for streaming tmux to Chromecast.
    
    Usage:
        caster = TmuxCast()
        caster.start()
        # ... runs until stopped
        caster.stop()
    
    Or with context manager:
        with TmuxCast() as caster:
            caster.cast_to("Living Room TV")
            input("Press Enter to stop...")
    """
    
    def __init__(self, config: Optional[TmuxCastConfig] = None):
        self.config = config or TmuxCastConfig()
        
        # Components (created on start)
        self._capture: Optional[TmuxCapture] = None
        self._renderer: Optional[TerminalRenderer] = None
        self._streamer: Optional[VideoStreamer] = None
        self._controller: Optional[CastController] = None
        self._discovery: Optional[CastDiscovery] = None
        
        # State
        self._running = False
        self._capture_thread: Optional[threading.Thread] = None
        self._keepalive_thread: Optional[threading.Thread] = None
        self._stream_url: Optional[str] = None
        
        # Callbacks
        self.on_frame: Optional[callable] = None  # Called after each frame
        self.on_error: Optional[callable] = None
    
    def start(self) -> str:
        """
        Start the streaming pipeline.
        
        Returns the stream URL (can be used manually or with cast_to).
        """
        if self._running:
            return self._stream_url
        
        # Initialize capture
        self._capture = TmuxCapture(self.config.tmux_target)
        
        # Get terminal size
        try:
            cols, rows = self._capture.get_pane_size()
        except RuntimeError:
            # Default if not in tmux
            cols, rows = 80, 24
        
        # Create terminal style
        style = TerminalStyle(
            font_size=self.config.font_size,
            padding=self.config.padding,
            bg_color=self.config.bg_color,
            fg_color=self.config.fg_color,
        )
        
        # Initialize renderer
        self._renderer = TerminalRenderer(cols=cols, rows=rows, style=style)
        
        # Calculate output dimensions that maintain aspect ratio
        # and fit within configured bounds
        term_w, term_h = self._renderer.image_size
        scale = min(
            self.config.output_width / term_w,
            self.config.output_height / term_h
        )
        
        # Round to even dimensions (required by many codecs)
        output_w = int(term_w * scale) & ~1
        output_h = int(term_h * scale) & ~1
        
        # Initialize streamer
        self._streamer = VideoStreamer(
            width=output_w,
            height=output_h,
            fps=self.config.fps,
            port=self.config.port,
            bitrate=self.config.bitrate,
        )
        
        self._stream_url = self._streamer.start()
        
        # Start capture loop
        self._running = True
        self._capture_thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._capture_thread.start()
        
        return self._stream_url
    
    def _capture_loop(self):
        """Main loop that captures, renders, and streams frames."""
        from PIL import Image
        
        frame_interval = 1.0 / self.config.fps
        last_content = None
        last_valid_image = None
        error_count = 0
        last_error_time = 0.0
        last_error_message = None
        
        while self._running:
            loop_start = time.time()
            
            try:
                # Capture terminal content
                content = self._capture.capture_ansi()
                
                # Reset error tracking on successful capture
                error_count = 0
                last_error_message = None
                
                # Always render and write frames for video continuity
                # Even if content hasn't changed, we need to maintain frame rate
                if content != last_content:
                    last_content = content
                    # Feed new content to renderer
                    self._renderer.feed(content)
                # Note: If content hasn't changed, renderer still has the last frame
                
                # Render current state to image
                img = self._renderer.render()
                last_valid_image = img  # Store for use when session is unavailable
                
                # Resize if needed
                target_size = (
                    self._streamer.config.width,
                    self._streamer.config.height
                )
                if img.size != target_size:
                    img = img.resize(target_size, Image.Resampling.LANCZOS)
                
                # Always write frame to maintain continuous stream
                self._streamer.write_frame(img.tobytes())
                
                if self.on_frame:
                    self.on_frame()
                    
            except RuntimeError as e:
                error_msg = str(e)
                current_time = time.time()
                
                # Rate limit error messages (max once per 10 seconds)
                if current_time - last_error_time >= 10.0 or error_msg != last_error_message:
                    # Check if it's a session loss error
                    if "can't find session" in error_msg.lower() or "can't find pane" in error_msg.lower():
                        if self.on_error:
                            self.on_error(e)
                        else:
                            print(f"Session unavailable: {error_msg}", file=sys.stderr)
                            print("Attempting recovery... (will continue with last frame)", file=sys.stderr)
                    else:
                        if self.on_error:
                            self.on_error(e)
                        else:
                            print(f"Capture error: {error_msg}", file=sys.stderr)
                    
                    last_error_time = current_time
                    last_error_message = error_msg
                
                error_count += 1
                
                # Continue streaming the last valid frame to maintain video continuity
                if last_valid_image is not None:
                    try:
                        target_size = (
                            self._streamer.config.width,
                            self._streamer.config.height
                        )
                        if last_valid_image.size != target_size:
                            frame_img = last_valid_image.resize(target_size, Image.Resampling.LANCZOS)
                        else:
                            frame_img = last_valid_image
                        
                        # Write the last valid frame
                        self._streamer.write_frame(frame_img.tobytes())
                    except Exception:
                        # If we can't write the frame, just continue
                        pass
                    
            except Exception as e:
                # Other unexpected errors
                if self.on_error:
                    self.on_error(e)
                else:
                    current_time = time.time()
                    if current_time - last_error_time >= 10.0:
                        print(f"Unexpected error: {e}", file=sys.stderr)
                        last_error_time = current_time
                
                # Continue with last valid frame if available
                if last_valid_image is not None:
                    try:
                        target_size = (
                            self._streamer.config.width,
                            self._streamer.config.height
                        )
                        if last_valid_image.size != target_size:
                            frame_img = last_valid_image.resize(target_size, Image.Resampling.LANCZOS)
                        else:
                            frame_img = last_valid_image
                        self._streamer.write_frame(frame_img.tobytes())
                    except Exception:
                        pass
            
            # Maintain frame rate
            elapsed = time.time() - loop_start
            sleep_time = frame_interval - elapsed
            if sleep_time > 0:
                time.sleep(sleep_time)
    
    def discover_devices(self, timeout: float = 10.0) -> list[CastDevice]:
        """Discover available Chromecast devices."""
        self._discovery = CastDiscovery(timeout=timeout)
        return self._discovery.discover()
    
    def cast_to(self, device_name: Optional[str] = None) -> bool:
        """
        Cast the stream to a Chromecast device.
        
        Args:
            device_name: Name of the device (partial match OK).
                        If None, uses first discovered device.
        """
        if not self._stream_url:
            raise RuntimeError("Must call start() before cast_to()")
        
        # Discover if needed
        if not self._discovery:
            self.discover_devices()
        
        # Find device
        if device_name:
            device = self._discovery.get_device_by_name(device_name)
            if not device:
                raise ValueError(f"Device '{device_name}' not found")
        else:
            devices = list(self._discovery._devices.values())
            if not devices:
                raise RuntimeError("No Chromecast devices found")
            device = devices[0]
        
        # Connect and play
        self._controller = CastController(device)
        self._controller.connect()
        
        # Wait a moment to ensure stream has some data before casting
        # This helps Chromecast recognize it as a valid stream
        import time
        time.sleep(1.0)  # Give encoder time to produce initial data
        
        self._controller.play_url(
            self._stream_url,
            content_type="video/mp4",
            title="tmux-cast"
        )
        
        # Periodically update status to keep Chromecast playing
        # Start a background thread to monitor and maintain playback
        def keep_alive():
            while self._running and self._controller:
                try:
                    time.sleep(5.0)  # Check every 5 seconds (less aggressive)
                    if self._controller._mc:
                        # Update status to keep connection alive
                        self._controller._mc.update_status()
                        # Only try to resume if we're sure it stopped and shouldn't have
                        # Don't force play if Chromecast is buffering or paused by user
                        if (self._controller._mc.status and 
                            self._controller._mc.status.player_state == "IDLE" and 
                            self._stream_url):
                            # Only resume if it went to IDLE unexpectedly
                            try:
                                self._controller._mc.play()
                            except:
                                pass
                except Exception:
                    pass
        
        import threading
        self._keepalive_thread = threading.Thread(target=keep_alive, daemon=True)
        self._keepalive_thread.start()
        
        return True
    
    def stop(self):
        """Stop streaming and disconnect from Chromecast."""
        self._running = False
        
        if self._capture_thread:
            self._capture_thread.join(timeout=2)
        
        if self._controller:
            self._controller.stop()
            self._controller.disconnect()
        
        if self._streamer:
            self._streamer.stop()
        
        if self._discovery:
            self._discovery.stop()
    
    @property
    def stream_url(self) -> Optional[str]:
        """Get the current stream URL."""
        return self._stream_url
    
    @property
    def is_running(self) -> bool:
        """Check if streaming is active."""
        return self._running
    
    def is_streaming(self) -> bool:
        """
        Check if the stream is actively being served to clients.
        
        Returns:
            True if there are active connections and frames are being written
        """
        if not self._running or not self._streamer:
            return False
        
        # Check if there are active connections
        has_connections = self._streamer.server.has_active_connections()
        
        # Check if frames are being written (at least some frames written recently)
        frames_written = self._streamer.frames_written
        
        return has_connections and frames_written > 0
    
    def get_stream_status(self) -> dict:
        """
        Get detailed streaming status.
        
        Returns:
            Dictionary with streaming status information
        """
        status = {
            "running": self._running,
            "stream_url": self._stream_url,
            "frames_written": 0,
            "active_connections": 0,
            "chromecast_playing": False,
        }
        
        if self._streamer:
            status["frames_written"] = self._streamer.frames_written
            status["active_connections"] = self._streamer.server.get_active_connections()
        
        if self._controller:
            try:
                status["chromecast_playing"] = self._controller.is_playing
                status["chromecast_status"] = self._controller.status
            except:
                pass
        
        return status
    
    def __enter__(self):
        self.start()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()
        return False


def main():
    """CLI entry point."""
    import argparse
    
    parser = argparse.ArgumentParser(
        description="Stream tmux sessions to Chromecast"
    )
    parser.add_argument(
        "-t", "--target",
        default="",
        help="tmux target (session:window.pane)"
    )
    parser.add_argument(
        "-d", "--device",
        default=None,
        help="Chromecast device name"
    )
    parser.add_argument(
        "--list-devices",
        action="store_true",
        help="List available Chromecast devices and exit"
    )
    parser.add_argument(
        "--width", type=int, default=1920,
        help="Output video width (default: 1920)"
    )
    parser.add_argument(
        "--height", type=int, default=1080,
        help="Output video height (default: 1080)"
    )
    parser.add_argument(
        "--fps", type=int, default=10,
        help="Frames per second (default: 10)"
    )
    parser.add_argument(
        "--font-size", type=int, default=20,
        help="Terminal font size (default: 20)"
    )
    parser.add_argument(
        "--port", type=int, default=0,
        help="HTTP server port (default: auto)"
    )
    parser.add_argument(
        "--url-only",
        action="store_true",
        help="Only print stream URL, don't cast"
    )
    parser.add_argument(
        "--list-sessions",
        action="store_true",
        help="List available tmux sessions and exit"
    )
    parser.add_argument(
        "--no-interactive",
        action="store_true",
        help="Don't prompt for session selection (use current pane if no target specified)"
    )
    
    args = parser.parse_args()
    
    # List devices mode
    if args.list_devices:
        from .cast import discover_and_list
        discover_and_list()
        return
    
    # List sessions mode
    if args.list_sessions:
        from .terminal import list_tmux_sessions, list_tmux_windows, list_tmux_panes
        sessions = list_tmux_sessions()
        if not sessions:
            print("No tmux sessions found.")
            return
        
        print("\nAvailable tmux sessions:")
        for session_id, session_name in sessions:
            print(f"\n  Session: {session_name} (id: {session_id})")
            windows = list_tmux_windows(session_id)
            for window_id, window_name in windows:
                print(f"    Window {window_id}: {window_name}")
                panes = list_tmux_panes(session_id, window_id)
                for pane_id, pane_title in panes:
                    title_str = f" - {pane_title}" if pane_title else ""
                    print(f"      Pane {pane_id}{title_str}")
                    print(f"        Target: {session_id}:{window_id}.{pane_id}")
        return
    
    # Determine tmux target
    tmux_target = args.target
    if not tmux_target and not args.no_interactive:
        # Interactive selection
        print("No tmux target specified. Starting interactive selection...")
        tmux_target = select_tmux_target()
        if not tmux_target:
            print("No target selected. Exiting.")
            return
    
    # Create config
    config = TmuxCastConfig(
        tmux_target=tmux_target or "",
        output_width=args.width,
        output_height=args.height,
        fps=args.fps,
        font_size=args.font_size,
        port=args.port,
        device_name=args.device,
    )
    
    # Create caster
    caster = TmuxCast(config)
    
    # Handle Ctrl+C gracefully
    def signal_handler(sig, frame):
        print("\nStopping...")
        caster.stop()
        sys.exit(0)
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # Start streaming
    url = caster.start()
    print(f"Stream URL: {url}")
    
    if args.url_only:
        print("\nPress Ctrl+C to stop")
        while caster.is_running:
            time.sleep(1)
    else:
        # Cast to device
        try:
            caster.cast_to(args.device)
            device_name = args.device or "first available device"
            print(f"Casting to: {device_name}")
            print("\nPress Ctrl+C to stop")
            
            while caster.is_running:
                time.sleep(1)
                
        except Exception as e:
            print(f"Cast error: {e}")
            print(f"\nYou can still access the stream at: {url}")
            while caster.is_running:
                time.sleep(1)


def tcast_main():
    """Simplified CLI entry point for tcast command."""
    import argparse
    import signal
    import sys
    
    parser = argparse.ArgumentParser(
        prog="tcast",
        description="Stream tmux sessions to Chromecast devices (simplified CLI)"
    )
    parser.add_argument(
        "-s", "--source",
        dest="source",
        help="tmux session name to stream from (uses window 0, pane 0)"
    )
    parser.add_argument(
        "-t", "--target-device",
        dest="target_device",
        help="Chromecast device friendly name to stream to"
    )
    parser.add_argument(
        "--scan",
        action="store_true",
        help="Scan and list available Chromecast devices"
    )
    parser.add_argument(
        "--width", type=int, default=1920,
        help="Output video width (default: 1920)"
    )
    parser.add_argument(
        "--height", type=int, default=1080,
        help="Output video height (default: 1080)"
    )
    parser.add_argument(
        "--fps", type=int, default=10,
        help="Frames per second (default: 10)"
    )
    
    args = parser.parse_args()
    
    # Scan mode
    if args.scan:
        from .cast import discover_and_list
        discover_and_list()
        return
    
    # Validate required arguments
    if not args.source:
        parser.error("--source (-s) is required. Use --scan to find Chromecast devices.")
    
    if not args.target_device:
        parser.error("--target-device (-t) is required. Use --scan to find Chromecast devices.")
    
    # Verify tmux session exists
    from .terminal import list_tmux_sessions
    sessions = list_tmux_sessions()
    session_found = False
    
    for sid, sname in sessions:
        if args.source == sname or args.source == sid:
            session_found = True
            break
    
    if not session_found:
        print(f"Error: tmux session '{args.source}' not found.")
        print("\nAvailable tmux sessions:")
        for sid, sname in sessions:
            print(f"  - {sname} (id: {sid})")
        sys.exit(1)
    
    # Construct tmux target: session:0.0 (window 0, pane 0)
    # tmux accepts both session name and session ID in targets
    tmux_target = f"{args.source}:0.0"
    
    print(f"Streaming from tmux session: {args.source} (window 0, pane 0)")
    print(f"Target Chromecast device: {args.target_device}")
    print()
    
    # Create config
    config = TmuxCastConfig(
        tmux_target=tmux_target,
        output_width=args.width,
        output_height=args.height,
        fps=args.fps,
        device_name=args.target_device,
    )
    
    # Create caster
    caster = TmuxCast(config)
    
    # Handle Ctrl+C gracefully
    def signal_handler(sig, frame):
        print("\n\nStopping stream...")
        caster.stop()
        print("Done. Goodbye!")
        sys.exit(0)
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    try:
        # Start streaming
        print("Initializing stream...")
        stream_url = caster.start()
        print(f"✓ Stream URL: {stream_url}")
        
        # Cast to device
        print(f"\nConnecting to {args.target_device}...")
        caster.cast_to(args.target_device)
        print(f"✓ Casting to: {args.target_device}")
        
        print("\n" + "=" * 60)
        print("Streaming Active!")
        print("=" * 60)
        print(f"\nStreaming from: {tmux_target}")
        print(f"Streaming to: {args.target_device}")
        print(f"Stream URL: {stream_url}")
        print("\nThe stream is now active on your Chromecast.")
        print("Press Ctrl+C to stop streaming.\n")
        
        # Keep running until interrupted
        while caster.is_running:
            time.sleep(1)
            
    except KeyboardInterrupt:
        signal_handler(None, None)
    except Exception as e:
        print(f"\nError: {e}")
        if stream_url:
            print(f"\nStream URL is still available: {stream_url}")
            print("You can open it manually in a media player.")
        caster.stop()
        sys.exit(1)


if __name__ == "__main__":
    main()
