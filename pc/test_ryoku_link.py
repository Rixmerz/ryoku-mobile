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
        sent, _ = self.run_cast({"cmd": "cast", "package": "com.x.y"}, mock.Mock(side_effect=ryoku_link.CastError("scrcpy is not installed on the computer")))
        self.assertEqual(sent, [{"type": "cast", "package": "com.x.y", "error": "scrcpy is not installed on the computer"}])
        self.assertEqual(ryoku_link.casts, {})

    def test_a_second_cast_of_the_same_app_is_refused(self):
        ryoku_link.casts["com.x.y"] = ryoku_link.Cast("com.x.y", 8, "0x1", 3)
        sent, launched = self.run_cast({"cmd": "cast", "package": "com.x.y"})
        launched.assert_not_called()
        self.assertEqual(sent, [{"type": "cast", "package": "com.x.y", "error": "already on the PC, workspace 8"}])

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
            self.run_cast({"cmd": "cast", "package": "com.x.y"}, mock.Mock(return_value=ryoku_link.Cast("com.x.y", 7, "0xabc", 3)))
        self.assertEqual(self.order, ["end view", ("zoom", 1.0), "launch"])

    def test_a_cast_resets_the_zoom_even_without_a_live_view(self):
        self.run_cast({"cmd": "cast", "package": "com.x.y"}, mock.Mock(return_value=ryoku_link.Cast("com.x.y", 7, "0xabc", 3)))
        self.assertEqual(self.order, [("zoom", 1.0), "launch"])

    def test_a_refused_duplicate_leaves_the_live_view_alone(self):
        ryoku_link.casts["com.x.y"] = ryoku_link.Cast("com.x.y", 8, "0x1", 3)
        self.run_cast({"cmd": "cast", "package": "com.x.y"})
        self.assertEqual(self.order, [])

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

    def scenario(self, directory, *, stub_text=None, stack=STACK_LIST, moved_stack=None):
        calls, dispatched = [], []
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
            return {"workspaces": [{"id": i, "windows": 1} for i in (1, 2, 3, 4, 5)], "monitors": monitors(5),
                    "clients": [{"title": "ryoku-cast com.google.android.youtube", "pid": 99, "address": "0xfeed", "workspace": {"id": 6}}]}[args[0]]

        stub = self.make_stub(directory, stub_text) if stub_text is not None else self.make_stub(directory)
        with mock.patch.object(ryoku_link, "SCRCPY", stub), mock.patch.object(ryoku_link, "adb", adb), \
                mock.patch.object(ryoku_link, "hyprctl", hypr), mock.patch.object(ryoku_link, "SOCKET_DIR", directory), \
                mock.patch.object(ryoku_link, "reverse_targets", return_value=["192.168.1.9:5555", "324751914997"]), \
                mock.patch.object(ryoku_link, "dispatch", lambda *lua: dispatched.append(lua) or True), \
                mock.patch.object(ryoku_link, "log"):
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
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(os, "kill") as kill:
            err, _, _ = self.scenario(tmp, moved_stack=STACK_LIST)  # still on display 0 after the move
        self.assertIn("would not move", str(err))
        kill.assert_called_once_with(99, signal.SIGTERM)


if __name__ == "__main__":
    unittest.main()
