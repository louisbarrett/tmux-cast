#!/usr/bin/env python3
"""
Interactive demo app for tmux-cast.

Lets you choose both a Chromecast device and a tmux session/window/pane
to stream from.
"""

import sys
import signal
import time
from typing import Optional

from tmuxcast import (
    TmuxCast,
    TmuxCastConfig,
    select_tmux_target,
    CastDiscovery,
    CastDevice,
)


def select_chromecast_device() -> Optional[CastDevice]:
    """
    Interactive selection of a Chromecast device.
    
    Returns:
        Selected CastDevice or None if cancelled
    """
    print("\n" + "=" * 60)
    print("Chromecast Device Selection")
    print("=" * 60)
    
    print("\nDiscovering Chromecast devices...")
    discovery = CastDiscovery(timeout=10)
    
    try:
        devices = discovery.discover()
    except Exception as e:
        print(f"\nError discovering devices: {e}")
        print("Make sure your computer and Chromecast are on the same network.")
        return None
    
    if not devices:
        print("\nNo Chromecast devices found.")
        print("Make sure:")
        print("  - Your Chromecast is powered on")
        print("  - Your computer and Chromecast are on the same network")
        print("  - Multicast/mDNS traffic is allowed")
        return None
    
    print(f"\nFound {len(devices)} Chromecast device(s):")
    for i, device in enumerate(devices):
        print(f"  [{i}] {device.name}")
        print(f"      Model: {device.model}")
        print(f"      Address: {device.host}:{device.port}")
        print()
    
    # Select device
    while True:
        try:
            choice = input("Select device number (or 'q' to quit): ").strip()
            if choice.lower() == 'q':
                return None
            
            device_idx = int(choice)
            if 0 <= device_idx < len(devices):
                selected_device = devices[device_idx]
                print(f"\n✓ Selected: {selected_device.name}")
                return selected_device
            else:
                print(f"Invalid choice. Please enter a number between 0 and {len(devices) - 1}")
        except ValueError:
            print("Invalid input. Please enter a number or 'q' to quit")
        except KeyboardInterrupt:
            print("\n\nCancelled.")
            return None
        finally:
            discovery.stop()


def select_tmux_session() -> Optional[str]:
    """
    Interactive selection of a tmux session/window/pane.
    
    Returns:
        tmux target string or None if cancelled
    """
    print("\n" + "=" * 60)
    print("tmux Session Selection")
    print("=" * 60)
    
    target = select_tmux_target()
    if target:
        print(f"\n✓ Selected target: {target}")
    return target


def main():
    """Main interactive demo."""
    print("\n" + "=" * 60)
    print("tmux-cast Interactive Demo")
    print("=" * 60)
    print("\nThis demo will help you:")
    print("  1. Select a tmux session/window/pane to stream from")
    print("  2. Select a Chromecast device to stream to")
    print("  3. Start streaming")
    print("\nPress Ctrl+C at any time to exit\n")
    
    # Step 1: Select tmux target
    tmux_target = select_tmux_session()
    if not tmux_target:
        print("\nNo tmux target selected. Exiting.")
        return
    
    # Step 2: Select Chromecast device
    chromecast_device = select_chromecast_device()
    if not chromecast_device:
        print("\nNo Chromecast device selected. Exiting.")
        return
    
    # Step 3: Configure and start streaming
    print("\n" + "=" * 60)
    print("Starting Stream")
    print("=" * 60)
    
    config = TmuxCastConfig(
        tmux_target=tmux_target,
        output_width=1920,
        output_height=1080,
        fps=10,
        font_size=20,
    )
    
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
        print("\nInitializing stream...")
        stream_url = caster.start()
        print(f"✓ Stream URL: {stream_url}")
        
        # Cast to selected device
        print(f"\nConnecting to {chromecast_device.name}...")
        caster.cast_to(chromecast_device.name)
        print(f"✓ Casting to: {chromecast_device.name}")
        
        print("\n" + "=" * 60)
        print("Streaming Active!")
        print("=" * 60)
        print(f"\nStreaming from: {tmux_target}")
        print(f"Streaming to: {chromecast_device.name}")
        print(f"Stream URL: {stream_url}")
        print("\nThe stream is now active on your Chromecast.")
        print("Press Ctrl+C to stop streaming.\n")
        
        # Monitor streaming status
        last_status_time = time.time()
        status_interval = 3.0  # Print status every 3 seconds
        
        # Keep running until interrupted
        while caster.is_running:
            time.sleep(0.5)
            
            # Print status periodically
            current_time = time.time()
            if current_time - last_status_time >= status_interval:
                status = caster.get_stream_status()
                is_streaming = caster.is_streaming()
                
                # Status indicator
                status_indicator = "✓ STREAMING" if is_streaming else "⚠ NOT STREAMING"
                print(f"\r[{status_indicator}] Frames: {status['frames_written']}, "
                      f"Connections: {status['active_connections']}, "
                      f"Chromecast: {'Playing' if status.get('chromecast_playing') else 'Idle'}",
                      end="", flush=True)
                
                last_status_time = current_time
            
    except KeyboardInterrupt:
        signal_handler(None, None)
    except Exception as e:
        print(f"\nError: {e}")
        print(f"\nStream URL is still available: {stream_url}")
        print("You can open it manually in a media player.")
        caster.stop()
        sys.exit(1)


if __name__ == "__main__":
    main()

