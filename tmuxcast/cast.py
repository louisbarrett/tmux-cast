"""
Chromecast discovery and media control.

Uses pychromecast for device discovery and casting.
"""

import time
import threading
from typing import Optional, List, Callable
from dataclasses import dataclass

try:
    import pychromecast
    from pychromecast.controllers.media import MediaController
    PYCHROMECAST_AVAILABLE = True
except ImportError:
    PYCHROMECAST_AVAILABLE = False


@dataclass
class CastDevice:
    """Represents a discovered Chromecast device."""
    name: str
    uuid: str
    model: str
    host: str
    port: int
    _chromecast: object = None
    
    def __str__(self):
        return f"{self.name} ({self.model}) at {self.host}:{self.port}"


class CastDiscovery:
    """Discovers Chromecast devices on the local network."""
    
    def __init__(self, timeout: float = 10.0):
        if not PYCHROMECAST_AVAILABLE:
            raise ImportError("pychromecast is required: pip install pychromecast")
        self.timeout = timeout
        self._browser = None
        self._devices: dict[str, CastDevice] = {}
        self._lock = threading.Lock()
    
    def discover(self, blocking: bool = True) -> List[CastDevice]:
        """
        Discover Chromecast devices.
        
        Args:
            blocking: If True, wait for timeout then return all devices.
                     If False, start discovery and return immediately.
        """
        chromecasts, browser = pychromecast.get_chromecasts(timeout=self.timeout)
        self._browser = browser
        
        devices = []
        for cc in chromecasts:
            device = CastDevice(
                name=cc.cast_info.friendly_name,
                uuid=str(cc.uuid),
                model=cc.cast_info.model_name,
                host=cc.cast_info.host,
                port=cc.cast_info.port,
                _chromecast=cc,
            )
            devices.append(device)
            with self._lock:
                self._devices[device.uuid] = device
        
        return devices
    
    def get_device_by_name(self, name: str) -> Optional[CastDevice]:
        """Find a device by friendly name (case-insensitive partial match)."""
        name_lower = name.lower()
        with self._lock:
            for device in self._devices.values():
                if name_lower in device.name.lower():
                    return device
        return None
    
    def stop(self):
        """Stop discovery."""
        if self._browser:
            self._browser.stop_discovery()


class CastController:
    """Controls a Chromecast device for media playback."""
    
    def __init__(self, device: CastDevice):
        if not PYCHROMECAST_AVAILABLE:
            raise ImportError("pychromecast is required")
        
        self.device = device
        self._cc = device._chromecast
        self._mc: Optional[MediaController] = None
        self._connected = False
        self._status_callback: Optional[Callable] = None
    
    def connect(self, timeout: float = 30.0) -> bool:
        """Connect to the Chromecast and wait for it to be ready."""
        if self._connected:
            return True
        
        self._cc.wait(timeout=timeout)
        self._mc = self._cc.media_controller
        self._connected = True
        return True
    
    def play_url(self, url: str, content_type: str = "video/mp4", 
                 title: str = "tmux-cast") -> bool:
        """
        Start playing a media URL.
        
        Args:
            url: HTTP URL to the media stream
            content_type: MIME type of the content
            title: Title to display on the Chromecast
        """
        if not self._connected:
            self.connect()
        
        # Stop any existing media first to ensure clean start
        # But be gentle - only stop if actually playing something
        try:
            self._mc.update_status()
            if (self._mc.status and 
                self._mc.status.player_state in ("PLAYING", "BUFFERING") and
                self._mc.status.content_id):  # Only stop if there's actual content
                self._mc.stop()
                # Wait a moment for stop to complete
                import time
                time.sleep(0.3)  # Shorter wait
        except Exception:
            # If stopping fails, continue anyway
            pass
        
        # Start playing the new stream
        self._mc.play_media(
            url,
            content_type,
            title=title,
            stream_type="LIVE",  # Important for our continuous stream
            autoplay=True,
        )
        self._mc.block_until_active(timeout=30)
        
        # Ensure media controller is set up for live streaming
        if self._mc.status:
            # Update status to ensure it's playing
            self._mc.update_status()
            # Force play to ensure it starts
            try:
                if self._mc.status.player_state != "PLAYING":
                    self._mc.play()
            except Exception:
                pass
        
        return True
    
    def stop(self):
        """Stop playback."""
        if self._mc:
            self._mc.stop()
    
    def pause(self):
        """Pause playback."""
        if self._mc:
            self._mc.pause()
    
    def play(self):
        """Resume playback."""
        if self._mc:
            self._mc.play()
    
    @property
    def is_playing(self) -> bool:
        """Check if media is currently playing."""
        if not self._mc:
            return False
        try:
            # Update status to get current state
            self._mc.update_status()
            if not self._mc.status:
                return False
            # Check if playing or buffering (buffering means it's trying to play)
            return self._mc.status.player_is_playing or self._mc.status.player_state == "BUFFERING"
        except Exception:
            return False
    
    @property
    def status(self) -> dict:
        """Get current media status."""
        if not self._mc:
            return {}
        s = self._mc.status
        return {
            "player_state": s.player_state,
            "title": s.title,
            "content_type": s.content_type,
            "duration": s.duration,
            "current_time": s.current_time,
        }
    
    def disconnect(self):
        """Disconnect from the Chromecast."""
        if self._cc:
            self._cc.disconnect()
        self._connected = False


def discover_and_list():
    """Utility function to discover and list all Chromecasts."""
    print("Discovering Chromecast devices...")
    discovery = CastDiscovery(timeout=10)
    devices = discovery.discover()
    
    if not devices:
        print("No Chromecast devices found.")
        return []
    
    print(f"\nFound {len(devices)} device(s):")
    for i, device in enumerate(devices):
        print(f"  [{i}] {device}")
    
    discovery.stop()
    return devices


def quick_cast(url: str, device_name: Optional[str] = None) -> Optional[CastController]:
    """
    Quick helper to discover and cast to a device.
    
    Args:
        url: Media URL to cast
        device_name: Name of device (uses first found if not specified)
    
    Returns:
        CastController if successful, None otherwise
    """
    discovery = CastDiscovery(timeout=10)
    devices = discovery.discover()
    
    if not devices:
        print("No Chromecast devices found")
        return None
    
    if device_name:
        device = discovery.get_device_by_name(device_name)
        if not device:
            print(f"Device '{device_name}' not found")
            print("Available devices:", [d.name for d in devices])
            return None
    else:
        device = devices[0]
        print(f"Using first device: {device.name}")
    
    controller = CastController(device)
    controller.connect()
    controller.play_url(url)
    
    discovery.stop()
    return controller


if __name__ == "__main__":
    discover_and_list()
