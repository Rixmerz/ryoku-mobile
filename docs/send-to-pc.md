# Send to PC

Hand the YouTube video you are watching on the phone over to the computer: it opens in the PC's default
browser at the same minute, in a new window on a new empty workspace, and the PC switches there. The phone's
video is paused so it does not play in two places.

## Use

1. Install the app and enable **Settings > Accessibility > Ryoku: Send to PC** once
   (or `adb shell settings put secure enabled_accessibility_services com.rixmerz.ryokumobile/com.rixmerz.ryokumobile.CastService`).
2. Give the app notification access (the Ryoku app does this through Shizuku; or
   `adb shell cmd notification allow_listener com.rixmerz.ryokumobile/com.rixmerz.ryokumobile.MediaListener`).
   It is what lets the app read the video's minute and pause it. Without it the video is not paused and the
   PC opens it at the start (or at the time the link itself carries).
3. Open YouTube. A small **PC** button appears on the edge of the screen **only while YouTube is in front**;
   drag it to move it. Tap it.
4. A line over the screen says what happened: `Opened on the PC, desktop N`, or why not.

The PC needs `ryoku-link` running (USB cable or the paired Wi-Fi link) and a Hyprland desktop.

## How it works

On the tap the phone pauses the video and notes its minute (`MediaController`, `PcHandoff.kt`), then reads the
video's link off YouTube's own share sheet (`CastService.kt`): it taps YouTube's Share button, scrolls the app
row to its last entry ("More"), which opens Android's chooser, whose header shows the link as text. It reads
that and closes the sheets. The Share button is found by its label in the phone's language (`SHARE_LABELS`);
a language missing there gets `Could not read the video link (no Share button)` and the video resumes. The
manual way always works: YouTube's **Share > Send to PC** (the app registers as a share target,
`SendToPcActivity.kt`) sends the same link.

The phone then sends `{"cmd": "open_url", "url": ..., "position_seconds": ...}` to the bridge. The bridge
(`pc/ryoku_link.py`, "Handing a video over"):

- accepts only `youtube.com`, `www.youtube.com`, `m.youtube.com` and `youtu.be` video links, takes the
  11-character video id out of them and builds `https://www.youtube.com/watch?v=<id>&t=<seconds>s` itself
  from the id and a whole number of seconds. Nothing else the phone sends is passed on, and no shell is used.
- takes the default browser from `xdg-settings get default-web-browser`, reads its `.desktop` file and runs
  its `Exec` line (inside its own `systemd-run --user --scope`, so the browser outlives a restart of
  `ryoku-link`) with `--new-window` added: Chromium-based browsers and Firefox understand it. A browser that
  ignores the flag only opens a tab; the bridge then reports `the browser opened no new window`.
- switches the PC to the lowest workspace with no windows that no monitor shows, starts the browser there,
  waits for the new window, moves it to that workspace if the browser put it elsewhere, and focuses it.

## Limits

- Autoplay is the browser's decision. YouTube's watch page starts playing by itself, but a browser that blocks
  sound before the first click on the site (Chromium's autoplay policy, for a profile that has never played
  YouTube) leaves the video paused on the PC until a click.
- Only YouTube. The share-sheet walk depends on YouTube's own screens and on Android's chooser; a YouTube update
  that renames or reorders them breaks the button (the error line says which step), not the manual
  Share > Send to PC.
- The minute needs notification access (see Use). It is the minute when the button was tapped, to the second.
- Over Wi-Fi the PC cannot reach the phone through adb, which this feature does not need.
