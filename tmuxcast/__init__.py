"""
tmux-cast: Stream tmux sessions to Chromecast devices.

Basic usage:
    from tmuxcast import TmuxCast
    
    with TmuxCast() as caster:
        caster.cast_to("Living Room TV")
        input("Press Enter to stop...")

Or manually:
    caster = TmuxCast()
    url = caster.start()
    print(f"Stream at: {url}")
    caster.cast_to("Living Room TV")
    # ... later
    caster.stop()
"""

from .main import TmuxCast, TmuxCastConfig
from .terminal import (
    TmuxCapture, TerminalRenderer, TerminalStyle,
    list_tmux_sessions, list_tmux_windows, list_tmux_panes, select_tmux_target
)
from .stream import VideoStreamer, StreamConfig
from .cast import CastDiscovery, CastController, CastDevice, discover_and_list

__version__ = "0.1.0"
__all__ = [
    "TmuxCast",
    "TmuxCastConfig",
    "TmuxCapture",
    "TerminalRenderer", 
    "TerminalStyle",
    "VideoStreamer",
    "StreamConfig",
    "CastDiscovery",
    "CastController",
    "CastDevice",
    "discover_and_list",
    "list_tmux_sessions",
    "list_tmux_windows",
    "list_tmux_panes",
    "select_tmux_target",
]
