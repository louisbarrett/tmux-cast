"""
Video encoding and HTTP streaming for Chromecast.

Uses ffmpeg for encoding frames into a streamable format and serves
via HTTP for the Chromecast media receiver.
"""

import subprocess
import threading
import queue
import time
import struct
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Optional, Callable
from dataclasses import dataclass
import socket
import io


@dataclass  
class StreamConfig:
    """Configuration for the video stream."""
    width: int = 1280
    height: int = 720
    fps: int = 10
    bitrate: str = "1M"
    codec: str = "libx264"  # or "libvpx" for VP8
    format: str = "mp4"  # "mp4" for fragmented mp4, "webm" for webm
    preset: str = "ultrafast"  # x264 preset for low latency


class FrameEncoder:
    """Encodes raw frames to video using ffmpeg."""
    
    def __init__(self, config: StreamConfig):
        self.config = config
        self._process: Optional[subprocess.Popen] = None
        self._output_queue: queue.Queue[bytes] = queue.Queue(maxsize=120)
        self._reader_thread: Optional[threading.Thread] = None
        self._running = False
        self._header: bytes = b""  # Store ftyp+moov atoms
        self._header_complete = False
        self._header_lock = threading.Lock()
    
    def start(self):
        """Start the ffmpeg encoding process."""
        if self._process is not None:
            return
        
        # Build ffmpeg command for fragmented MP4 (good for streaming)
        cmd = [
            "ffmpeg",
            "-y",  # overwrite
            "-f", "rawvideo",
            "-pix_fmt", "rgb24",
            "-s", f"{self.config.width}x{self.config.height}",
            "-r", str(self.config.fps),
            "-i", "-",  # stdin
            "-c:v", self.config.codec,
            "-preset", self.config.preset,
            "-tune", "zerolatency",
            "-b:v", self.config.bitrate,
            "-maxrate", self.config.bitrate,
            "-bufsize", "500k",
            "-pix_fmt", "yuv420p",
            "-g", str(self.config.fps * 2),  # keyframe every 2 seconds (more frequent for Chromecast)
            "-keyint_min", str(self.config.fps),  # minimum keyframe interval
            "-f", "mp4",
            "-movflags", "frag_keyframe+default_base_moof",
            "-frag_duration", "1000000",  # 1 second fragments (more compatible with Chromecast)
            "-frag_size", "100000",  # Fragment size hint
            "-",  # stdout
        ]
        
        self._process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,  # Capture stderr for debugging
        )
        
        self._running = True
        self._reader_thread = threading.Thread(target=self._read_output, daemon=True)
        self._reader_thread.start()
    
    def _read_output(self):
        """Background thread to read encoded output from ffmpeg."""
        header_buffer = bytearray()
        
        while self._running and self._process and self._process.stdout:
            chunk = self._process.stdout.read(32768)
            if not chunk:
                break
            
            # Capture the header (ftyp + moov atoms) from the first chunks
            with self._header_lock:
                if not self._header_complete:
                    header_buffer.extend(chunk)
                    # Look for the first moof atom which signals end of header
                    # ftyp + moov should be in the first few KB
                    # With empty_moov, the moov is minimal, so we need to find moof correctly
                    if len(header_buffer) >= 32:  # Need at least ftyp atom
                        # Find moof atom (indicates start of fragments)
                        # MP4 atoms have a 4-byte big-endian size prefix, then 4-byte type
                        moof_pos = header_buffer.find(b'moof')
                        if moof_pos >= 4:
                            # Verify this is actually an atom boundary
                            # The 4 bytes before 'moof' should be the atom size (big-endian)
                            atom_start = moof_pos - 4
                            # Parse atom size to verify it's valid
                            try:
                                atom_size = struct.unpack('>I', header_buffer[atom_start:moof_pos])[0]
                                # Atom size should be reasonable (at least 8, not too large)
                                if 8 <= atom_size <= len(header_buffer) - atom_start:
                                    # Header is everything before the moof atom
                                    self._header = bytes(header_buffer[:atom_start])
                                    self._header_complete = True
                            except (struct.error, IndexError):
                                # If parsing fails, continue accumulating
                                pass
                        # Safety: if we have a lot of data and no moof, something might be wrong
                        # But with empty_moov, moof should appear quickly
                        if len(header_buffer) > 65536:  # 64KB safety limit
                            # Fallback: use everything we have as header
                            # This shouldn't happen with proper MP4, but prevents infinite buffer
                            self._header = bytes(header_buffer)
                            self._header_complete = True
            
            try:
                self._output_queue.put(chunk, timeout=1)
            except queue.Full:
                # Drop old data if queue is full
                try:
                    self._output_queue.get_nowait()
                    self._output_queue.put(chunk, timeout=0.1)
                except:
                    pass
    
    def get_header(self) -> bytes:
        """Get the MP4 header (ftyp + moov atoms)."""
        with self._header_lock:
            return self._header
    
    def is_header_ready(self) -> bool:
        """Check if header has been captured."""
        with self._header_lock:
            return self._header_complete
    
    def write_frame(self, frame_data: bytes):
        """Write a raw RGB frame to the encoder."""
        if self._process and self._process.stdin:
            try:
                self._process.stdin.write(frame_data)
                self._process.stdin.flush()
            except BrokenPipeError:
                # Encoder process died - this is a problem
                self._running = False
            except Exception as e:
                # Other errors - log but continue
                pass
    
    def read_output(self, timeout: float = 0.1) -> Optional[bytes]:
        """Read encoded output (may block briefly)."""
        try:
            return self._output_queue.get(timeout=timeout)
        except queue.Empty:
            return None
    
    def stop(self):
        """Stop the encoder."""
        self._running = False
        if self._process:
            try:
                if self._process.stdin:
                    self._process.stdin.close()
                # Give ffmpeg a moment to finish processing
                try:
                    self._process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    # If it doesn't exit gracefully, force kill
                    self._process.kill()
                    self._process.wait(timeout=1)
            except Exception:
                # If anything fails, just kill it
                try:
                    self._process.kill()
                except:
                    pass
            self._process = None


class StreamBuffer:
    """Thread-safe buffer for accumulating stream data with header preservation."""
    
    def __init__(self, max_size: int = 20 * 1024 * 1024):  # 20MB default
        self._header = b""  # MP4 header (ftyp + moov)
        self._header_ready = False  # Flag to indicate header is ready
        self._buffer = bytearray()
        self._lock = threading.Lock()
        self._max_size = max_size
        self._total_written = 0
    
    def set_header(self, header: bytes):
        """Set the MP4 header that all clients receive first."""
        with self._lock:
            self._header = header
            self._header_ready = True
    
    def is_header_ready(self) -> bool:
        """Check if header is ready."""
        with self._lock:
            return self._header_ready
    
    def write(self, data: bytes):
        """Append data to buffer."""
        with self._lock:
            self._buffer.extend(data)
            self._total_written += len(data)
            # Trim if too large (keep recent data, but always keep some context)
            if len(self._buffer) > self._max_size:
                # Keep the last 75% of max_size
                keep_size = int(self._max_size * 0.75)
                trim = len(self._buffer) - keep_size
                self._buffer = self._buffer[trim:]
    
    def read_from(self, position: int, include_header: bool = False, max_bytes: Optional[int] = None) -> tuple[bytes, int]:
        """
        Read data from position, return (data, new_position).
        
        If include_header is True and position is 0, prepend the MP4 header.
        Returns empty bytes if no new data is available.
        
        Args:
            position: Current read position
            include_header: Whether to include MP4 header if starting from 0
            max_bytes: Maximum bytes to read (for throttling to prevent sending all at once)
        """
        with self._lock:
            result = b""
            
            # If starting from 0 and we have a header, include it
            if include_header and position == 0 and self._header:
                header_data = self._header
                if max_bytes and len(header_data) > max_bytes:
                    # If header is too large, send it in chunks
                    result = header_data[:max_bytes]
                    return result, position + len(result)
                result = header_data
            
            if position < 0:
                position = 0
            
            # Adjust for buffer trimming
            available_start = self._total_written - len(self._buffer)
            if position < available_start:
                # Data has been trimmed, start from available data
                position = available_start
            
            # Calculate how much new data is available
            buffer_offset = position - available_start
            if buffer_offset < len(self._buffer):
                # There's new data to read
                available_data = bytes(self._buffer[buffer_offset:])
                
                # Limit how much we send at once to ensure continuous flow
                if max_bytes:
                    # Send in chunks to maintain continuous stream
                    chunk_size = min(max_bytes, len(available_data))
                    result += available_data[:chunk_size]
                    new_position = position + len(result)
                else:
                    # Send all available data
                    result += available_data
                    new_position = self._total_written
            else:
                # No new data, position is already at the end
                new_position = position
            
            return result, new_position
    
    def has_new_data(self, position: int) -> bool:
        """
        Check if there's new data available from the given position.
        
        Args:
            position: Current read position
            
        Returns:
            True if new data is available
        """
        with self._lock:
            if position < 0:
                return True  # Start from beginning
            
            # Adjust for buffer trimming
            available_start = self._total_written - len(self._buffer)
            if position < available_start:
                return True  # Data was trimmed, need to resync
            
            # Check if position is behind total_written
            return position < self._total_written
    
    def get_size(self) -> int:
        """Get current buffer size."""
        with self._lock:
            return len(self._buffer)
    
    def get_total_written(self) -> int:
        """Get total bytes written since start."""
        with self._lock:
            return self._total_written


class StreamHandler(BaseHTTPRequestHandler):
    """HTTP handler for serving the video stream."""
    
    buffer: StreamBuffer = None  # Set by server
    content_type: str = "video/mp4"  # Chromecast compatible MIME type
    protocol_version = "HTTP/1.1"
    
    def log_message(self, format, *args):
        pass  # Suppress logging
    
    def handle_one_request(self):
        """Handle a single HTTP request, suppressing connection reset errors."""
        try:
            super().handle_one_request()
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError, OSError) as e:
            # Client disconnected - this is normal, don't log as error
            # Connection reset errors are common when clients disconnect
            pass
        except Exception:
            # Other errors might be worth logging, but for now suppress all
            pass
    
    def handle(self):
        """Handle requests, suppressing connection errors."""
        try:
            super().handle()
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError, OSError):
            # Client disconnected - normal occurrence
            pass
    
    def do_HEAD(self):
        """Handle HEAD requests for media players that probe first."""
        self.send_response(200)
        self.send_header("Content-Type", self.content_type)
        self.send_header("Accept-Ranges", "none")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
    
    def do_OPTIONS(self):
        """Handle CORS preflight."""
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Range")
        self.end_headers()
    
    def do_GET(self):
        if self.path == "/stream.mp4" or self.path == "/":
            self._serve_stream()
        elif self.path == "/health":
            self._serve_health()
        else:
            self.send_error(404)
    
    def _serve_health(self):
        """Health check endpoint."""
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status":"ok"}')
    
    def _serve_stream(self):
        """Serve the video stream."""
        # Wait for header to be ready (with timeout)
        header_wait_start = time.time()
        while not self.buffer.is_header_ready():
            if time.time() - header_wait_start > 10.0:  # 10 second timeout
                self.send_error(503, "Stream not ready")
                return
            time.sleep(0.1)
        
        self.send_response(200)
        self.send_header("Content-Type", self.content_type)
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Accept-Ranges", "bytes")  # Chromecast prefers "bytes"
        self.send_header("Connection", "keep-alive")
        # Don't set Transfer-Encoding: chunked - let Python handle it naturally
        # Don't set Content-Length - this is a live stream
        self.end_headers()
        
        position = 0
        first_read = True
        last_data_sent = time.time()
        chunk_size = 65536  # 64KB chunks - send data incrementally to maintain continuous flow
        
        while True:
            try:
                # Check if there's new data available
                if self.buffer.has_new_data(position) or first_read:
                    # Read data in chunks to ensure continuous flow
                    # This prevents sending all buffered data at once
                    data, new_position = self.buffer.read_from(
                        position, 
                        include_header=first_read,
                        max_bytes=chunk_size  # Limit chunk size for continuous streaming
                    )
                    first_read = False
                    
                    if data:
                        # Write data and flush to ensure delivery
                        self.wfile.write(data)
                        self.wfile.flush()
                        position = new_position
                        last_data_sent = time.time()
                        
                        # Small delay to ensure data flows continuously, not in bursts
                        time.sleep(0.01)  # 10ms delay between chunks
                    else:
                        # No data available yet, wait a bit
                        time.sleep(0.05)
                else:
                    # No new data in buffer, but keep connection alive
                    # Send keepalive flush periodically
                    if time.time() - last_data_sent > 1.0:
                        try:
                            self.wfile.flush()  # Keep connection alive
                            last_data_sent = time.time()
                        except:
                            break
                    time.sleep(0.1)  # Wait for new data
                
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                # Client disconnected
                break
            except Exception as e:
                # Log error but don't break - might be recoverable
                # In production, you might want to log this
                break


class StreamServer:
    """HTTP server for video streaming."""
    
    def __init__(self, host: str = "0.0.0.0", port: int = 0):
        self.host = host
        self.port = port
        self.buffer = StreamBuffer()
        self._server: Optional[HTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._active_connections = 0
        self._connection_lock = threading.Lock()
    
    def start(self) -> int:
        """Start the server, returns the actual port."""
        # Create handler class with buffer reference and connection tracking
        buffer_ref = self.buffer
        server_ref = self
        
        class TrackingHandler(StreamHandler):
            buffer = buffer_ref
            
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                with server_ref._connection_lock:
                    server_ref._active_connections += 1
            
            def finish(self):
                super().finish()
                with server_ref._connection_lock:
                    server_ref._active_connections = max(0, server_ref._active_connections - 1)
        
        handler = TrackingHandler
        
        # Create a custom server class that suppresses connection reset errors
        class QuietHTTPServer(HTTPServer):
            def handle_error(self, request, client_address):
                """Suppress connection reset errors which are normal."""
                import sys
                exc_type, exc_value, exc_traceback = sys.exc_info()
                
                # Suppress connection reset errors (errno 54 on macOS, 104 on Linux)
                if exc_value:
                    # Check for connection reset errors by error code or type
                    if isinstance(exc_value, (ConnectionResetError, BrokenPipeError, ConnectionAbortedError)):
                        # These are normal when clients disconnect - don't log
                        return
                    elif isinstance(exc_value, OSError):
                        # Check error code for connection reset (54 on macOS, 104 on Linux)
                        if hasattr(exc_value, 'errno') and exc_value.errno in (54, 104, 32, 10053):
                            # Connection reset by peer - normal, suppress
                            return
                
                # For other errors, use default handling (but we've already caught most in the handler)
                # Call parent to log unexpected errors
                super().handle_error(request, client_address)
        
        self._server = QuietHTTPServer((self.host, self.port), handler)
        self._server.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.port = self._server.server_address[1]
        
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        
        return self.port
    
    def set_header(self, header: bytes):
        """Set the MP4 header for the stream."""
        self.buffer.set_header(header)
    
    def write(self, data: bytes):
        """Write data to the stream buffer."""
        self.buffer.write(data)
    
    def get_active_connections(self) -> int:
        """Get number of active connections."""
        with self._connection_lock:
            return self._active_connections
    
    def has_active_connections(self) -> bool:
        """Check if there are any active connections."""
        return self.get_active_connections() > 0
    
    def stop(self):
        """Stop the server."""
        if self._server:
            self._server.shutdown()
            self._server = None
    
    def get_url(self) -> str:
        """Get the stream URL."""
        # Get local IP for Chromecast to connect to
        ip = self._get_local_ip()
        return f"http://{ip}:{self.port}/stream.mp4"
    
    def _get_local_ip(self) -> str:
        """Get the local IP address that's reachable from the network."""
        try:
            # Connect to a remote address to determine local IP
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except:
            return "127.0.0.1"


class VideoStreamer:
    """
    High-level interface combining encoding and serving.
    
    Usage:
        streamer = VideoStreamer(width=1280, height=720, fps=10)
        streamer.start()
        print(f"Stream at: {streamer.get_url()}")
        
        while running:
            frame = renderer.render_bytes()
            streamer.write_frame(frame)
        
        streamer.stop()
    """
    
    def __init__(self, width: int = 1280, height: int = 720, fps: int = 10, 
                 port: int = 0, bitrate: str = "1M"):
        self.config = StreamConfig(
            width=width,
            height=height,
            fps=fps,
            bitrate=bitrate,
        )
        self.encoder = FrameEncoder(self.config)
        self.server = StreamServer(port=port)
        self._pump_thread: Optional[threading.Thread] = None
        self._running = False
        self._frames_written = 0
    
    def start(self) -> str:
        """Start encoder and server, returns stream URL."""
        self.server.start()
        self.encoder.start()
        
        self._running = True
        self._pump_thread = threading.Thread(target=self._pump_data, daemon=True)
        self._pump_thread.start()
        
        # Give ffmpeg a moment to initialize
        time.sleep(0.3)
        
        return self.get_url()
    
    def _pump_data(self):
        """Transfer encoded data from encoder to server."""
        header_set = False
        
        while self._running:
            data = self.encoder.read_output(timeout=0.05)  # Shorter timeout for more frequent checks
            if data:
                self.server.write(data)
                
                # Once we have the header, set it on the server
                if not header_set and self.encoder.is_header_ready():
                    self.server.set_header(self.encoder.get_header())
                    header_set = True
            else:
                # Brief sleep if no data to avoid busy-waiting
                time.sleep(0.01)
    
    def write_frame(self, frame_data: bytes):
        """Write a raw RGB frame."""
        self.encoder.write_frame(frame_data)
        self._frames_written += 1
    
    def get_url(self) -> str:
        """Get the stream URL for Chromecast."""
        return self.server.get_url()
    
    @property
    def frames_written(self) -> int:
        """Get number of frames written."""
        return self._frames_written
    
    def stop(self):
        """Stop everything."""
        self._running = False
        self.encoder.stop()
        self.server.stop()


def demo():
    """Demo with generated test frames."""
    from PIL import Image, ImageDraw
    
    streamer = VideoStreamer(width=640, height=480, fps=5)
    url = streamer.start()
    print(f"Stream URL: {url}")
    print("Generating test frames for 30 seconds...")
    
    try:
        for i in range(150):  # 30 seconds at 5 fps
            # Create a test frame
            img = Image.new("RGB", (640, 480), "#1e1e1e")
            draw = ImageDraw.Draw(img)
            draw.text((50, 200), f"Frame {i}", fill="#00ff00")
            draw.rectangle([100, 100, 200 + (i % 100) * 3, 150], fill="#ff0000")
            
            streamer.write_frame(img.tobytes())
            time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    
    streamer.stop()
    print("Done")


if __name__ == "__main__":
    demo()
