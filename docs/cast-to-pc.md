# Send to PC

Moves the app you are using on the phone (a YouTube video, say) to a new Hyprland workspace on the
PC, where it stays interactive with the mouse and keyboard. Closing the window returns the app to the phone.

## Use

1. Install the app and enable **Settings > Accessibility > Ryoku: Send to PC** once
   (or `adb shell settings put secure enabled_accessibility_services com.rixmerz.ryokumobile/com.rixmerz.ryokumobile.CastService`).
2. A small **PC** button appears on the right edge of every app that has a launcher entry. It is hidden
   on home screens. Drag it to move it; tap it to send the app in front. The Ryoku app need not be open.
3. The phone shows a toast with the PC workspace, or the reason it failed.

The PC needs `scrcpy` (3.x or newer, `sudo pacman -S scrcpy`) and `ryoku-link` running.

## Why a button and not a three-finger gesture

Android only delivers multi-finger accessibility gestures to a service in touch-exploration mode.
Requesting it on this phone (Android 15) set `touch_exploration_enabled=1` system-wide, the TalkBack
mode where a single tap only explores and a double tap clicks. That breaks every app, so the service
asks for no gestures. A button drawn with `TYPE_ACCESSIBILITY_OVERLAY` needs no overlay permission.

## What the PC does

`cast` (see the protocol at the top of `pc/ryoku_link.py`) starts scrcpy with `--new-display=1920x1080/240`
and no `--start-app`, then runs `am display move-stack` on the app's existing task. Starting the app on
the display instead creates a second task and loses its state; moving the task keeps it (a playing video
keeps playing, checked on the Nubia, Android 15).

- Workspace: the lowest id with no windows that no monitor shows. It is placed with
  `exec_cmd(..., { workspace = "N silent" })`, so the PC monitor keeps its workspace and focus.
- Audio: `--audio-source=playback` sends app audio to the PC. Apps that opt out of capture are silent.
  Needs Android 13+.
- Closing the window: scrcpy ends the virtual display. With `--no-vd-destroy-content` (added when this
  scrcpy has it) Android moves the app back to the phone instead of closing it.
- scrcpy's output goes to `$XDG_RUNTIME_DIR/ryoku-link/cast-<package>.log`.
- The app name sent by the phone must look like an installed package (`com.example.app`); the bridge
  checks the pattern, that the phone has it installed and that it is running, and only integers reach
  `am display move-stack`.

## Wi-Fi

scrcpy talks to the phone through adb, so the cast needs adb to see the phone. The Wi-Fi link only
carries the request. USB is used when plugged in; otherwise adb must already be connected over TCP
(`adb tcpip 5555` once with the cable, then `adb connect <phone-ip>:5555`, or Android's Wireless
debugging). The bridge never runs `adb connect` itself. Video over Wi-Fi is also slower and less steady.

## Limits

- One cast per app; a second request for the same app is refused until its window is closed.
- Apps that forbid secondary displays (some banking or DRM apps) are not moved: the bridge reports that
  Android would not move the app, and closes the scrcpy window.
- The virtual display shows the phone vendor's secondary-display launcher beneath the app.
