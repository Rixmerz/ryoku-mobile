# Ryoku Mobile

*Ryoku inspired.* An Android companion for a Linux desktop running
[Hyprland](https://hyprland.org): it brings the look and feel of the Ryoku
desktop to the phone and links the two.

## What it does

- **Live desktop view.** Pick a workspace and watch it live on the phone, with
  touch, scroll, keyboard and pinch zoom (the zoom is mirrored on the PC, handy
  for presenting). Workspaces the PC is not showing are rendered on a headless
  Hyprland output, so the PC's own monitor never changes; the one it is showing
  is mirrored. Two-finger swipe switches workspace.
- **Send to PC.** Watching a YouTube video on the phone? Tap the **PC** button
  and it opens on the computer at the same minute, fullscreen, in one reused
  window of your default browser and profile. While it plays there, the phone's
  volume buttons control the PC's volume and a play/pause button controls the
  video. See [docs/send-to-pc.md](docs/send-to-pc.md).
- **USB or Wi-Fi.** The link runs over `adb reverse` on the cable, or over the
  local network with TLS and a pairing token.

## Layout

| Path | What |
|---|---|
| `app/` | The Android app (Kotlin, Jetpack Compose) |
| `pc/ryoku_link.py` | The PC bridge: workspaces, live view, Send to PC (Python, stdlib only) |
| `pc/ryoku_link_wifi.py` | The Wi-Fi relay in front of the bridge |
| `pc/test_ryoku_link.py` | Bridge tests (`python3 -m pytest -q pc`) |

## Requirements

PC: Hyprland (configured through its Lua API), Python 3, `adb`, `wf-recorder`,
`wtype`, `wpctl`, `yt-dlp`. Phone: Android 10+ with USB debugging.

## Build and run

```bash
./gradlew installDebug          # build and install the app on the connected phone
python3 pc/ryoku_link.py        # run the bridge (or install it as a systemd user service)
python3 -m pytest -q pc         # bridge tests
```

## Thanks

Thanks to the Ryoku desktop that inspired this project, to the Hyprland,
Android and Jetpack Compose teams, to the authors of `wf-recorder`, `wtype`,
`yt-dlp` and `scrcpy` (used while exploring), and to the designers of
Fraunces, Space Grotesk and JetBrains Mono, whose fonts the app ships.

## License

[MIT](LICENSE). The bundled fonts keep their own SIL Open Font License 1.1.
