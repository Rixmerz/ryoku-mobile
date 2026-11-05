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
5. The video opens **fullscreen** on the PC and a **PC session** starts (see below).

## Fullscreen

The PC's player window is one reused browser window. Every send ends with the video filling the whole monitor,
no browser chrome: once the page has loaded the bridge presses YouTube's `f` key in the focused player, and the
browser then asks Hyprland to make the window fullscreen. If `f` has not taken after 1.5 s it is pressed once
more, and if it still has not, Hyprland's own fullscreen is set as a fallback (that one keeps YouTube's page
around the video).

A re-send never types into a fullscreen page (with the player fullscreen, Ctrl+L does not reach the address bar and
the address would be typed into the page as YouTube shortcuts). So every send first takes the window out of
fullscreen (Hyprland's `unset` also ends the page's fullscreen), loads the video, then presses `f` once on the
freshly loaded page. `f` is a toggle and is never pressed twice for one load unless the window still reads as not fullscreen.

## PC volume (the phone's volume buttons)

After a send succeeds, a **PC session** is active: the phone's **volume up / down buttons change the PC's volume**
(5 points a step on the default audio sink, never above 100%) and the phone's own volume does not change. A small
line over the screen shows `PC volume 40%`. The PC button reads **VOL** while the session runs.

The session ends, and the buttons control the phone again, when:

- you **long-press the VOL button** (a tap still sends the video again and keeps the session), or
- **YouTube plays on the phone again**, or
- the PC cannot be reached (cable out, bridge stopped or too old) or cannot change its volume: the buttons are never
  swallowed by a PC that is not there, or
- the accessibility service stops.

Outside a session the volume buttons are untouched, and a ringing or in-call phone always keeps them (they silence
the ringer). This needs the accessibility service's key-event filtering (`canRequestFilterKeyEvents`); it takes
only the two volume keys, only during a session, and every other key goes on to the phone. After updating the app,
check that the service is still on in **Settings > Accessibility**.

The bridge side is `{"cmd": "volume", "step": 1 | -1}`, answered with `{"type": "volume", "level": 40, "muted": false}`;
`volume` is in the state's `features`, so a phone can tell an older bridge.

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
