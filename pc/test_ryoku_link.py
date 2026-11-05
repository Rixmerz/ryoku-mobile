"""Run with: python3 -m unittest discover -s pc"""

import asyncio
import json
import os
import shlex
import signal
import stat
import struct
import tempfile
import unittest
from unittest import mock

import ryoku_link


class WireFormat(unittest.TestCase):
    def test_message_roundtrip(self):
        raw = ryoku_link.pack(7, 3, struct.pack("<I", 42)) + ryoku_link.pack(8, 1)
        msgs, rest = ryoku_link.unpack(raw + b"\x01\x02")
        self.assertEqual(msgs, [(7, 3, struct.pack("<I", 42)), (8, 1, b"")])
        self.assertEqual(rest, b"\x01\x02")

    def test_strings_are_nul_terminated_and_padded(self):
        enc = ryoku_link._string("wl_output")  # 9 chars + NUL = 10 -> padded to 12
        self.assertEqual(struct.unpack_from("<I", enc)[0], 10)
        self.assertEqual(len(enc), 4 + 12)
        self.assertEqual(ryoku_link._read_string(enc, 0), ("wl_output", len(enc)))

    def test_short_message_is_rejected(self):
        with self.assertRaises(ValueError):
            ryoku_link.unpack(struct.pack("<II", 1, 4 << 16))


class OutputSize(unittest.TestCase):
    def test_tall_phone_is_scaled_to_max_side_with_even_sides(self):
        self.assertEqual(ryoku_link.output_size(1080, 2400), (1920, 864))

    def test_small_screen_is_kept(self):
        self.assertEqual(ryoku_link.output_size(720, 1280), (1280, 720))

    def test_odd_sizes_become_even(self):
        w, h = ryoku_link.output_size(721, 1281)
        self.assertEqual((w % 2, h % 2), (0, 0))


def monitors(pc_workspace=6, phone_workspace=None, phone_focused=False):
    out = [{"name": "eDP-1", "focused": not phone_focused, "activeWorkspace": {"id": pc_workspace}, "x": 0, "y": 0, "width": 1920, "height": 1080}]
    if phone_workspace is not None:
        out.append({"name": ryoku_link.OUTPUT, "focused": phone_focused, "activeWorkspace": {"id": phone_workspace}, "x": 1536, "y": 0, "width": 864, "height": 1920})
    return out


def fake_hyprctl(mons, workspaces):
    def run(*args):
        return {"monitors": mons, "workspaces": workspaces, "cursorpos": {"x": 10, "y": 20}}[args[0]]
    return run


class MirrorDecision(unittest.TestCase):
    def test_a_workspace_a_pc_monitor_displays_is_mirrored(self):
        self.assertEqual(ryoku_link.mirror_target(6, {"eDP-1": 6, "HDMI-A-1": 2}), "eDP-1")
        self.assertEqual(ryoku_link.mirror_target(2, {"eDP-1": 6, "HDMI-A-1": 2}), "HDMI-A-1")

    def test_a_workspace_no_pc_monitor_displays_is_not_mirrored(self):
        self.assertIsNone(ryoku_link.mirror_target(3, {"eDP-1": 6}))

    def test_mirror_size_keeps_the_monitor_aspect_and_caps_the_long_side(self):
        self.assertEqual(ryoku_link.fit_size(1920, 1080), (1920, 1080))
        self.assertEqual(ryoku_link.fit_size(3840, 2160), (1920, 1080))
        self.assertEqual(ryoku_link.fit_size(1080, 1920), (1080, 1920))  # a portrait monitor stays portrait


class ApplyWorkspace(unittest.TestCase):
    def setUp(self):
        self.view = ryoku_link.PhoneView(None, 1080, 2400)
        self.view.pointer = mock.Mock()

    def apply(self, ws, mons, workspaces, from_pc=False):
        self.view.ws = ws
        with mock.patch.object(ryoku_link, "hyprctl", fake_hyprctl(mons, workspaces)), \
                mock.patch.object(ryoku_link, "dispatch", return_value=True) as d, \
                mock.patch.object(ryoku_link, "VirtualPointer") as pointer:
            return self.view.apply(from_pc), d, pointer

    def test_workspace_on_the_pc_monitor_is_mirrored_without_moving_anything(self):
        changed, d, pointer = self.apply(6, monitors(6, 4), [{"id": 6, "monitor": "eDP-1"}])
        self.assertTrue(changed)
        self.assertEqual((self.view.mirror, self.view.source), ("eDP-1", "eDP-1"))
        self.assertEqual(self.view.look(), ("eDP-1", 1920, 1080))
        d.assert_not_called()
        pointer.assert_called_once_with("eDP-1")  # the phone's input is bound to the real monitor

    def test_free_workspace_is_pulled_on_the_phone_output_and_focus_goes_back(self):
        changed, d, pointer = self.apply(3, monitors(6, 4), [{"id": 3, "monitor": "eDP-1"}, {"id": 6, "monitor": "eDP-1"}, {"id": 4, "monitor": ryoku_link.OUTPUT}])
        self.assertFalse(changed)  # still the headless output
        self.assertIsNone(self.view.mirror)
        steps = d.call_args.args
        self.assertEqual(steps[0], ryoku_link.focus_monitor(ryoku_link.OUTPUT))
        self.assertIn(ryoku_link.focus_workspace(3, on_current_monitor=True), steps)
        self.assertEqual(steps[-2], ryoku_link.focus_monitor("eDP-1"))  # seat handed back (then the cursor)
        self.assertEqual(self.view.pulled, {3: "eDP-1"})
        pointer.assert_not_called()

    def test_leaving_a_pulled_workspace_sends_it_home_and_keeps_what_the_pc_shows(self):
        self.view.pulled = {3: "eDP-1"}
        # the PC moved to 5 since 3 was pulled: 5 is restored, not the 6 shown back then
        changed, d, _ = self.apply(7, monitors(5, 3), [{"id": 3, "monitor": ryoku_link.OUTPUT}, {"id": 7, "monitor": "eDP-1"}, {"id": 5, "monitor": "eDP-1"}])
        steps = d.call_args.args
        self.assertEqual(steps[1:5], tuple(ryoku_link.PhoneView._send_home("eDP-1", 5)))
        self.assertEqual(self.view.pulled, {7: "eDP-1"})

    def test_swiping_from_a_pulled_workspace_to_the_pc_one_hands_the_pulled_one_back_and_mirrors(self):
        self.view.pulled = {3: "eDP-1"}
        changed, d, pointer = self.apply(5, monitors(5, 3), [{"id": 3, "monitor": ryoku_link.OUTPUT}, {"id": 5, "monitor": "eDP-1"}])
        self.assertTrue(changed)
        self.assertEqual(self.view.mirror, "eDP-1")
        self.assertEqual(d.call_args.args[1:5], tuple(ryoku_link.PhoneView._send_home("eDP-1", 5)))  # the PC keeps 5
        self.assertEqual(self.view.pulled, {})

    def test_mirroring_a_monitor_then_a_free_workspace_goes_back_to_the_headless_output(self):
        self.apply(6, monitors(6, 4), [{"id": 6, "monitor": "eDP-1"}])
        self.view.pointer_on = "eDP-1"
        changed, _, pointer = self.apply(3, monitors(6, 4), [{"id": 3, "monitor": "eDP-1"}, {"id": 4, "monitor": ryoku_link.OUTPUT}])
        self.assertTrue(changed)
        self.assertIsNone(self.view.mirror)
        pointer.assert_called_once_with(ryoku_link.OUTPUT)

    def test_pc_taking_the_workspace_the_phone_holds_gets_it_back_and_the_phone_mirrors(self):
        self.view.pulled = {3: "eDP-1"}
        # Hyprland answered the PC's switch by focusing the headless output, where 3 is shown
        changed, d, _ = self.apply(3, monitors(6, 3, phone_focused=True), [{"id": 3, "monitor": ryoku_link.OUTPUT}, {"id": 6, "monitor": "eDP-1"}], from_pc=True)
        self.assertTrue(changed)
        self.assertEqual(self.view.mirror, "eDP-1")
        d.assert_called_once_with(ryoku_link.focus_monitor(ryoku_link.OUTPUT), ryoku_link.move_current_workspace("eDP-1"), ryoku_link.focus_monitor("eDP-1"))
        self.assertEqual(self.view.pulled, {})

    def test_the_phone_borrowing_the_seat_is_not_a_pc_takeover(self):
        self.view.pulled = {3: "eDP-1"}
        self.view.rest = ("eDP-1", 10, 20)
        changed, d, _ = self.apply(3, monitors(6, 3, phone_focused=True), [{"id": 3, "monitor": ryoku_link.OUTPUT}], from_pc=True)
        self.assertFalse(changed)
        d.assert_not_called()

    def test_the_phones_own_switch_is_not_a_pc_takeover(self):
        changed, d, _ = self.apply(3, monitors(6, 3, phone_focused=True), [{"id": 3, "monitor": ryoku_link.OUTPUT}])
        self.assertFalse(changed)
        d.assert_not_called()

    def test_reapplying_a_settled_view_changes_nothing(self):
        mons, ws = monitors(6, 4), [{"id": 6, "monitor": "eDP-1"}]
        self.apply(6, mons, ws)
        self.view.pointer_on = "eDP-1"
        changed, d, pointer = self.apply(6, mons, ws, from_pc=True)
        self.assertFalse(changed)
        d.assert_not_called()
        pointer.assert_not_called()

    def test_pc_leaving_the_mirrored_workspace_moves_the_phone_to_the_headless_output(self):
        self.apply(6, monitors(6, 4), [{"id": 6, "monitor": "eDP-1"}])
        self.view.pointer_on = "eDP-1"
        changed, d, _ = self.apply(6, monitors(2, 4), [{"id": 6, "monitor": "eDP-1"}, {"id": 2, "monitor": "eDP-1"}, {"id": 4, "monitor": ryoku_link.OUTPUT}], from_pc=True)
        self.assertTrue(changed)
        self.assertIsNone(self.view.mirror)
        self.assertIn(ryoku_link.focus_workspace(6, on_current_monitor=True), d.call_args.args)


class Input(unittest.TestCase):
    def setUp(self):
        self.view = ryoku_link.PhoneView(None, 1080, 2400)
        self.view.pointer = mock.Mock()
        self.view.remember_pc = mock.Mock()

    def test_pointer_down_moves_then_presses(self):
        self.view.pointer_event({"action": "down", "x": 0.25, "y": 0.5})
        self.assertEqual([c[0] for c in self.view.pointer.method_calls], ["move_to", "button"])
        self.assertEqual(self.view.pressed, {"left"})
        self.view.pointer_event({"action": "up", "x": 0.25, "y": 0.5})
        self.assertEqual(self.view.pressed, set())

    def test_bad_pointer_events_are_dropped(self):
        for bad in ({"action": "down", "x": "a", "y": 0}, {"action": "teleport", "x": 0, "y": 0},
                    {"action": "down", "x": 0, "y": 0, "button": "side"}, {"action": "move", "x": float("nan"), "y": 0}):
            self.view.pointer_event(bad)
        self.view.pointer.move_to.assert_not_called()

    def wtype_argv(self, msg):
        with mock.patch.object(ryoku_link, "dispatch"), \
                mock.patch.object(ryoku_link.subprocess, "run", return_value=mock.Mock(returncode=0)) as run:
            self.view.key_event(msg)
        return run.call_args.args[0] if run.called else None

    def test_text_goes_after_a_double_dash_so_it_cannot_be_an_option(self):
        self.assertEqual(self.wtype_argv({"text": "--help"}), ["wtype", "--", "--help"])

    def test_shortcut_wraps_the_key_in_its_modifiers(self):
        self.assertEqual(self.wtype_argv({"key": "c", "mods": ["ctrl", "shift"]}),
                         ["wtype", "-M", "ctrl", "-M", "shift", "-k", "c", "-m", "shift", "-m", "ctrl"])

    def test_unknown_key_names_never_reach_wtype(self):
        self.assertIsNone(self.wtype_argv({"key": "rm -rf"}))
        self.assertIsNone(self.wtype_argv({"key": "-M"}))


class Zoom(unittest.TestCase):
    def setUp(self):
        self.view = ryoku_link.PhoneView(None, 1080, 2400)
        self.view.pointer = mock.Mock()
        self.view.remember_pc = mock.Mock()
        self.view.ws = 6
        self.real_set_zoom = ryoku_link.set_zoom
        patch = mock.patch.object(ryoku_link, "set_zoom", return_value=True)
        self.set_zoom = patch.start()
        self.addCleanup(patch.stop)

    def test_zoom_centres_the_pointer_then_zooms(self):
        self.view.zoom_event({"factor": 2.5, "x": 0.3, "y": 0.7})
        self.view.pointer.move_to.assert_called_once_with(0.3, 0.7)
        self.set_zoom.assert_called_once_with(2.5)
        self.assertEqual(self.view.zoomed_ws, 6)

    def test_factor_is_clamped_to_the_supported_range(self):
        self.assertEqual(ryoku_link.clamp_zoom(100), ryoku_link.MAX_ZOOM)
        self.assertEqual(ryoku_link.clamp_zoom(0.2), 1.0)
        with mock.patch.object(ryoku_link.subprocess, "run", return_value=mock.Mock(stdout="ok\n")) as run:
            self.real_set_zoom(100)
        self.assertIn("zoom_factor = 8.000", run.call_args.args[0][-1])

    def test_bad_zoom_messages_are_dropped(self):
        for bad in ({"factor": "2", "x": 0, "y": 0}, {"factor": float("nan"), "x": 0, "y": 0},
                    {"factor": 2, "x": None, "y": 0}, {"factor": True, "x": 0, "y": 0}, {}):
            self.view.zoom_event(bad)
        self.set_zoom.assert_not_called()
        self.view.pointer.move_to.assert_not_called()

    def test_factor_one_switches_the_zoom_off(self):
        self.view.zoom_event({"factor": 3, "x": 0.5, "y": 0.5})
        self.view.zoom_event({"factor": 1.0, "x": 0.5, "y": 0.5})
        self.assertEqual(self.set_zoom.call_args_list[-1], mock.call(1.0))
        self.assertIsNone(self.view.zoomed_ws)

    def test_nothing_is_reset_when_never_zoomed(self):
        self.view.reset_zoom()
        self.set_zoom.assert_not_called()

    def test_closing_the_view_resets_the_zoom(self):
        self.view.zoom_event({"factor": 3, "x": 0.5, "y": 0.5})
        with mock.patch.object(ryoku_link, "dispatch"), mock.patch.object(ryoku_link, "hyprctl", return_value=[]), \
                mock.patch.object(ryoku_link.subprocess, "run"):
            self.view.close()
        self.assertEqual(self.set_zoom.call_args_list[-1], mock.call(1.0))

    def test_closing_the_view_resets_the_zoom_even_when_it_was_never_tracked(self):
        # A zoom left by an earlier run, or by a failed reset, is not in zoomed_ws: close still clears it.
        self.assertIsNone(self.view.zoomed_ws)
        with mock.patch.object(ryoku_link, "dispatch"), mock.patch.object(ryoku_link, "hyprctl", return_value=[]), \
                mock.patch.object(ryoku_link.subprocess, "run"):
            self.view.close()
        self.set_zoom.assert_called_once_with(1.0)

    def test_changing_workspace_resets_the_zoom(self):
        self.view.zoom_event({"factor": 3, "x": 0.5, "y": 0.5})
        self.view.ws = 4
        with mock.patch.object(ryoku_link, "hyprctl", fake_hyprctl(monitors(6, 4), [{"id": 6, "monitor": "eDP-1"}])), \
                mock.patch.object(ryoku_link, "dispatch", return_value=True), mock.patch.object(ryoku_link, "VirtualPointer"):
            self.view.apply()
        self.assertEqual(self.set_zoom.call_args_list[-1], mock.call(1.0))


class Capabilities(unittest.TestCase):
    def test_the_state_tells_the_phone_this_bridge_can_cast(self):
        def hypr(*args):
            return {"monitors": monitors(1), "workspaces": [], "clients": []}[args[0]]

        with mock.patch.object(ryoku_link, "hyprctl", hypr), mock.patch.object(ryoku_link, "view", None):
            self.assertIn("cast", ryoku_link.snapshot()["features"])


class PrivateSocket(unittest.TestCase):
    def test_socket_is_owner_only_and_a_second_instance_is_refused(self):
        async def scenario(directory):
            async def noop(reader, writer):
                writer.close()
                await writer.wait_closed()

            first = await ryoku_link.serve_private(noop, 4242)
            try:
                path = ryoku_link.socket_path(4242)
                self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(os.stat(directory).st_mode), 0o700)
                with self.assertRaises(SystemExit):
                    await ryoku_link.serve_private(noop, 4242)
            finally:
                first.close()
                await first.wait_closed()

        with tempfile.TemporaryDirectory() as tmp:
            directory = os.path.join(tmp, "ryoku-link")
            with mock.patch.object(ryoku_link, "SOCKET_DIR", directory):
                asyncio.run(scenario(directory))


class VideoConnection(unittest.TestCase):
    """The phone only draws what arrives on the video socket: a connection that starts no encoder is a black screen."""

    def test_video_connection_records_the_headless_output_and_relays_the_bytes(self):
        class FakeEncoder:
            returncode = 0

            def __init__(self):
                self.stdout = asyncio.StreamReader()
                self.stdout.feed_data(b"\x00\x00\x00\x01frame")
                self.stdout.feed_eof()

            async def wait(self):
                return 0

        class FakeWriter:
            def __init__(self):
                self.sent, self.closed = b"", False

            def write(self, data):
                self.sent += data

            async def drain(self):
                pass

            def close(self):
                self.closed = True

        async def scenario():
            v = ryoku_link.PhoneView(None, 1080, 2400)
            v.pointer = object()  # an open view; no compositor is touched
            writer, spawned = FakeWriter(), []

            async def spawn(*argv, **kwargs):
                spawned.append(argv)
                return FakeEncoder()

            with mock.patch.object(ryoku_link, "view", v), \
                    mock.patch.object(asyncio, "create_subprocess_exec", spawn):
                await ryoku_link.handle_video(None, writer)
            return writer, spawned

        with mock.patch.object(ryoku_link, "log"):
            writer, spawned = asyncio.run(scenario())
        self.assertEqual(len(spawned), 1)
        self.assertEqual(spawned[0][:3], ("wf-recorder", "-o", ryoku_link.OUTPUT))
        self.assertEqual(writer.sent, b"\x00\x00\x00\x01frame")
        self.assertTrue(writer.closed)

    def test_mirroring_records_the_pc_monitor_not_the_headless_output(self):
        class Encoder:
            returncode = 0

            def __init__(self):
                self.stdout = asyncio.StreamReader()
                self.stdout.feed_eof()

            async def wait(self):
                return 0

        class Writer:
            def write(self, data): pass

            async def drain(self): pass

        async def scenario():
            v = ryoku_link.PhoneView(None, 1080, 2400)
            v.mirror, v.mirror_native, v.mirror_size = "eDP-1", (3840, 2160), (1920, 1080)
            spawned = []

            async def spawn(*argv, **kwargs):
                spawned.append(argv)
                return Encoder()

            with mock.patch.object(asyncio, "create_subprocess_exec", spawn):
                await v.stream_to(Writer())
            return spawned[0]

        with mock.patch.object(ryoku_link, "log"):
            argv = asyncio.run(scenario())
        self.assertEqual(argv[:3], ("wf-recorder", "-o", "eDP-1"))
        self.assertIn("scale=1920:1080,format=yuv420p", argv)  # a monitor past MAX_SIDE is scaled to the announced size

    def test_video_connection_without_an_open_view_is_closed_untouched(self):
        class Writer:
            closed = False

            def close(self):
                self.closed = True

        writer = Writer()
        with mock.patch.object(ryoku_link, "view", None):
            asyncio.run(ryoku_link.handle_video(None, writer))
        self.assertTrue(writer.closed)

# Output captured from the Nubia (Android 15) while a YouTube video played.
STACK_LIST = """RootTask id=1827 bounds=[0,0][900,2030] displayId=0 userId=0
 configuration={1.0 730mcc2mnc [es_CL] ldltr sw360dp}
  taskId=1827: com.google.android.youtube/com.google.android.youtube.UrlActivity bounds=[0,0][900,2030] userId=0 visible=true topActivity=ComponentInfo{com.google.android.youtube/com.google.android.apps.youtube.app.watchwhile.InternalMainActivity}

RootTask id=1824 bounds=[0,0][900,2030] displayId=0 userId=0
  taskId=1824: com.rixmerz.ryokumobile/com.rixmerz.ryokumobile.LauncherActivity bounds=[0,0][900,2030] userId=0 visible=true
RootTask id=1830 bounds=[0,0][1920,1080] displayId=2 userId=0
  taskId=1830: com.google.android.apps.youtube.music/.activities.MusicActivity bounds=[0,0][1920,1080] userId=0 visible=true
"""
DISPLAYS_BEFORE = """  Display Id=0
    mBaseDisplayInfo=DisplayInfo{"Pantalla integrada", displayId 0, displayGroupId 0, FLAG_SECURE, real 900 x 2030, type INTERNAL, uniqueId "local:4619827259835644672", app 900 x 2030}
"""
DISPLAYS_AFTER = DISPLAYS_BEFORE + """  Display Id=2
    mBaseDisplayInfo=DisplayInfo{"Superposici\u00f3n #1", displayId 2, displayGroupId 0, FLAG_PRESENTATION, real 1920 x 1080, type OVERLAY, uniqueId "overlay:1", app 1920 x 1080}
  Display Id=3
    mBaseDisplayInfo=DisplayInfo{"scrcpy", displayId 3, displayGroupId 0, FLAG_TRUSTED, real 1920 x 1080, type VIRTUAL, uniqueId "virtual:com.android.shell:scrcpy", app 1920 x 1080}
"""


class CastPackage(unittest.TestCase):
    def test_real_package_names_pass(self):
        for name in ("com.google.android.youtube", "org.mozilla.firefox", "com.a1.b_2", "com.rixmerz.ryokumobile.debug"):
            self.assertTrue(ryoku_link.valid_package(name), name)

    def test_shell_strings_and_odd_shapes_are_refused(self):
        for name in ("com.x; rm -rf ~", "com.x && id", "$(id).a", "com.x\n", "a b.c", "-n.com", "com..x", "com.", ".com.x",
                     "single", "com.1abc", "com.x`id`", "", None, 7, ["com.x.y"], "a." * 200 + "b", ryoku_link.OWN_PACKAGE):
            self.assertFalse(ryoku_link.valid_package(name), repr(name))


class CastAllowlist(unittest.TestCase):
    def run_cast(self, package):
        sent, launched = [], []

        async def fake_reply(writer, message):
            sent.append(message)

        async def go():
            with mock.patch.object(ryoku_link, "reply", fake_reply), \
                    mock.patch.object(ryoku_link, "launch_cast", lambda p: launched.append(p) or ryoku_link.Cast(p, 7, "0x1", 3, None)), \
                    mock.patch.object(ryoku_link, "set_zoom", lambda z: None), \
                    mock.patch.object(ryoku_link, "autoplay", lambda cast: None), \
                    mock.patch.object(ryoku_link, "casts", {}):
                await ryoku_link.start_cast(None, package)

        asyncio.run(go())
        return sent, launched

    def test_only_youtube_is_allowed(self):
        self.assertEqual(ryoku_link.CASTABLE_PACKAGES, {"com.google.android.youtube"})

    def test_youtube_is_cast(self):
        sent, launched = self.run_cast("com.google.android.youtube")
        self.assertEqual(launched, ["com.google.android.youtube"])
        self.assertEqual(sent[0]["workspace"], 7)

    def test_other_apps_are_refused_without_launching(self):
        for package in ("com.brave.browser", "com.android.chrome", "com.google.android.youtube.music"):
            sent, launched = self.run_cast(package)
            self.assertEqual(launched, [], package)
            self.assertIn("only YouTube", sent[0]["error"])


class CastWorkspace(unittest.TestCase):
    def test_first_unused_id_is_chosen(self):
        workspaces = [{"id": i, "windows": 1} for i in (1, 2, 3, 4, 5)]
        self.assertEqual(ryoku_link.free_workspace(workspaces, monitors(pc_workspace=5)), 6)

    def test_a_gap_is_filled_and_an_empty_workspace_counts_as_unused(self):
        workspaces = [{"id": 1, "windows": 2}, {"id": 2, "windows": 0}, {"id": 3, "windows": 1}]
        self.assertEqual(ryoku_link.free_workspace(workspaces, monitors(pc_workspace=1)), 2)

    def test_never_the_workspace_a_monitor_shows_even_when_empty(self):
        self.assertEqual(ryoku_link.free_workspace([{"id": 1, "windows": 1}], monitors(pc_workspace=2)), 3)

    def test_never_the_workspace_the_phone_output_shows(self):
        self.assertEqual(ryoku_link.free_workspace([], monitors(pc_workspace=1, phone_workspace=2)), 3)

    def test_special_workspaces_are_ignored(self):
        self.assertEqual(ryoku_link.free_workspace([{"id": -98, "windows": 1}], monitors(pc_workspace=4)), 1)


class CastPhoneState(unittest.TestCase):
    def test_the_front_task_of_the_package_on_the_phone_display_is_found(self):
        self.assertEqual(ryoku_link.find_root_task(STACK_LIST, "com.google.android.youtube"), 1827)

    def test_a_task_on_another_display_or_another_package_is_not(self):
        self.assertIsNone(ryoku_link.find_root_task(STACK_LIST, "com.google.android.apps.youtube.music"))  # on display 2
        self.assertEqual(ryoku_link.find_root_task(STACK_LIST, "com.google.android.apps.youtube.music", display=2), 1830)
        self.assertIsNone(ryoku_link.find_root_task(STACK_LIST, "com.google.android.youtub"))

    def test_the_new_virtual_display_is_the_one_that_appeared(self):
        before = ryoku_link.parse_displays(DISPLAYS_BEFORE)
        self.assertEqual(before, {0: "INTERNAL"})
        after = ryoku_link.parse_displays(DISPLAYS_AFTER)
        self.assertEqual(after, {0: "INTERNAL", 2: "OVERLAY", 3: "VIRTUAL"})
        self.assertEqual(ryoku_link.new_display(before, after), 3)  # not the simulated overlay display
        self.assertIsNone(ryoku_link.new_display(after, after))


class CastArgv(unittest.TestCase):
    def test_scrcpy_gets_a_new_display_but_never_starts_the_app(self):
        argv = ryoku_link.scrcpy_argv("/usr/bin/scrcpy", "324751914997", "ryoku-cast com.x.y", keep_apps=True, display="1920x1080/400")
        self.assertEqual(argv[:4], ["/usr/bin/scrcpy", "-s", "324751914997", "--new-display=1920x1080/400"])
        self.assertIn("--window-title=ryoku-cast com.x.y", argv)
        self.assertIn("--audio-source=playback", argv)
        self.assertIn("--no-vd-destroy-content", argv)
        self.assertFalse([a for a in argv if a.startswith("--start-app")])  # that would start a second task

    def test_the_display_uses_the_phones_own_density_not_a_tablet_one(self):
        # 240 dpi made the display 1280 dp wide and YouTube drew a tablet layout with a spinner that never ended.
        self.assertEqual(ryoku_link.phone_density("Physical density: 400\n"), 400)
        self.assertEqual(ryoku_link.phone_density("Physical density: 420\nOverride density: 320\n"), 320)
        self.assertIsNone(ryoku_link.phone_density(""))
        self.assertEqual(ryoku_link.cast_display(400), "1920x1080/400")
        self.assertEqual(ryoku_link.cast_display(None), "1920x1080")
        self.assertNotIn("/240", " ".join(ryoku_link.scrcpy_argv("scrcpy", "S", "t", True)))

    def test_the_keep_flag_is_left_out_when_this_scrcpy_has_none(self):
        self.assertNotIn("--no-vd-destroy-content", ryoku_link.scrcpy_argv("scrcpy", "S", "t", keep_apps=False))

    def test_the_hyprland_command_places_the_window_silently_and_quotes_every_word(self):
        argv = ryoku_link.scrcpy_argv("/opt/my scrcpy/scrcpy", "S1", "ryoku-cast com.x.y", keep_apps=True)
        lua = ryoku_link.exec_in_workspace(argv, 7, "/run/user/1/ryoku-link/cast-com.x.y.log")
        self.assertTrue(lua.startswith("hl.dsp.exec_cmd(") and lua.endswith('{ workspace = "7 silent" })'))
        command = json.loads(lua[len("hl.dsp.exec_cmd("):lua.index(", { workspace")])
        words = shlex.split(command)
        self.assertEqual(words[words.index("/opt/my scrcpy/scrcpy"):][:2], ["/opt/my scrcpy/scrcpy", "-s"])
        self.assertIn("--window-title=ryoku-cast com.x.y", words)
        self.assertTrue(command.endswith(">/run/user/1/ryoku-link/cast-com.x.y.log 2>&1"))


def lua_focuses_a_workspace_then_the_window(steps):
    return steps[0].startswith("hl.dsp.focus({ workspace =") and steps[1].startswith('hl.dsp.focus({ window = "address:')


class FakeCastWriter:
    def __init__(self):
        self.sent = []

    def write(self, data):
        self.sent.append(json.loads(data))

    async def drain(self):
        pass


class CastCommand(unittest.TestCase):
    """The `cast` message through on_command, the entry the phone uses; scrcpy, adb and Hyprland are stubbed."""

    def setUp(self):
        self.addCleanup(ryoku_link.casts.clear)
        self.order = []  # what happened, in order, across the stubs below
        for name in ("log",):
            patcher = mock.patch.object(ryoku_link, name)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(ryoku_link, "set_zoom", side_effect=lambda f: self.order.append(("zoom", f)) or True)
        self.set_zoom = patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(ryoku_link, "autoplay")
        self.autoplay = patcher.start()
        self.addCleanup(patcher.stop)

    def run_cast(self, message, launch=None):
        writer = FakeCastWriter()
        launch = launch or mock.Mock()
        launched = mock.Mock(side_effect=lambda *a: self.order.append("launch") or launch(*a))
        with mock.patch.object(ryoku_link, "launch_cast", launched):
            asyncio.run(ryoku_link.on_command(writer, message))
        return writer.sent, launched

    def test_a_valid_package_is_launched_and_the_phone_is_told_the_workspace(self):
        sent, launched = self.run_cast({"cmd": "cast", "package": "com.google.android.youtube"},
                                       mock.Mock(return_value=ryoku_link.Cast("com.google.android.youtube", 7, "0xabc", 3)))
        launched.assert_called_once_with("com.google.android.youtube")
        self.assertEqual(sent, [{"type": "cast", "package": "com.google.android.youtube", "workspace": 7}])
        self.assertEqual(ryoku_link.casts["com.google.android.youtube"].display, 3)

    def test_a_shell_string_never_reaches_the_launcher(self):
        for bad in ("com.x; reboot", "$(id)", 5, None):
            sent, launched = self.run_cast({"cmd": "cast", "package": bad})
            launched.assert_not_called()
            self.assertEqual(sent, [{"type": "cast", "error": "not a package name"}])

    def test_a_failed_cast_reports_the_reason_and_is_not_remembered(self):
        sent, _ = self.run_cast({"cmd": "cast", "package": "com.google.android.youtube"}, mock.Mock(side_effect=ryoku_link.CastError("scrcpy is not installed on the computer")))
        self.assertEqual(sent, [{"type": "cast", "package": "com.google.android.youtube", "error": "scrcpy is not installed on the computer"}])
        self.assertEqual(ryoku_link.casts, {})

    def test_a_cast_ends_the_live_view_and_resets_the_zoom_before_launching(self):
        class Owner:
            pass
        ended = []

        async def end_view():
            ended.append(True)
            self.order.append("end view")
            ryoku_link.view = None

        ryoku_link.view = Owner()
        self.addCleanup(setattr, ryoku_link, "view", None)
        with mock.patch.object(ryoku_link, "end_view", end_view):
            self.run_cast({"cmd": "cast", "package": "com.google.android.youtube"}, mock.Mock(return_value=ryoku_link.Cast("com.google.android.youtube", 7, "0xabc", 3)))
        self.assertEqual(self.order, ["end view", ("zoom", 1.0), "launch"])

    def test_a_cast_resets_the_zoom_even_without_a_live_view(self):
        self.run_cast({"cmd": "cast", "package": "com.google.android.youtube"}, mock.Mock(return_value=ryoku_link.Cast("com.google.android.youtube", 7, "0xabc", 3)))
        self.assertEqual(self.order, [("zoom", 1.0), "launch"])

    def test_closing_the_window_forgets_the_cast(self):
        ryoku_link.casts["com.x.y"] = ryoku_link.Cast("com.x.y", 8, "0x1", 3)
        ryoku_link.casts["com.a.b"] = ryoku_link.Cast("com.a.b", 9, "0x2", 4)
        with mock.patch.object(ryoku_link, "hyprctl", lambda *a: {"clients": [{"address": "0x2"}], "monitors": monitors(2)}[a[0]]):
            asyncio.run(ryoku_link.reap_casts())
        self.assertEqual(list(ryoku_link.casts), ["com.a.b"])

    def reap_with(self, cast, shown_workspace):
        ryoku_link.casts[cast.package] = cast
        dispatched = []

        def hypr(*args):
            return {"clients": [], "monitors": monitors(shown_workspace)}[args[0]]

        with mock.patch.object(ryoku_link, "hyprctl", hypr), mock.patch.object(ryoku_link, "dispatch", lambda *lua: dispatched.append(lua) or True):
            asyncio.run(ryoku_link.reap_casts())
        return dispatched

    def test_closing_the_window_returns_the_pc_to_the_workspace_it_was_on(self):
        dispatched = self.reap_with(ryoku_link.Cast("com.x.y", 8, "0x1", 3, previous=4), shown_workspace=8)
        self.assertEqual(dispatched, [(ryoku_link.focus_monitor("eDP-1"), ryoku_link.focus_workspace(4))])

    def test_a_pc_that_moved_on_by_itself_is_not_pulled_back(self):
        self.assertEqual(self.reap_with(ryoku_link.Cast("com.x.y", 8, "0x1", 3, previous=4), shown_workspace=2), [])

    def test_a_cast_with_no_known_previous_workspace_returns_nowhere(self):
        self.assertEqual(self.reap_with(ryoku_link.Cast("com.x.y", 8, "0x1", 3), shown_workspace=8), [])


class FakeScrcpy(unittest.TestCase):
    """launch_cast end to end against a scrcpy stub: the order of the steps and what each one is given."""

    def make_stub(self, directory, text="  --new-display  --no-vd-destroy-content\n"):
        path = os.path.join(directory, "scrcpy")
        with open(path, "w") as f:
            f.write(f"#!/bin/sh\ncat <<'EOF'\n{text}EOF\n")
        os.chmod(path, 0o755)
        return path

    def scenario(self, directory, *, stub_text=None, stack=STACK_LIST, moved_stack=None, leftover=False):
        calls, dispatched = [], []
        window = {"title": "ryoku-cast com.google.android.youtube", "pid": 99, "address": "0xfeed", "workspace": {"id": 6}}
        old_window = {"title": window["title"], "pid": 55, "address": "0xdead", "workspace": {"id": 3}}
        gone = []  # leftover windows already closed
        displays = iter([DISPLAYS_BEFORE, DISPLAYS_AFTER, DISPLAYS_AFTER])
        stacks = iter([stack, moved_stack or stack.replace("displayId=0", "displayId=3", 1)])

        def adb(serial, *args, timeout=8):
            calls.append(args)
            if args[:2] == ("shell", "pm"):
                return "package:/data/app/base.apk\n"
            if args[:3] == ("shell", "am", "stack"):
                return next(stacks)
            if args[:3] == ("shell", "dumpsys", "display"):
                return next(displays, DISPLAYS_AFTER)
            if args[:3] == ("shell", "wm", "density"):
                return "Physical density: 400\n"
            return ""

        def hypr(*args):
            # The new window exists only once scrcpy was started; a leftover one only until it is signalled.
            clients = ([window] if dispatched else []) + ([old_window] if leftover and not gone else [])
            return {"workspaces": [{"id": i, "windows": 1} for i in (1, 2, 3, 4, 5)], "monitors": monitors(5), "clients": clients}[args[0]]

        def kill(pid, sig):
            calls.append(("kill", pid, sig))
            if pid == 55:
                gone.append(pid)

        stub = self.make_stub(directory, stub_text) if stub_text is not None else self.make_stub(directory)
        with mock.patch.object(ryoku_link, "SCRCPY", stub), mock.patch.object(ryoku_link, "adb", adb), \
                mock.patch.object(ryoku_link, "hyprctl", hypr), mock.patch.object(ryoku_link, "SOCKET_DIR", directory), \
                mock.patch.object(ryoku_link, "reverse_targets", return_value=["192.168.1.9:5555", "324751914997"]), \
                mock.patch.object(ryoku_link, "dispatch", lambda *lua: dispatched.append(lua) or True), \
                mock.patch.object(os, "kill", kill), mock.patch.object(ryoku_link, "log"):
            try:
                cast = ryoku_link.launch_cast("com.google.android.youtube")
            except ryoku_link.CastError as e:
                return e, calls, dispatched
        return cast, calls, dispatched

    def test_the_app_is_moved_to_the_new_display_and_the_window_goes_to_the_first_free_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            cast, calls, dispatched = self.scenario(tmp)
        self.assertEqual((cast.display, cast.address), (3, "0xfeed"))
        self.assertIn(("shell", "am", "display", "move-stack", "1827", "3"), calls)
        self.assertEqual(len(dispatched), 2)
        self.assertIn('workspace = "6 silent" }', dispatched[0][0])  # 1-5 hold windows, 5 is on the monitor
        self.assertIn("--new-display=1920x1080/400", dispatched[0][0])  # the phone's own density
        self.assertIn("-s 324751914997", dispatched[0][0])  # the USB phone, not the TCP one

    def test_once_the_app_is_on_the_window_the_pc_switches_to_it_and_remembers_where_it_was(self):
        with tempfile.TemporaryDirectory() as tmp:
            cast, calls, dispatched = self.scenario(tmp)
        self.assertEqual(dispatched[1], (ryoku_link.focus_workspace(6), ryoku_link.focus_window("0xfeed")))
        self.assertEqual((cast.workspace, cast.previous), (6, 5))  # the monitor showed 5 before
        # The window is shown only after the app was moved onto the display, never while scrcpy was still starting.
        self.assertIn(("shell", "am", "display", "move-stack", "1827", "3"), calls)
        self.assertTrue(lua_focuses_a_workspace_then_the_window(dispatched[1]))

    def test_the_phone_is_asked_about_nothing_but_a_validated_name_and_integers(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, calls, _ = self.scenario(tmp)
        self.assertIn(("shell", "pm", "path", "com.google.android.youtube"), calls)
        move = next(c for c in calls if "move-stack" in c)
        self.assertTrue(all(a.isdigit() for a in move[-2:]))

    def test_an_old_scrcpy_without_virtual_displays_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            err, _, dispatched = self.scenario(tmp, stub_text="  --serial\n")
        self.assertIsInstance(err, ryoku_link.CastError)
        self.assertIn("too old", str(err))
        self.assertEqual(dispatched, [])

    def test_an_app_that_is_not_running_is_reported_before_scrcpy_starts(self):
        with tempfile.TemporaryDirectory() as tmp:
            err, _, dispatched = self.scenario(tmp, stack="RootTask id=1 bounds=[0,0][1,1] displayId=0 userId=0\n  taskId=1: com.other.app/.Main\n")
        self.assertIn("not running", str(err))
        self.assertEqual(dispatched, [])

    def test_missing_scrcpy_is_reported_without_touching_adb_or_the_desktop(self):
        with mock.patch.object(ryoku_link, "SCRCPY", None), mock.patch.object(ryoku_link, "adb") as adb, \
                mock.patch.object(ryoku_link, "dispatch") as dispatch:
            with self.assertRaisesRegex(ryoku_link.CastError, "scrcpy is not installed"):
                ryoku_link.launch_cast("com.google.android.youtube")
        adb.assert_not_called()
        dispatch.assert_not_called()

    def test_an_app_android_will_not_move_ends_the_scrcpy_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            err, calls, _ = self.scenario(tmp, moved_stack=STACK_LIST)  # still on display 0 after the move
        self.assertIn("would not move", str(err))
        self.assertIn(("kill", 99, signal.SIGTERM), calls)

    def test_a_leftover_window_of_an_earlier_cast_is_closed_not_adopted(self):
        # A bridge restart forgets its casts but not their windows: the new cast must not take the old
        # window's address, and the old scrcpy (with its virtual display) must end.
        with tempfile.TemporaryDirectory() as tmp:
            cast, calls, _ = self.scenario(tmp, leftover=True)
        self.assertEqual(cast.address, "0xfeed")
        self.assertIn(("kill", 55, signal.SIGTERM), calls)
        self.assertNotIn(("kill", 99, signal.SIGTERM), calls)


YT = "com.google.android.youtube"
# YouTube on the virtual display 3, the phone showing its own launcher.
STACK_CAST_LIVE = """RootTask id=2102 bounds=[0,0][1920,1080] displayId=3 userId=0
  taskId=2101: com.google.android.youtube/com.google.android.youtube.app.honeycomb.Shell$HomeActivity bounds=[0,0][1920,1080] userId=0 visible=true
RootTask id=1 bounds=[0,0][2030,900] displayId=0 userId=0
  taskId=2: com.zte.mifavor.launcher/com.android.launcher3.uioverrides.QuickstepLauncher bounds=[0,0][2030,900] userId=0 visible=true
"""
# Captured from the phone: YouTube went back to the phone (pinned) and the virtual display fell back to its launcher.
STACK_CAST_LEFT = """RootTask id=2101 bounds=[1559,-82][1990,160] displayId=0 userId=0
  taskId=2101: com.google.android.youtube/com.google.android.youtube.app.honeycomb.Shell$HomeActivity bounds=[1559,-82][1990,160] userId=0 visible=true
RootTask id=2102 bounds=[0,0][1920,1080] displayId=3 userId=0
  taskId=2103: com.zte.mifavor.launcher/com.android.launcher3.secondarydisplay.SecondaryDisplayLauncher bounds=[0,0][1920,1080] userId=0 visible=true
"""
STACK_CAST_EMPTY = "RootTask id=1 bounds=[0,0][2030,900] displayId=0 userId=0\n  taskId=2: com.zte.mifavor.launcher/.Launcher bounds=[0,0][1,1] userId=0 visible=true\n"
MEDIA_SESSIONS = """  Sessions Stack - have 3 sessions:
    package=com.android.server.telecom
    state=null
    package=com.google.android.youtube
    state=PlaybackState {state=%s(3), position=0, buffered position=0, speed=1.0, updated=191055961, actions=8631, custom actions=[], active item id=-1, error=null}
    package=org.telegram.messenger
    state=PlaybackState {state=NONE(0), position=0, buffered position=0, speed=1.0, updated=174845600, actions=2477828, custom actions=[], active item id=-1, error=null}
"""


class PlaybackState(unittest.TestCase):
    def test_the_state_is_the_one_of_the_apps_own_session(self):
        self.assertEqual(ryoku_link.parse_playback_state(MEDIA_SESSIONS % "PAUSED", YT), "PAUSED")
        self.assertEqual(ryoku_link.parse_playback_state(MEDIA_SESSIONS % "PLAYING", YT), "PLAYING")
        self.assertEqual(ryoku_link.parse_playback_state(MEDIA_SESSIONS % "PLAYING", "org.telegram.messenger"), "NONE")

    def test_no_session_or_no_state_is_none_never_a_neighbours_state(self):
        self.assertIsNone(ryoku_link.parse_playback_state(MEDIA_SESSIONS % "PLAYING", "com.android.server.telecom"))  # state=null
        self.assertIsNone(ryoku_link.parse_playback_state(MEDIA_SESSIONS % "PLAYING", "com.other.app"))
        self.assertIsNone(ryoku_link.parse_playback_state("", YT))


class FakePhone:
    """adb for the cast health and playback checks: replies per command, records what was run."""

    def __init__(self, stack=STACK_CAST_LIVE, displays=DISPLAYS_AFTER, states=("PLAYING",)):
        self.stack, self.displays, self.calls = stack, displays, []
        self.states = list(states)  # successive media session states; the last one repeats

    def __call__(self, serial, *args, timeout=8):
        self.calls.append(args)
        if args[:3] == ("shell", "am", "stack"):
            return self.stack
        if args[:3] == ("shell", "dumpsys", "display"):
            return self.displays
        if args[:3] == ("shell", "dumpsys", "media_session"):
            return MEDIA_SESSIONS % (self.states.pop(0) if len(self.states) > 1 else self.states[0])
        return ""


def a_cast(**kw):
    return ryoku_link.Cast(YT, 6, "0xfeed", 3, kw.pop("previous", 5), "324751914997")


class CastHealth(unittest.TestCase):
    def problem(self, phone, window=True):
        clients = [{"address": "0xfeed", "pid": 99}] if window else []
        with mock.patch.object(ryoku_link, "adb", phone), mock.patch.object(ryoku_link, "hyprctl", lambda *a: clients):
            return ryoku_link.cast_problem(a_cast())

    def test_the_app_in_front_on_its_display_is_a_working_cast(self):
        self.assertIsNone(self.problem(FakePhone()))

    def test_the_front_task_of_a_display_is_found(self):
        self.assertEqual(ryoku_link.top_package(STACK_CAST_LIVE, 3), YT)
        self.assertEqual(ryoku_link.top_package(STACK_CAST_LIVE, 0), "com.zte.mifavor.launcher")
        self.assertEqual(ryoku_link.top_package(STACK_CAST_LEFT, 3), "com.zte.mifavor.launcher")
        self.assertIsNone(ryoku_link.top_package(STACK_CAST_LIVE, 9))

    def test_the_app_leaving_the_display_for_the_phone_launcher_is_a_dead_cast(self):
        # What the user saw: the PC window kept streaming the phone's home screen.
        self.assertIn("com.zte.mifavor.launcher is in front", self.problem(FakePhone(stack=STACK_CAST_LEFT)))

    def test_an_empty_display_a_missing_display_and_a_closed_window_are_dead_casts(self):
        self.assertIn("nothing", self.problem(FakePhone(stack=STACK_CAST_EMPTY.replace("displayId=0", "displayId=7"))))
        self.assertIn("virtual display is gone", self.problem(FakePhone(displays=DISPLAYS_BEFORE)))
        self.assertIn("window is gone", self.problem(FakePhone(), window=False))

    def test_an_adb_that_says_nothing_is_not_a_verdict(self):
        with self.assertRaises(OSError):
            self.problem(FakePhone(displays=""))


class ResumePlayback(unittest.TestCase):
    def resume(self, states):
        phone = FakePhone(states=states)
        with mock.patch.object(ryoku_link, "adb", phone), mock.patch.object(ryoku_link.time, "sleep"):
            return ryoku_link.resume_playback("S", 3, YT, timeout=2.0), phone.calls

    def test_a_paused_video_gets_play_on_its_display_until_the_session_says_playing(self):
        ok, calls = self.resume(["PAUSED", "PAUSED", "PLAYING"])
        self.assertTrue(ok)
        self.assertIn(("shell", "input", "-d", "3", "keyevent", "126"), calls)  # play, never play/pause (85)
        self.assertNotIn(("shell", "input", "-d", "3", "keyevent", "85"), calls)

    def test_a_video_that_already_plays_is_left_alone(self):
        ok, calls = self.resume(["PLAYING"])
        self.assertTrue(ok)
        self.assertFalse([c for c in calls if "keyevent" in c])

    def test_no_session_means_no_key_press_and_no_claim_of_playing(self):
        phone = FakePhone()
        with mock.patch.object(ryoku_link, "adb", phone), mock.patch.object(ryoku_link, "playback_state", lambda s, p: None), \
                mock.patch.object(ryoku_link.time, "sleep"):
            self.assertFalse(ryoku_link.resume_playback("S", 3, YT, timeout=0.0))
        self.assertFalse([c for c in phone.calls if "keyevent" in c])  # the key would start whatever played last

    def test_a_video_that_never_resumes_is_reported_not_playing(self):
        ok, _ = self.resume(["PAUSED"])
        self.assertFalse(ok)


class CastRecovery(unittest.TestCase):
    """A dead cast is ended and never blocks the next one; a live one is reused."""

    def setUp(self):
        self.addCleanup(ryoku_link.casts.clear)
        self.events = []
        for name, fake in (("log", lambda m: None), ("set_zoom", lambda z: self.events.append(("zoom", z)) or True),
                           ("autoplay", lambda c: self.events.append(("autoplay", c.package))),
                           ("dispatch", lambda *lua: self.events.append(("dispatch", lua)) or True),
                           ("close_cast_window", lambda address: self.events.append(("close", address)))):
            patcher = mock.patch.object(ryoku_link, name, fake)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_cast(self, problem, launch=None):
        sent = []

        async def reply(writer, message):
            sent.append(message)

        ryoku_link.casts[YT] = a_cast()
        launch = launch or (lambda package: self.events.append(("launch", package)) or ryoku_link.Cast(package, 7, "0xnew", 4, 5, "S"))
        with mock.patch.object(ryoku_link, "reply", reply), mock.patch.object(ryoku_link, "cast_problem", lambda cast: problem(cast)), \
                mock.patch.object(ryoku_link, "launch_cast", launch), \
                mock.patch.object(ryoku_link, "window_at", lambda address: {"address": address, "workspace": {"id": 9}}), \
                mock.patch.object(ryoku_link, "hyprctl", lambda *a: monitors(6)):
            asyncio.run(ryoku_link.start_cast(None, YT))
        return sent

    def test_a_dead_cast_is_cleaned_up_and_the_new_send_goes_through(self):
        sent = self.run_cast(lambda cast: "nothing is in front on the virtual display, not the app")
        self.assertEqual(sent, [{"type": "cast", "package": YT, "workspace": 7}])  # not "already on the PC"
        self.assertEqual(ryoku_link.casts[YT].address, "0xnew")
        names = [e if isinstance(e, str) else e[0] for e in self.events]
        self.assertLess(names.index("close"), names.index("launch"))  # the old scrcpy is closed first

    def test_a_phone_that_cannot_be_reached_does_not_keep_a_dead_cast_alive(self):
        def unreachable(cast):
            raise OSError("adb offline")
        self.run_cast(unreachable)
        self.assertEqual(ryoku_link.casts[YT].address, "0xnew")

    def test_a_live_cast_is_focused_and_reused_not_refused_and_not_relaunched(self):
        launched = []
        sent = self.run_cast(lambda cast: None, launch=lambda package: launched.append(package))
        self.assertEqual(launched, [])
        self.assertEqual(sent, [{"type": "cast", "package": YT, "workspace": 9}])  # where the window is now
        self.assertEqual(ryoku_link.casts[YT].address, "0xfeed")
        self.assertIn(("dispatch", (ryoku_link.focus_workspace(9), ryoku_link.focus_window("0xfeed"))), self.events)
        self.assertIn(("autoplay", YT), self.events)  # reusing it also makes sure the video plays
        self.assertNotIn(("close", "0xfeed"), self.events)

    def test_ending_a_cast_closes_the_window_returns_the_pc_and_resets_the_zoom(self):
        cast = a_cast()
        ryoku_link.casts[YT] = cast
        with mock.patch.object(ryoku_link, "hyprctl", lambda *a: monitors(6)):
            asyncio.run(ryoku_link.end_cast(cast, "test"))
        self.assertEqual(ryoku_link.casts, {})
        self.assertEqual(self.events, [("close", "0xfeed"), ("dispatch", (ryoku_link.focus_monitor("eDP-1"), ryoku_link.focus_workspace(5))), ("zoom", 1.0)])


class WatchCasts(unittest.TestCase):
    """watch_casts ends a cast after CAST_MISSES bad checks in a row, never on one."""

    def setUp(self):
        self.addCleanup(ryoku_link.casts.clear)
        self.ended = []

        async def end_cast(cast, reason):
            self.ended.append(reason)
            ryoku_link.casts.pop(cast.package, None)

        for name, fake in (("log", lambda m: None), ("end_cast", end_cast)):
            patcher = mock.patch.object(ryoku_link, name, fake)
            patcher.start()
            self.addCleanup(patcher.stop)

    def watch(self, answers, polls):
        """Runs `polls` rounds of the watcher; `answers` are the successive cast_problem results (an exception is raised)."""
        ryoku_link.casts[YT] = a_cast()
        answers = list(answers)

        def problem(cast):
            answer = answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer

        async def go():
            real_sleep = asyncio.sleep
            rounds = []

            async def sleep(_):
                rounds.append(1)
                if len(rounds) > polls:
                    raise asyncio.CancelledError
                await real_sleep(0)

            with mock.patch.object(ryoku_link, "cast_problem", problem), mock.patch.object(ryoku_link.asyncio, "sleep", sleep):
                try:
                    await ryoku_link.watch_casts()
                except asyncio.CancelledError:
                    pass

        asyncio.run(go())

    def test_two_bad_checks_in_a_row_end_the_cast(self):
        self.watch(["launcher is in front", "launcher is in front"], polls=2)
        self.assertEqual(self.ended, ["launcher is in front"])

    def test_one_bad_check_between_good_ones_does_not(self):
        self.watch([None, "launcher is in front", None, "launcher is in front"], polls=4)
        self.assertEqual(self.ended, [])
        self.assertIn(YT, ryoku_link.casts)

    def test_an_unreachable_phone_is_not_a_dead_cast(self):
        self.watch([OSError("adb offline")] * 3, polls=3)
        self.assertEqual(self.ended, [])
        self.assertIn(YT, ryoku_link.casts)


if __name__ == "__main__":
    unittest.main()
