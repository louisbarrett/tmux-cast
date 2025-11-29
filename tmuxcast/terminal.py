"""
Terminal capture and rendering for tmux sessions.

Uses pyte for ANSI escape sequence handling and Pillow for rasterization.
"""

import subprocess
import pyte
from PIL import Image, ImageDraw, ImageFont
from dataclasses import dataclass
from typing import Optional, List, Tuple, Union
import io


@dataclass
class TerminalStyle:
    """Visual style for terminal rendering."""
    font_size: int = 16
    font_family: str = ""  # Empty string means auto-detect best Unicode-supporting font
    line_height: float = 1.2
    padding: int = 20
    
    # Colors (basic ANSI palette)
    bg_color: str = "#1e1e1e"
    fg_color: str = "#d4d4d4"
    
    # ANSI color palette (0-15)
    palette: tuple = (
        "#000000",  # 0 black
        "#cd0000",  # 1 red
        "#00cd00",  # 2 green
        "#cdcd00",  # 3 yellow
        "#0000ee",  # 4 blue
        "#cd00cd",  # 5 magenta
        "#00cdcd",  # 6 cyan
        "#e5e5e5",  # 7 white
        "#7f7f7f",  # 8 bright black
        "#ff0000",  # 9 bright red
        "#00ff00",  # 10 bright green
        "#ffff00",  # 11 bright yellow
        "#5c5cff",  # 12 bright blue
        "#ff00ff",  # 13 bright magenta
        "#00ffff",  # 14 bright cyan
        "#ffffff",  # 15 bright white
    )


class TmuxCapture:
    """Captures content from a tmux pane."""
    
    def __init__(self, target: str = ""):
        """
        Initialize capture for a tmux target.
        
        Args:
            target: tmux target specification (session:window.pane)
                   Empty string targets the current pane.
        """
        self.target = target
        self._original_target = target  # Store original for recovery
        self._last_error_time = 0.0
        self._error_count = 0
        self._recovery_attempts = 0
    
    def capture_text(self) -> str:
        """Capture pane content as plain text."""
        cmd = ["tmux", "capture-pane", "-p"]
        if self.target:
            cmd.extend(["-t", self.target])
        
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"tmux capture failed: {result.stderr}")
        return result.stdout
    
    def capture_ansi(self) -> str:
        """Capture pane content with ANSI escape sequences."""
        cmd = ["tmux", "capture-pane", "-p", "-e"]  # -e includes escapes
        if self.target:
            cmd.extend(["-t", self.target])
        
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            error_msg = result.stderr.strip()
            
            # Check if it's a session not found error
            if "can't find session" in error_msg.lower() or "can't find pane" in error_msg.lower():
                # Try to recover
                recovered = self._try_recover_target()
                if recovered:
                    # Retry with recovered target
                    cmd = ["tmux", "capture-pane", "-p", "-e"]
                    if self.target:
                        cmd.extend(["-t", self.target])
                    result = subprocess.run(cmd, capture_output=True, text=True)
                    if result.returncode == 0:
                        # Success after recovery - reset error tracking
                        self._error_count = 0
                        self._recovery_attempts = 0
                        return result.stdout
            
            raise RuntimeError(f"tmux capture failed: {error_msg}")
        
        # Reset error tracking on success
        self._error_count = 0
        self._recovery_attempts = 0
        return result.stdout
    
    def _try_recover_target(self) -> bool:
        """
        Try to recover the target by finding the session with a different ID.
        
        Returns:
            True if target was recovered, False otherwise
        """
        import time
        
        # Rate limit recovery attempts (max once per 5 seconds)
        current_time = time.time()
        if current_time - self._last_error_time < 5.0:
            return False
        
        self._last_error_time = current_time
        self._recovery_attempts += 1
        
        # Don't try too many times
        if self._recovery_attempts > 10:
            return False
        
        if not self._original_target:
            # Can't recover empty target
            return False
        
        # Parse the original target: session:window.pane
        parts = self._original_target.split(":")
        if len(parts) != 2:
            return False
        
        original_session = parts[0]
        window_pane = parts[1]
        
        # Try to find the session by name or ID
        sessions = list_tmux_sessions()
        
        # First, try to find by original session ID/name
        for session_id, session_name in sessions:
            if session_id == original_session or session_name == original_session:
                # Found it! Update target
                self.target = f"{session_id}:{window_pane}"
                return True
        
        # If not found, try to use the first available session as fallback
        # (only if we've tried a few times and original is definitely gone)
        if self._recovery_attempts >= 3 and sessions:
            # Use first available session as fallback
            session_id, _ = sessions[0]
            self.target = f"{session_id}:{window_pane}"
            return True
        
        return False
    
    def is_target_valid(self) -> bool:
        """Check if the current target is valid."""
        if not self.target:
            # Empty target is always valid (current pane)
            return True
        
        try:
            # Try to get pane size as a validation check
            self.get_pane_size()
            return True
        except RuntimeError:
            return False
    
    def get_pane_size(self) -> tuple[int, int]:
        """Get pane dimensions (width, height) in characters."""
        cmd = ["tmux", "display-message", "-p", "#{pane_width} #{pane_height}"]
        if self.target:
            cmd.extend(["-t", self.target])
        
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"tmux size query failed: {result.stderr}")
        
        width, height = result.stdout.strip().split()
        return int(width), int(height)


def list_tmux_sessions() -> List[Tuple[str, str]]:
    """
    List all tmux sessions.
    
    Returns:
        List of (session_id, session_name) tuples
    """
    try:
        # Get session list: format is session_id:session_name
        cmd = ["tmux", "list-sessions", "-F", "#{session_id}:#{session_name}"]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        
        sessions = []
        for line in result.stdout.strip().split('\n'):
            if not line:
                continue
            parts = line.split(':', 1)
            if len(parts) == 2:
                sessions.append((parts[0], parts[1]))
        
        return sessions
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []


def list_tmux_windows(session_id: str) -> List[Tuple[str, str]]:
    """
    List all windows in a tmux session.
    
    Args:
        session_id: Session ID (e.g., "0" or session name)
    
    Returns:
        List of (window_id, window_name) tuples
    """
    try:
        cmd = ["tmux", "list-windows", "-t", session_id, "-F", "#{window_index}:#{window_name}"]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        
        windows = []
        for line in result.stdout.strip().split('\n'):
            if not line:
                continue
            parts = line.split(':', 1)
            if len(parts) == 2:
                windows.append((parts[0], parts[1]))
        
        return windows
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []


def list_tmux_panes(session_id: str, window_id: str) -> List[Tuple[str, str]]:
    """
    List all panes in a tmux window.
    
    Args:
        session_id: Session ID (e.g., "0" or session name)
        window_id: Window index (e.g., "0")
    
    Returns:
        List of (pane_id, pane_title) tuples
    """
    try:
        target = f"{session_id}:{window_id}"
        cmd = ["tmux", "list-panes", "-t", target, "-F", "#{pane_index}:#{pane_title}"]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        
        panes = []
        for line in result.stdout.strip().split('\n'):
            if not line:
                continue
            parts = line.split(':', 1)
            if len(parts) == 2:
                panes.append((parts[0], parts[1]))
            else:
                # Some panes might not have titles
                panes.append((parts[0], ""))
        
        return panes
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []


def select_tmux_target() -> Optional[str]:
    """
    Interactive selection of a tmux target (session:window.pane).
    
    Returns:
        tmux target string (e.g., "mysession:0.1") or None if cancelled
    """
    # Check if tmux is available
    try:
        subprocess.run(["tmux", "-V"], capture_output=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("Error: tmux is not installed or not in PATH")
        return None
    
    # List sessions
    sessions = list_tmux_sessions()
    if not sessions:
        print("No tmux sessions found.")
        print("Start a tmux session first: tmux new -s mysession")
        return None
    
    print("\nAvailable tmux sessions:")
    for i, (session_id, session_name) in enumerate(sessions):
        print(f"  [{i}] {session_name} (id: {session_id})")
    
    # Select session
    while True:
        try:
            choice = input("\nSelect session number (or 'q' to quit): ").strip()
            if choice.lower() == 'q':
                return None
            
            session_idx = int(choice)
            if 0 <= session_idx < len(sessions):
                selected_session_id, selected_session_name = sessions[session_idx]
                break
            else:
                print(f"Invalid choice. Please enter a number between 0 and {len(sessions) - 1}")
        except ValueError:
            print("Invalid input. Please enter a number or 'q' to quit")
        except KeyboardInterrupt:
            print("\nCancelled.")
            return None
    
    # List windows
    windows = list_tmux_windows(selected_session_id)
    if not windows:
        print(f"No windows found in session '{selected_session_name}'")
        return None
    
    print(f"\nAvailable windows in '{selected_session_name}':")
    for i, (window_id, window_name) in enumerate(windows):
        print(f"  [{i}] {window_name} (index: {window_id})")
    
    # Select window
    while True:
        try:
            choice = input("\nSelect window number (or 'q' to quit): ").strip()
            if choice.lower() == 'q':
                return None
            
            window_idx = int(choice)
            if 0 <= window_idx < len(windows):
                selected_window_id, selected_window_name = windows[window_idx]
                break
            else:
                print(f"Invalid choice. Please enter a number between 0 and {len(windows) - 1}")
        except ValueError:
            print("Invalid input. Please enter a number or 'q' to quit")
        except KeyboardInterrupt:
            print("\nCancelled.")
            return None
    
    # List panes
    panes = list_tmux_panes(selected_session_id, selected_window_id)
    if not panes:
        print(f"No panes found in window '{selected_window_name}'")
        return None
    
    # If only one pane, auto-select it
    if len(panes) == 1:
        pane_id, _ = panes[0]
        target = f"{selected_session_id}:{selected_window_id}.{pane_id}"
        print(f"\nAuto-selected pane {pane_id} (only pane in window)")
        return target
    
    print(f"\nAvailable panes in '{selected_window_name}':")
    for i, (pane_id, pane_title) in enumerate(panes):
        title_str = f" - {pane_title}" if pane_title else ""
        print(f"  [{i}] Pane {pane_id}{title_str}")
    
    # Select pane
    while True:
        try:
            choice = input("\nSelect pane number (or 'q' to quit): ").strip()
            if choice.lower() == 'q':
                return None
            
            pane_idx = int(choice)
            if 0 <= pane_idx < len(panes):
                selected_pane_id, _ = panes[pane_idx]
                target = f"{selected_session_id}:{selected_window_id}.{selected_pane_id}"
                print(f"\nSelected target: {target}")
                return target
            else:
                print(f"Invalid choice. Please enter a number between 0 and {len(panes) - 1}")
        except ValueError:
            print("Invalid input. Please enter a number or 'q' to quit")
        except KeyboardInterrupt:
            print("\nCancelled.")
            return None


class TerminalRenderer:
    """Renders terminal content to images."""
    
    def __init__(self, cols: int = 80, rows: int = 24, style: Optional[TerminalStyle] = None):
        self.cols = cols
        self.rows = rows
        self.style = style or TerminalStyle()
        
        # Initialize pyte screen for ANSI parsing
        self.screen = pyte.Screen(cols, rows)
        self.screen.set_mode(pyte.modes.LNM)  # Make \n work as CRLF
        self.stream = pyte.Stream(self.screen)
        
        # Load font
        self._font = self._load_font()
        self._char_width, self._char_height = self._measure_char()
    
    def _load_font(self) -> ImageFont.FreeTypeFont:
        """Load a monospace font with Unicode box-drawing character support."""
        import platform
        
        # Try the specified font first (if provided and not empty)
        if self.style.font_family:
            try:
                font = ImageFont.truetype(self.style.font_family, self.style.font_size)
                # Verify it supports box-drawing characters
                try:
                    bbox = font.getbbox("├")
                    if bbox[2] > bbox[0]:  # width > 0
                        return font
                except:
                    pass
            except OSError:
                pass
        
        # System-specific fonts that support Unicode box-drawing characters
        system = platform.system()
        font_candidates = []
        
        if system == "Darwin":  # macOS
            font_candidates = [
                "/System/Library/Fonts/Menlo.ttc",  # Menlo supports Unicode well
                "/System/Library/Fonts/Monaco.dfont",
                "/Library/Fonts/Courier New.ttf",
                "/System/Library/Fonts/Supplemental/Courier New.ttf",
            ]
        elif system == "Linux":
            font_candidates = [
                "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
                "/usr/share/fonts/TTF/DejaVuSansMono.ttf",
                "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
                "/usr/share/fonts/truetype/noto/NotoMono-Regular.ttf",
            ]
        elif system == "Windows":
            font_candidates = [
                "C:/Windows/Fonts/consola.ttf",  # Consolas
                "C:/Windows/Fonts/cour.ttf",  # Courier New
            ]
        
        # Add common fallbacks (try by name, Pillow will find them)
        font_candidates.extend([
            "Menlo",  # macOS system font
            "Monaco",  # macOS system font
            "DejaVuSansMono.ttf",
            "Liberation Mono",
            "Noto Mono",
            "Courier New",
            "Courier",
        ])
        
        # Try each candidate
        for font_path in font_candidates:
            try:
                font = ImageFont.truetype(font_path, self.style.font_size)
                # Test if font supports box-drawing characters
                try:
                    bbox = font.getbbox("├")
                    # If we get a valid bbox, the font supports the character
                    if bbox[2] > bbox[0]:  # width > 0
                        return font
                except Exception:
                    # Font loaded but might not support the character, try next
                    continue
            except (OSError, IOError):
                continue
        
        # Last resort: default font (may not support Unicode well)
        return ImageFont.load_default()
    
    def _measure_char(self) -> tuple[int, int]:
        """Measure character dimensions for the loaded font."""
        # Use a representative character
        bbox = self._font.getbbox("M")
        width = bbox[2] - bbox[0]
        height = int(self.style.font_size * self.style.line_height)
        return width, height
    
    def feed(self, data: str):
        """Feed ANSI data into the terminal emulator."""
        self.screen.reset()
        self.screen.set_mode(pyte.modes.LNM)  # Re-enable after reset
        self.stream.feed(data)
    
    def render(self) -> Image.Image:
        """Render current screen state to an image."""
        # Calculate image dimensions
        img_width = self.cols * self._char_width + 2 * self.style.padding
        img_height = self.rows * self._char_height + 2 * self.style.padding
        
        # Create image
        img = Image.new("RGB", (img_width, img_height), self.style.bg_color)
        draw = ImageDraw.Draw(img)
        
        # Render each character - iterate over all rows and columns to ensure nothing is cut off
        for y in range(self.rows):
            # Safely get the line, use empty dict if row doesn't exist (defensive)
            line = self.screen.buffer.get(y, {})
            
            # Render all characters that exist in the line
            # Iterate over all characters in the line to ensure we don't miss any
            for x, char in line.items():
                # Only render if within expected column range (safety check)
                if x < 0 or x >= self.cols:
                    continue
                
                px = self.style.padding + x * self._char_width
                py = self.style.padding + y * self._char_height
                
                # Ensure we don't draw outside image bounds (safety check)
                if px < 0 or py < 0 or px + self._char_width > img_width or py + self._char_height > img_height:
                    continue
                
                # Get colors
                fg = self._resolve_color(char.fg, default=self.style.fg_color)
                bg = self._resolve_color(char.bg, default=self.style.bg_color)
                
                # Handle reverse video
                if char.reverse:
                    fg, bg = bg, fg
                
                # Draw background if not default
                if bg != self.style.bg_color:
                    draw.rectangle(
                        [px, py, px + self._char_width, py + self._char_height],
                        fill=bg
                    )
                
                # Draw character
                if char.data and char.data != " ":
                    draw.text((px, py), char.data, font=self._font, fill=fg)
        
        # Draw cursor if visible
        if self.screen.cursor.x < self.cols and self.screen.cursor.y < self.rows:
            cx = self.style.padding + self.screen.cursor.x * self._char_width
            cy = self.style.padding + self.screen.cursor.y * self._char_height
            draw.rectangle(
                [cx, cy, cx + self._char_width, cy + self._char_height],
                outline=self.style.fg_color
            )
        
        return img
    
    def _resolve_color(self, color: Union[str, int, tuple, list], default: str) -> str:
        """
        Resolve a pyte color to a hex color string.
        
        Supports:
        - Basic 16-color palette (0-15)
        - 256-color mode (16-255)
        - True color (24-bit RGB) as hex strings or tuples
        - Color names (black, red, etc.)
        """
        if color == "default":
            return default
        
        # Handle color names
        color_map = {
            "black": 0, "red": 1, "green": 2, "yellow": 3,
            "blue": 4, "magenta": 5, "cyan": 6, "white": 7,
            "brightblack": 8, "brightred": 9, "brightgreen": 10,
            "brightyellow": 11, "brightblue": 12, "brightmagenta": 13,
            "brightcyan": 14, "brightwhite": 15,
        }
        
        if isinstance(color, str) and color in color_map:
            return self.style.palette[color_map[color]]
        
        # Handle hex color strings (true color)
        if isinstance(color, str) and color.startswith("#"):
            # Validate hex color format
            if len(color) == 7 and all(c in "0123456789abcdefABCDEF" for c in color[1:]):
                return color.lower()
            elif len(color) == 4:  # Short hex format #RGB
                # Expand to #RRGGBB
                return f"#{color[1]}{color[1]}{color[2]}{color[2]}{color[3]}{color[3]}".lower()
        
        # Handle RGB tuples/lists (true color)
        if isinstance(color, (tuple, list)) and len(color) == 3:
            r, g, b = color
            if all(isinstance(c, int) and 0 <= c <= 255 for c in (r, g, b)):
                return f"#{r:02x}{g:02x}{b:02x}"
        
        # Handle integer color indices (256-color mode)
        # Try to parse as integer (handles both int and string representations)
        if isinstance(color, int):
            idx = color
        elif isinstance(color, str) and color.isdigit():
            try:
                idx = int(color)
            except ValueError:
                idx = None
        else:
            idx = None
        
        if idx is not None:
            # Basic 16-color palette (0-15)
            if 0 <= idx < 16:
                return self.style.palette[idx]
            
            # 256-color mode (16-255)
            if 16 <= idx <= 255:
                return self._color_256_to_hex(idx)
        
        return default
    
    def _color_256_to_hex(self, idx: int) -> str:
        """
        Convert a 256-color index to hex RGB.
        
        Color scheme:
        - 0-15: Standard palette (handled separately)
        - 16-231: 6x6x6 color cube (216 colors)
        - 232-255: Grayscale (24 shades)
        """
        if idx < 16 or idx > 255:
            return self.style.palette[0]  # Fallback to black
        
        if 16 <= idx <= 231:
            # 6x6x6 color cube
            # Colors are arranged as: 16 + 36*r + 6*g + b
            # where r, g, b are in range [0, 5]
            idx -= 16
            r = idx // 36
            g = (idx // 6) % 6
            b = idx % 6
            
            # Convert to 0-255 range
            # 0 -> 0, 1 -> 95, 2 -> 135, 3 -> 175, 4 -> 215, 5 -> 255
            r_val = self._color_cube_value(r)
            g_val = self._color_cube_value(g)
            b_val = self._color_cube_value(b)
            
            return f"#{r_val:02x}{g_val:02x}{b_val:02x}"
        
        elif 232 <= idx <= 255:
            # Grayscale
            # 232 -> 8, 233 -> 18, ..., 255 -> 238
            gray = 8 + (idx - 232) * 10
            if gray > 255:
                gray = 255
            return f"#{gray:02x}{gray:02x}{gray:02x}"
        
        return self.style.palette[0]  # Fallback
    
    def _color_cube_value(self, level: int) -> int:
        """Convert color cube level (0-5) to RGB value (0-255)."""
        # Standard 256-color cube mapping
        if level == 0:
            return 0
        elif level == 1:
            return 95
        elif level == 2:
            return 135
        elif level == 3:
            return 175
        elif level == 4:
            return 215
        else:  # level == 5
            return 255
    
    def render_bytes(self, format: str = "RGB") -> bytes:
        """Render to raw bytes (for ffmpeg input)."""
        img = self.render()
        if format == "RGB":
            return img.tobytes()
        else:
            buf = io.BytesIO()
            img.save(buf, format=format)
            return buf.getvalue()
    
    @property
    def image_size(self) -> tuple[int, int]:
        """Get rendered image dimensions."""
        width = self.cols * self._char_width + 2 * self.style.padding
        height = self.rows * self._char_height + 2 * self.style.padding
        return width, height


def demo():
    """Quick demo of terminal rendering."""
    # Create some ANSI test content
    test_content = "\033[32mHello\033[0m \033[1;31mWorld\033[0m!\n"
    test_content += "\033[44;37m  Blue background  \033[0m\n"
    test_content += "Normal text here\n"
    test_content += "\033[7mReverse video\033[0m"
    
    renderer = TerminalRenderer(cols=40, rows=10)
    renderer.feed(test_content)
    img = renderer.render()
    img.save("/tmp/terminal_demo.png")
    print("Saved demo to /tmp/terminal_demo.png")


if __name__ == "__main__":
    demo()
