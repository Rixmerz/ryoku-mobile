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
    def test_the_state_tells_the_phone_this_bridge_can_open_urls(self):
        def hypr(*args):
            return {"monitors": monitors(1), "workspaces": [], "clients": []}[args[0]]

        with mock.patch.object(ryoku_link, "hyprctl", hypr), mock.patch.object(ryoku_link, "view", None):
            self.assertIn("open_url", ryoku_link.snapshot()["features"])
            self.assertIn("open_video", ryoku_link.snapshot()["features"])
            self.assertIn("volume", ryoku_link.snapshot()["features"])
            self.assertNotIn("cast", ryoku_link.snapshot()["features"])


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


class FreeWorkspace(unittest.TestCase):
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


VIDEO = "dQw4w9WgXcQ"


class VideoUrl(unittest.TestCase):
    def test_the_link_forms_youtube_shares_give_the_video_id(self):
        for url in (f"https://www.youtube.com/watch?v={VIDEO}", f"https://youtube.com/watch?v={VIDEO}&si=3kSBl_nfCdVs49jN",
                    f"https://m.youtube.com/watch?v={VIDEO}", f"https://youtu.be/{VIDEO}?si=abc", f"http://youtu.be/{VIDEO}",
                    f"https://www.youtube.com/shorts/{VIDEO}", f"https://www.youtube.com/live/{VIDEO}", f"  https://youtu.be/{VIDEO}  "):
            self.assertEqual(ryoku_link.parse_video_url(url), (VIDEO, None), url)

    def test_the_start_time_of_a_link_is_read(self):
        for tail, seconds in (("t=77", 77), ("t=77s", 77), ("t=1m17s", 77), ("t=1h2m3s", 3723), ("start=90", 90)):
            self.assertEqual(ryoku_link.parse_video_url(f"https://youtu.be/{VIDEO}?{tail}"), (VIDEO, seconds), tail)

    def test_a_start_time_that_is_not_a_time_is_none_not_a_refusal(self):
        for tail in ("t=abc", "t=-5", "t=1.5", "t=$(id)", "t=", "t=999999999"):
            self.assertEqual(ryoku_link.parse_video_url(f"https://youtu.be/{VIDEO}?{tail}"), (VIDEO, None), tail)

    def test_other_hosts_schemes_and_shapes_are_refused(self):
        for url in (f"https://evil.com/watch?v={VIDEO}", f"https://youtube.com.evil.com/watch?v={VIDEO}", f"https://notyoutube.com/watch?v={VIDEO}",
                    f"https://youtube.com@evil.com/watch?v={VIDEO}", f"https://evil.com@youtube.com/watch?v={VIDEO}",
                    f"https://youtube.com:8080/watch?v={VIDEO}", f"javascript:alert(1)//youtu.be/{VIDEO}", f"file:///youtu.be/{VIDEO}",
                    f"ftp://youtu.be/{VIDEO}", "https://www.youtube.com/", "https://www.youtube.com/watch", "https://www.youtube.com/results?search_query=x",
                    "https://www.youtube.com/playlist?list=PL1234567890A", f"https://www.youtube.com/watch/{VIDEO}", f"https://youtu.be/{VIDEO}/extra",
                    "https://youtu.be/", "", "not a url", None, 7, [f"https://youtu.be/{VIDEO}"], "https://youtu.be/" + "a" * 3000):
            self.assertIsNone(ryoku_link.parse_video_url(url), repr(url)[:80])

    def test_an_id_that_is_not_eleven_plain_characters_is_refused(self):
        for bad in (f"{VIDEO};rm", f"{VIDEO}%20x", "short", f"{VIDEO}X", "dQw4w9WgXc ", "dQw4w9WgX/Q", "dQw4w9WgX$Q", "dQw4w9WgX\nQ", "$(id)abcdefg", "`id`abcdefgh"):
            self.assertIsNone(ryoku_link.parse_video_url(f"https://youtu.be/{bad}"), bad)
            self.assertIsNone(ryoku_link.parse_video_url(f"https://www.youtube.com/watch?v={bad}"), bad)


class WatchUrl(unittest.TestCase):
    def test_the_address_is_built_from_the_id_and_whole_seconds(self):
        self.assertEqual(ryoku_link.watch_url(VIDEO, 77), f"https://www.youtube.com/watch?v={VIDEO}&t=77s")
        self.assertEqual(ryoku_link.watch_url(VIDEO, 0), f"https://www.youtube.com/watch?v={VIDEO}&t=0s")

    def test_nothing_but_an_id_and_whole_seconds_can_get_in(self):
        for video_id, seconds in ((f"{VIDEO};id", 1), ("short", 1), (VIDEO, "77"), (VIDEO, 7.5), (VIDEO, True), (VIDEO, -1), (VIDEO, ryoku_link.MAX_SECONDS + 1), (VIDEO, None)):
            with self.assertRaises(ValueError, msg=repr((video_id, seconds))):
                ryoku_link.watch_url(video_id, seconds)

    def test_the_time_of_a_link_text(self):
        self.assertEqual([ryoku_link.parse_time(t) for t in ("77", "77s", "1m", "2h", "1h1m1s", "0")], [77, 77, 60, 7200, 3661, 0])
        for bad in ("", "s", "h", "1x", "1.5", "-1", " 1", "1 ", "1s1m", str(ryoku_link.MAX_SECONDS + 1)):
            self.assertIsNone(ryoku_link.parse_time(bad), bad)


class HandoffTarget(unittest.TestCase):
    def test_the_phones_position_is_the_minute(self):
        msg = {"url": f"https://youtu.be/{VIDEO}?t=5", "position_seconds": 77}
        self.assertEqual(ryoku_link.handoff_target(msg), f"https://www.youtube.com/watch?v={VIDEO}&t=77s")

    def test_without_a_position_the_links_own_time_then_zero(self):
        self.assertEqual(ryoku_link.handoff_target({"url": f"https://youtu.be/{VIDEO}?t=5"}), f"https://www.youtube.com/watch?v={VIDEO}&t=5s")
        self.assertEqual(ryoku_link.handoff_target({"url": f"https://youtu.be/{VIDEO}"}), f"https://www.youtube.com/watch?v={VIDEO}&t=0s")

    def test_a_position_that_is_not_whole_seconds_is_refused_not_repaired(self):
        for bad in (True, 7.5, "77", -1, ryoku_link.MAX_SECONDS + 1, [77], {"s": 1}, float("nan")):
            self.assertIsNone(ryoku_link.handoff_target({"url": f"https://youtu.be/{VIDEO}", "position_seconds": bad}), repr(bad))

    def test_the_target_never_carries_the_phones_own_text(self):
        target = ryoku_link.handoff_target({"url": f"https://youtu.be/{VIDEO}?si=$(id)&x=;reboot&t=7#`id`", "position_seconds": 3})
        self.assertEqual(target, f"https://www.youtube.com/watch?v={VIDEO}&t=3s")


class BrowserCommand(unittest.TestCase):
    URL = f"https://www.youtube.com/watch?v={VIDEO}&t=77s"

    def test_the_exec_words_of_the_main_entry_are_read(self):
        text = "[Desktop Entry]\nName=Brave\nExec=brave-origin %U\n\n[Desktop Action new-window]\nExec=brave-origin --other\n"
        self.assertEqual(ryoku_link.browser_exec(text), ["brave-origin", "%U"])
        self.assertEqual(ryoku_link.browser_exec('[Desktop Entry]\nExec="/opt/my browser/b" --x %u\n'), ["/opt/my browser/b", "--x", "%u"])
        self.assertIsNone(ryoku_link.browser_exec("[Desktop Entry]\nName=x\n"))
        self.assertIsNone(ryoku_link.browser_exec('[Desktop Entry]\nExec="unclosed\n'))

    def test_the_address_replaces_the_field_code_as_one_word_in_a_new_window(self):
        self.assertEqual(ryoku_link.browser_argv(["brave-origin", "%U"], self.URL), ["brave-origin", "--new-window", self.URL])
        self.assertEqual(ryoku_link.browser_argv(["firefox", "%u", "%i"], self.URL), ["firefox", "--new-window", self.URL])
        self.assertEqual(ryoku_link.browser_argv(["env", "A=1", "chromium", "--flag", "%U"], self.URL), ["env", "A=1", "chromium", "--flag", "--new-window", self.URL])

    def test_an_exec_without_a_field_code_gets_the_address_appended(self):
        self.assertEqual(ryoku_link.browser_argv(["epiphany"], self.URL), ["epiphany", "--new-window", self.URL])

    def test_no_exec_falls_back_to_xdg_open(self):
        self.assertEqual(ryoku_link.browser_argv(None, self.URL), ["xdg-open", self.URL])
        self.assertEqual(ryoku_link.browser_argv([], self.URL), ["xdg-open", self.URL])


class LaunchBrowser(unittest.TestCase):
    def launch(self, default, exec_line):
        started = []
        answer = mock.Mock(stdout=default + "\n")
        with mock.patch.object(ryoku_link.subprocess, "run", return_value=answer), \
                mock.patch.object(ryoku_link, "read_desktop_file", lambda name: f"[Desktop Entry]\nExec={exec_line}\n"), \
                mock.patch.object(ryoku_link.shutil, "which", lambda name: None), \
                mock.patch.object(ryoku_link.subprocess, "Popen", lambda argv, **kw: started.append((argv, kw))):
            ryoku_link.launch_browser("https://www.youtube.com/watch?v=" + VIDEO + "&t=9s")
        return started

    def test_the_default_browser_is_run_not_a_hardcoded_one(self):
        (argv, _), = self.launch("firefox.desktop", "firefox %u")
        self.assertEqual(argv[0], "firefox")
        (argv, _), = self.launch("brave-origin.desktop", "brave-origin %U")
        self.assertEqual(argv[0], "brave-origin")

    def test_it_is_started_from_an_argument_list_without_a_shell(self):
        (argv, kw), = self.launch("firefox.desktop", "firefox %u")
        self.assertIsInstance(argv, list)
        self.assertFalse(kw.get("shell", False))
        self.assertEqual([w for w in argv if "youtube.com" in w], [f"https://www.youtube.com/watch?v={VIDEO}&t=9s"])

    def test_a_default_that_is_not_a_desktop_file_name_is_not_used(self):
        with mock.patch.object(ryoku_link.subprocess, "run", return_value=mock.Mock(stdout="x; rm -rf ~\n")):
            self.assertIsNone(ryoku_link.default_browser_desktop())

    def test_it_runs_in_its_own_systemd_scope_when_there_is_systemd_run(self):
        started = []
        with mock.patch.object(ryoku_link.subprocess, "run", return_value=mock.Mock(stdout="firefox.desktop\n")), \
                mock.patch.object(ryoku_link, "read_desktop_file", lambda name: "[Desktop Entry]\nExec=firefox %u\n"), \
                mock.patch.object(ryoku_link.shutil, "which", lambda name: "/usr/bin/" + name), \
                mock.patch.object(ryoku_link.subprocess, "Popen", lambda argv, **kw: started.append(argv)):
            ryoku_link.launch_browser("https://www.youtube.com/watch?v=" + VIDEO + "&t=9s")
        self.assertEqual(started[0][:4], ["systemd-run", "--user", "--scope", "--quiet"])
        self.assertEqual(started[0][started[0].index("--") + 1], "firefox")


class WorkspaceChoice(unittest.TestCase):
    def test_the_pc_monitor_is_the_focused_one_never_the_phone_output(self):
        mons = monitors(pc_workspace=3, phone_workspace=9, phone_focused=True)
        self.assertEqual(ryoku_link.pc_monitor(mons)["name"], "eDP-1")
        self.assertIsNone(ryoku_link.pc_monitor([m for m in mons if m["name"] == ryoku_link.OUTPUT]))

    def test_a_window_already_on_the_workspace_is_only_focused(self):
        window = {"address": "0xabc", "workspace": {"id": 5}}
        steps = ryoku_link.show_steps(window, 5)
        self.assertEqual(len(steps), 2)
        self.assertIn("workspace = 5", steps[0])
        self.assertIn('"address:0xabc"', steps[1])

    def test_a_window_somewhere_else_is_moved_there_first(self):
        steps = ryoku_link.show_steps({"address": "0xabc", "workspace": {"id": 2}}, 5)
        self.assertEqual(len(steps), 3)
        self.assertIn("window.move", steps[0])
        self.assertIn("workspace = 5", steps[0])

    def test_the_new_window_is_the_one_that_was_not_there(self):
        clients = [{"address": "0x1"}, {"address": "0x2"}, {"address": "0x3", "mapped": False}]
        self.assertEqual(ryoku_link.new_window({"0x1"}, clients)["address"], "0x2")
        self.assertIsNone(ryoku_link.new_window({"0x1", "0x2"}, clients))


class OpenOnNewWorkspace(unittest.TestCase):
    TARGET = f"https://www.youtube.com/watch?v={VIDEO}&t=77s"

    def run_handoff(self, clients_after, pc_workspace=1, workspaces=()):
        calls = []
        shown = iter([[{"address": "0xold", "workspace": {"id": 1}}], *[clients_after] * 50])

        def hypr(*args):
            if args[0] == "clients":
                return next(shown)
            return {"monitors": monitors(pc_workspace), "workspaces": list(workspaces)}[args[0]]

        def dispatched(*lua):
            calls.append(("dispatch", lua))
            return True

        with mock.patch.object(ryoku_link, "hyprctl", hypr), mock.patch.object(ryoku_link, "dispatch", dispatched), \
                mock.patch.object(ryoku_link, "launch_browser", lambda url: calls.append(("launch", url)) or ["browser", url]), \
                mock.patch.object(ryoku_link, "BROWSER_WINDOW_TIMEOUT", 0.3):
            try:
                result = ryoku_link.open_on_new_workspace(self.TARGET)
            except ryoku_link.HandoffError as e:
                result = e
        return result, calls

    def test_the_pc_goes_to_the_empty_workspace_before_the_browser_starts_and_ends_on_the_window(self):
        old = {"address": "0xold", "workspace": {"id": 1}}
        new = {"address": "0xnew", "workspace": {"id": 2}}
        result, calls = self.run_handoff([old, new], workspaces=[{"id": 1, "windows": 1}])
        self.assertEqual(result, 2)
        kinds = [c[0] for c in calls]
        self.assertEqual(kinds, ["dispatch", "launch", "dispatch"])
        self.assertIn("workspace = 2", calls[0][1][1])
        self.assertEqual(calls[1][1], self.TARGET)
        self.assertIn("workspace = 2", calls[2][1][0])
        self.assertIn('"address:0xnew"', calls[2][1][1])

    def test_a_window_the_browser_put_elsewhere_is_moved_to_the_new_workspace(self):
        result, calls = self.run_handoff([{"address": "0xold", "workspace": {"id": 1}}, {"address": "0xnew", "workspace": {"id": 4}}])
        self.assertEqual(result, 2)
        self.assertIn("window.move", calls[-1][1][0])

    def test_no_new_window_is_reported_and_the_pc_goes_back(self):
        result, calls = self.run_handoff([{"address": "0xold", "workspace": {"id": 1}}], pc_workspace=1)
        self.assertIsInstance(result, ryoku_link.HandoffError)
        self.assertIn("no new window", str(result))
        self.assertIn("workspace = 1", calls[-1][1][0])

    def test_a_browser_that_cannot_start_is_reported_and_the_pc_goes_back(self):
        calls = []
        with mock.patch.object(ryoku_link, "hyprctl", lambda *a: {"monitors": monitors(3), "workspaces": [], "clients": []}[a[0]]), \
                mock.patch.object(ryoku_link, "dispatch", lambda *lua: calls.append(lua) or True), \
                mock.patch.object(ryoku_link, "launch_browser", mock.Mock(side_effect=FileNotFoundError(2, "No such file"))):
            with self.assertRaises(ryoku_link.HandoffError):
                ryoku_link.open_on_new_workspace(self.TARGET)
        self.assertIn("workspace = 3", calls[-1][0])


class OpenUrlCommand(unittest.TestCase):
    def send(self, msg):
        sent, opened, dispatched = [], [], []

        async def fake_reply(writer, message):
            sent.append(message)

        async def go():
            with mock.patch.object(ryoku_link, "reply", fake_reply), \
                    mock.patch.object(ryoku_link, "open_player", lambda url: opened.append(url) or 4), \
                    mock.patch.object(ryoku_link, "dispatch", lambda *lua: dispatched.append(lua) or True):
                await ryoku_link.on_command(None, {"cmd": "open_url", **msg})

        asyncio.run(go())
        return sent, opened, dispatched

    def test_a_youtube_link_is_opened_at_the_position_and_the_phone_is_told_the_workspace(self):
        sent, opened, _ = self.send({"url": f"https://youtu.be/{VIDEO}?si=x", "position_seconds": 125})
        self.assertEqual(opened, [f"https://www.youtube.com/watch?v={VIDEO}&t=125s"])
        self.assertEqual(sent, [{"type": "open_url", "workspace": 4}])

    def test_anything_else_is_refused_before_the_desktop_is_touched(self):
        for msg in ({"url": "https://evil.com/watch?v=" + VIDEO}, {"url": f"https://youtu.be/{VIDEO};reboot"}, {"url": "$(id)"}, {},
                    {"url": f"https://youtu.be/{VIDEO}", "position_seconds": "9; id"}, {"url": 5}):
            sent, opened, dispatched = self.send(msg)
            self.assertEqual((opened, dispatched), ([], []), msg)
            self.assertEqual(sent, [{"type": "open_url", "error": "not a YouTube video link"}], msg)

    def test_a_failed_handoff_tells_the_phone_why(self):
        sent = []

        async def fake_reply(writer, message):
            sent.append(message)

        async def go():
            with mock.patch.object(ryoku_link, "reply", fake_reply), \
                    mock.patch.object(ryoku_link, "open_player", mock.Mock(side_effect=ryoku_link.HandoffError("the browser opened no new window"))):
                await ryoku_link.on_command(None, {"cmd": "open_url", "url": f"https://youtu.be/{VIDEO}"})

        asyncio.run(go())
        self.assertEqual(sent, [{"type": "open_url", "error": "the browser opened no new window"}])

    def test_no_text_from_the_phone_reaches_a_shell_or_a_desktop_command(self):
        """The whole path, with the real launcher and workspace code and only the processes faked: the
        phone's hostile text is in the link and the minute, and what runs is an argument list holding only
        the rebuilt address, with no hyprctl line carrying any of it."""
        hostile = f"https://youtu.be/{VIDEO}?si=$(touch /tmp/pwned)&x=`id`;reboot&t=1#\"; rm -rf ~ #"
        popen, batches = [], []
        old = [{"address": "0xold", "workspace": {"id": 1}}]
        shown = iter([old, *[old + [{"address": "0xnew", "workspace": {"id": 2}}]] * 50])

        def hypr(*args):
            return next(shown) if args[0] == "clients" else {"monitors": monitors(1), "workspaces": []}[args[0]]

        def run(argv, **kw):
            if argv[0] == "hyprctl":
                batches.append(argv[-1])
                return mock.Mock(stdout="ok\n")
            return mock.Mock(stdout="firefox.desktop\n")

        async def go():
            with mock.patch.object(ryoku_link, "reply", mock.AsyncMock()), mock.patch.object(ryoku_link, "hyprctl", hypr), \
                    mock.patch.object(ryoku_link, "PLAYER_FILE", os.path.join(tempfile.mkdtemp(), "player.json")), \
                    mock.patch.object(ryoku_link, "enter_fullscreen", lambda address: True), \
                    mock.patch.object(ryoku_link.subprocess, "run", run), \
                    mock.patch.object(ryoku_link, "read_desktop_file", lambda name: "[Desktop Entry]\nExec=firefox %u\n"), \
                    mock.patch.object(ryoku_link.shutil, "which", lambda name: None), \
                    mock.patch.object(ryoku_link.subprocess, "Popen", lambda argv, **kw: popen.append((argv, kw))):
                await ryoku_link.on_command(None, {"cmd": "open_url", "url": hostile, "position_seconds": 42})

        asyncio.run(go())
        (argv, kw), = popen
        self.assertEqual(argv, ["firefox", "--new-window", f"https://www.youtube.com/watch?v={VIDEO}&t=42s"])
        self.assertFalse(kw.get("shell", False))
        for text in [*argv, *batches]:
            for fragment in ("$(", "`", "rm -rf", "reboot", "pwned", "si="):
                self.assertNotIn(fragment, text)
        self.assertTrue(batches)


class PlayerWindow(unittest.TestCase):
    URL = f"https://www.youtube.com/watch?v={VIDEO}&t=77s"

    def setUp(self):
        self.file = os.path.join(tempfile.mkdtemp(), "ryoku-link", "player.json")
        patch = mock.patch.object(ryoku_link, "PLAYER_FILE", self.file)
        patch.start()
        self.addCleanup(patch.stop)

    def run_show(self, clients, active, saved="0xa1"):
        if saved:
            ryoku_link.save_player(saved)
        ran, dispatched = [], []

        def hypr(*args):
            return {"clients": clients, "monitors": monitors(1), "activewindow": {"address": active}}[args[0]]

        with mock.patch.object(ryoku_link, "hyprctl", hypr), \
                mock.patch.object(ryoku_link, "dispatch", lambda *lua: dispatched.append(lua) or True), \
                mock.patch.object(ryoku_link, "FOCUS_TIMEOUT", 0.2), mock.patch.object(ryoku_link.time, "sleep", lambda s: None), \
                mock.patch.object(ryoku_link.subprocess, "run", lambda argv, **kw: ran.append(argv) or mock.Mock(returncode=0, stderr="")):
            return ryoku_link.show_player(self.URL), ran, dispatched

    def test_an_existing_player_is_focused_and_gets_the_address_typed(self):
        result, ran, dispatched = self.run_show([{"address": "0xa1", "workspace": {"id": 3}}], "0xa1")
        self.assertEqual(result, 3)
        self.assertEqual(ran, [["wtype", "-M", "ctrl", "-k", "l", "-m", "ctrl"], ["wtype", "--", self.URL], ["wtype", "-k", "Return"]])
        self.assertIn("workspace = 3", dispatched[0][1])
        self.assertIn('"address:0xa1"', dispatched[0][2])

    def test_a_missing_player_types_nothing(self):
        result, ran, _ = self.run_show([{"address": "0xb2", "workspace": {"id": 1}}], "0xb2")
        self.assertEqual((result, ran), (None, []))

    def test_no_saved_player_types_nothing(self):
        self.assertEqual(self.run_show([{"address": "0xa1", "workspace": {"id": 1}}], "0xa1", saved=None)[:2], (None, []))

    def test_unconfirmed_focus_types_nothing(self):
        result, ran, _ = self.run_show([{"address": "0xa1", "workspace": {"id": 3}}], "0xc0ffee")
        self.assertEqual((result, ran), (None, []))

    def test_player_falls_back_to_a_new_window_that_becomes_the_player(self):
        with mock.patch.object(ryoku_link, "show_player", lambda url: None), \
                mock.patch.object(ryoku_link, "enter_fullscreen", lambda address: True), \
                mock.patch.object(ryoku_link, "open_on_new_workspace", lambda url, on_window: on_window("0xbeef") or 2):
            self.assertEqual(ryoku_link.open_player(self.URL), 2)
        self.assertEqual(ryoku_link.load_player(), "0xbeef")

    def test_the_player_survives_a_restart_and_a_bad_file_is_ignored(self):
        ryoku_link.save_player("0xabc")
        self.assertEqual(ryoku_link.load_player(), "0xabc")
        for bad in ("not json", '{"address": "$(id)"}', "[]"):
            with open(self.file, "w") as f:
                f.write(bad)
            self.assertIsNone(ryoku_link.load_player(), bad)

    def test_only_a_rebuilt_watch_address_can_be_typed(self):
        self.assertEqual(ryoku_link.navigate_argv(self.URL)[1], ["wtype", "--", self.URL])
        for bad in ("https://evil.com/watch?v=" + VIDEO + "&t=1s", self.URL + ";id", self.URL.replace("77s", "x"), "-k Return"):
            with self.assertRaises(ValueError):
                ryoku_link.navigate_argv(bad)


class PlayerFullscreen(unittest.TestCase):
    """The player window ends every send fullscreen, and a re-send never types into a fullscreen page."""
    URL = f"https://www.youtube.com/watch?v={VIDEO}&t=77s"
    ADDRESS = "0xa1"

    def setUp(self):
        self.file = os.path.join(tempfile.mkdtemp(), "ryoku-link", "player.json")
        patch = mock.patch.object(ryoku_link, "PLAYER_FILE", self.file)
        patch.start()
        self.addCleanup(patch.stop)
        ryoku_link.save_player(self.ADDRESS)

    def send(self, fullscreen, f_takes=True, active=None, leaves=True):
        """One send to a player that is `fullscreen` (Hyprland's 0/1/2) before it. Returns what happened, in order."""
        world = {"fs": fullscreen, "events": []}

        def hypr(*args):
            if args[0] == "activewindow":
                return {"address": active or self.ADDRESS}
            return {"clients": [{"address": self.ADDRESS, "workspace": {"id": 3}, "fullscreen": world["fs"]}], "monitors": monitors(1)}[args[0]]

        def dispatched(*lua):
            for expr in lua:
                if "action = \"unset\"" in expr:
                    world["events"].append("unset")
                    world["fs"] = 0 if leaves else world["fs"]
                elif "action = \"set\"" in expr:
                    world["events"].append("set")
                    world["fs"] = 2
            return True

        def run(argv, **kw):
            if argv == ryoku_link.FULLSCREEN_KEY:
                world["events"].append("f")
                if f_takes:
                    world["fs"] = 2
            else:
                world["events"].append(("typed", world["fs"], argv))
            return mock.Mock(returncode=0, stderr="")

        with mock.patch.object(ryoku_link, "hyprctl", hypr), mock.patch.object(ryoku_link, "dispatch", dispatched), \
                mock.patch.object(ryoku_link, "FOCUS_TIMEOUT", 0.2), mock.patch.object(ryoku_link, "FULLSCREEN_TIMEOUT", 0.2), \
                mock.patch.object(ryoku_link.time, "sleep", lambda s: None), mock.patch.object(ryoku_link.subprocess, "run", run):
            try:
                result = ryoku_link.open_player(self.URL)
            except ryoku_link.HandoffError as e:
                result = e
        return result, world["fs"], world["events"]

    def test_a_resend_to_a_fullscreen_player_leaves_fullscreen_before_typing_and_presses_f_once(self):
        result, fs, events = self.send(fullscreen=2)
        self.assertEqual(result, 3)
        self.assertEqual(events[0], "unset")
        typed = [e for e in events if isinstance(e, tuple)]
        self.assertEqual(len(typed), 3)
        self.assertTrue(all(fs_then == 0 for _, fs_then, _ in typed), "typed into a fullscreen page")
        self.assertEqual(events.count("f"), 1)
        self.assertEqual(events.index("f"), len(events) - 1, "f is pressed after the address is typed")
        self.assertEqual(fs, 2)

    def test_a_maximized_player_is_typed_into_without_leaving_and_f_is_not_mistaken_for_done(self):
        result, fs, events = self.send(fullscreen=1)
        self.assertEqual(result, 3)
        self.assertNotIn("unset", events)
        self.assertEqual(len([e for e in events if isinstance(e, tuple)]), 3)
        self.assertEqual(events.count("f"), 1)
        self.assertEqual(fs, 2)
        # f that does not take on a maximized window still falls through to Hyprland's fullscreen
        _, fs, events = self.send(fullscreen=1, f_takes=False)
        self.assertEqual((events.count("f"), events[-1], fs), (2, "set", 2))

    def test_a_new_player_window_is_made_fullscreen_too(self):
        entered = []
        with mock.patch.object(ryoku_link, "show_player", lambda url: None), \
                mock.patch.object(ryoku_link, "open_on_new_workspace", lambda url, on_window: on_window("0xbeef") or 5), \
                mock.patch.object(ryoku_link, "enter_fullscreen", lambda address: entered.append(address) or True):
            self.assertEqual(ryoku_link.open_player(self.URL), 5)
        self.assertEqual(entered, ["0xbeef"])

    def test_a_first_send_to_a_windowed_player_just_presses_f(self):
        _, fs, events = self.send(fullscreen=0)
        self.assertNotIn("unset", events)
        self.assertNotIn("set", events)
        self.assertEqual(events.count("f"), 1)
        self.assertEqual(fs, 2)

    def test_two_sends_in_a_row_each_end_fullscreen_without_f_toggling_it_off(self):
        _, fs, first = self.send(fullscreen=0)
        _, fs, second = self.send(fullscreen=fs)
        self.assertEqual((first.count("f"), second.count("f")), (1, 1))
        self.assertEqual(fs, 2)

    def test_when_f_does_not_take_it_is_tried_once_more_then_hyprlands_fullscreen_is_used(self):
        result, fs, events = self.send(fullscreen=0, f_takes=False)
        self.assertEqual(result, 3)
        self.assertEqual(events.count("f"), 2)
        self.assertEqual(events[-1], "set")
        self.assertEqual(fs, 2)

    def test_a_player_that_will_not_leave_fullscreen_is_not_typed_into(self):
        result, _, events = self.send(fullscreen=2, leaves=False)
        self.assertIsInstance(result, ryoku_link.HandoffError)
        self.assertEqual([e for e in events if e != "unset"], [])

    def test_f_is_never_pressed_when_focus_is_not_on_the_player(self):
        ran = []
        with mock.patch.object(ryoku_link, "hyprctl", lambda *a: {"activewindow": {"address": "0xother"}}[a[0]]), \
                mock.patch.object(ryoku_link, "FOCUS_TIMEOUT", 0.2), mock.patch.object(ryoku_link.time, "sleep", lambda s: None), \
                mock.patch.object(ryoku_link.subprocess, "run", lambda argv, **kw: ran.append(argv)):
            self.assertFalse(ryoku_link.enter_fullscreen(self.ADDRESS))
        self.assertEqual(ran, [])

    def test_the_dispatcher_names_the_window_and_only_takes_set_or_unset(self):
        self.assertEqual(ryoku_link.fullscreen_dispatch("0xa1", "set"),
                         'hl.dsp.window.fullscreen({ mode = "fullscreen", action = "set", window = "address:0xa1" })')
        with self.assertRaises(ValueError):
            ryoku_link.fullscreen_dispatch("0xa1", "toggle")


class WpctlSink:
    """A wpctl that keeps one sink's volume, as the real one does: a relative step is limited by `-l`."""
    def __init__(self, level, muted=False):
        self.level, self.muted, self.calls = level, muted, []

    def run(self, argv, **kw):
        self.calls.append((argv, kw))
        if argv[:2] == ["wpctl", "get-volume"]:
            return mock.Mock(returncode=0, stdout=f"Volume: {self.level / 100:.2f}{' [MUTED]' if self.muted else ''}\n", stderr="")
        sign = 1 if argv[3].endswith("+") else -1
        limit = float(argv[argv.index("-l") + 1]) * 100 if "-l" in argv else float("inf")
        self.level = max(0, min(limit, self.level + sign * int(argv[3][:-2]))) if sign > 0 else max(0, self.level - int(argv[3][:-2]))
        return mock.Mock(returncode=0, stdout="", stderr="")


class PcVolume(unittest.TestCase):
    def change(self, sink, step):
        with mock.patch.object(ryoku_link.subprocess, "run", sink.run):
            return ryoku_link.change_volume(step)

    def test_a_step_is_five_points_of_the_default_sink_capped_at_full_volume(self):
        self.assertEqual(ryoku_link.volume_argv(1), ["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", "5%+", "-l", "1.0"])
        self.assertEqual(ryoku_link.volume_argv(-1), ["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", "5%-", "-l", "1.0"])
        for bad in (0, 2, -2, 5, None, "1"):
            with self.assertRaises(ValueError):
                ryoku_link.volume_argv(bad)

    def test_steps_move_the_level_and_never_pass_the_limits(self):
        self.assertEqual(self.change(WpctlSink(35), 1), (40, False))
        self.assertEqual(self.change(WpctlSink(35), -1), (30, False))
        self.assertEqual(self.change(WpctlSink(98), 1), (100, False))
        self.assertEqual(self.change(WpctlSink(100), 1), (100, False))
        self.assertEqual(self.change(WpctlSink(2), -1), (0, False))

    def test_a_muted_sink_says_so(self):
        self.assertEqual(self.change(WpctlSink(35, muted=True), 1), (40, True))

    def test_commands_are_argument_lists_run_without_a_shell(self):
        sink = WpctlSink(35)
        self.change(sink, 1)
        self.assertEqual([argv[:2] for argv, _ in sink.calls], [["wpctl", "set-volume"], ["wpctl", "get-volume"]])
        for argv, kw in sink.calls:
            self.assertIsInstance(argv, list)
            self.assertFalse(kw.get("shell", False))

    def test_wpctl_output_is_read_strictly(self):
        self.assertEqual(ryoku_link.parse_volume("Volume: 0.35\n"), (35, False))
        self.assertEqual(ryoku_link.parse_volume("Volume: 1.00 [MUTED]\n"), (100, True))
        for bad in ("", "Volume: loud", "Volume: 0.35 [MUTED] extra", "Failed to find"):
            self.assertIsNone(ryoku_link.parse_volume(bad), bad)

    def test_a_failing_wpctl_is_an_error_not_a_level(self):
        failed = mock.Mock(returncode=1, stdout="", stderr="no such node")
        with mock.patch.object(ryoku_link.subprocess, "run", lambda argv, **kw: failed):
            with self.assertRaises(ryoku_link.VolumeError):
                ryoku_link.change_volume(1)
        with mock.patch.object(ryoku_link.subprocess, "run", mock.Mock(side_effect=FileNotFoundError(2, "No such file"))):
            with self.assertRaises(ryoku_link.VolumeError):
                ryoku_link.change_volume(1)

    def command(self, msg, sink=None):
        sent = []

        async def fake_reply(writer, message):
            sent.append(message)

        async def go():
            with mock.patch.object(ryoku_link, "reply", fake_reply), mock.patch.object(ryoku_link.subprocess, "run", (sink or WpctlSink(35)).run):
                await ryoku_link.on_command(None, {"cmd": "volume", **msg})

        asyncio.run(go())
        return sent

    def test_the_phone_is_answered_with_the_new_level(self):
        self.assertEqual(self.command({"step": 1}), [{"type": "volume", "level": 40, "muted": False}])
        self.assertEqual(self.command({"step": -1}), [{"type": "volume", "level": 30, "muted": False}])

    def test_anything_but_a_step_of_one_is_refused_before_wpctl_runs(self):
        for step in (0, 2, -5, True, 1.0, "1", None, [1]):
            sink = WpctlSink(35)
            self.assertEqual(self.command({"step": step}, sink), [{"type": "volume", "error": "not a volume step"}], step)
            self.assertEqual(sink.calls, [], step)
        self.assertEqual(self.command({})[0]["type"], "volume")

    def test_a_failure_is_reported_to_the_phone(self):
        failed = mock.Mock(returncode=1, stdout="", stderr="no such node")
        sink = mock.Mock(run=lambda argv, **kw: failed)
        sent = self.command({"step": 1}, sink)
        self.assertEqual(sent[0]["type"], "volume")
        self.assertIn("failed", sent[0]["error"])


class PlayPause(unittest.TestCase):
    """The play/pause button types `k` into the tracked player window only."""
    ADDRESS = "0xa1"

    def setUp(self):
        self.file = os.path.join(tempfile.mkdtemp(), "ryoku-link", "player.json")
        patch = mock.patch.object(ryoku_link, "PLAYER_FILE", self.file)
        patch.start()
        self.addCleanup(patch.stop)

    def command(self, clients, active, returncode=0):
        sent, ran = [], []

        async def fake_reply(writer, message):
            sent.append(message)

        def hypr(*args):
            return {"clients": clients, "activewindow": {"address": active}}[args[0]]

        def run(argv, **kw):
            ran.append(argv)
            return mock.Mock(returncode=returncode, stderr="no keyboard")

        async def go():
            with mock.patch.object(ryoku_link, "reply", fake_reply), mock.patch.object(ryoku_link, "hyprctl", hypr), \
                    mock.patch.object(ryoku_link, "dispatch", lambda *lua: True), mock.patch.object(ryoku_link, "FOCUS_TIMEOUT", 0.2), \
                    mock.patch.object(ryoku_link.subprocess, "run", run):
                await ryoku_link.on_command(None, {"cmd": "playpause"})

        asyncio.run(go())
        return sent, ran

    def test_k_is_typed_as_an_argv_list_once_the_player_is_focused(self):
        ryoku_link.save_player(self.ADDRESS)
        sent, ran = self.command([{"address": self.ADDRESS}], self.ADDRESS)
        self.assertEqual(sent, [{"type": "playpause"}])
        self.assertEqual(ran, [["wtype", "-k", "k"]])

    def test_nothing_is_typed_when_another_window_has_focus(self):
        ryoku_link.save_player(self.ADDRESS)
        sent, ran = self.command([{"address": self.ADDRESS}], "0xb2")
        self.assertEqual(ran, [])
        self.assertEqual(sent[0]["type"], "playpause")
        self.assertIn("focus", sent[0]["error"])

    def test_a_gone_player_is_an_error_and_nothing_is_typed(self):
        ryoku_link.save_player(self.ADDRESS)
        for clients in ([], [{"address": "0xb2"}]):
            sent, ran = self.command(clients, "0xb2")
            self.assertEqual(sent, [{"type": "playpause", "error": "the PC player window is gone"}])
            self.assertEqual(ran, [])

    def test_no_remembered_player_is_an_error(self):
        sent, ran = self.command([{"address": self.ADDRESS}], self.ADDRESS)
        self.assertIn("gone", sent[0]["error"])
        self.assertEqual(ran, [])

    def test_a_wtype_failure_is_reported(self):
        ryoku_link.save_player(self.ADDRESS)
        sent, _ = self.command([{"address": self.ADDRESS}], self.ADDRESS, returncode=1)
        self.assertIn("wtype failed", sent[0]["error"])

    def test_the_bridge_announces_it(self):
        self.assertIn("playpause", ryoku_link.snapshot()["features"])


class FindingTheVideo(unittest.TestCase):
    """The phone reports title, channel, length and position; the bridge picks the video from a search."""

    RICK = {"id": "dQw4w9WgXcQ", "title": "Rick Astley - Never Gonna Give You Up (Official Video)", "channel": "Rick Astley", "duration": 214}

    def result(self, **over):
        return {**self.RICK, **over}

    def pick(self, candidates, title=None, channel="Rick Astley", duration=214):
        return ryoku_link.pick_video(candidates, title or self.RICK["title"], channel, duration)

    def test_the_result_with_the_same_title_channel_and_length_wins(self):
        decoy = self.result(id="AAAAAAAAAAA", channel="Super Top Songs")
        self.assertEqual(self.pick([decoy, self.result()]), "dQw4w9WgXcQ")

    def test_a_cover_with_the_same_title_but_another_length_is_not_the_video(self):
        self.assertIsNone(self.pick([self.result(duration=240)]))
        self.assertEqual(self.pick([self.result(duration=215)]), "dQw4w9WgXcQ")  # within the slack

    def test_another_channel_is_not_the_video(self):
        self.assertIsNone(self.pick([self.result(channel="Super Top Songs")]))

    def test_punctuation_case_and_a_topic_suffix_do_not_matter(self):
        found = self.result(title="rick astley – never gonna give you up (official video)", channel="Rick Astley - Topic")
        self.assertEqual(self.pick([found]), "dQw4w9WgXcQ")

    def test_a_different_title_is_not_the_video(self):
        self.assertIsNone(self.pick([self.result(title="Never Gonna Give You Up (Lyrics)")]))

    def test_the_exact_title_beats_a_similar_one_that_came_first(self):
        similar = self.result(id="BBBBBBBBBBB", title=self.RICK["title"] + " 4K")
        self.assertEqual(self.pick([similar, self.result()]), "dQw4w9WgXcQ")

    def test_without_a_channel_only_an_exact_title_matches(self):
        self.assertEqual(self.pick([self.result()], channel=None), "dQw4w9WgXcQ")
        self.assertIsNone(self.pick([self.result(title=self.RICK["title"] + " 4K")], channel=None))

    def test_a_result_without_a_plain_video_id_or_length_is_never_picked(self):
        self.assertIsNone(self.pick([self.result(id="$(id)"), self.result(id="short"), self.result(duration=None)]))

    def test_the_same_video_is_searched_once(self):
        ryoku_link.resolved_videos.clear()
        search = mock.Mock(return_value=[self.result()])
        for title in (self.RICK["title"], self.RICK["title"].upper()):
            self.assertEqual(ryoku_link.resolve_video(title, "Rick Astley", 214, search), "dQw4w9WgXcQ")
        self.assertEqual(search.call_count, 1)

    def test_a_video_nobody_matched_is_not_cached_and_is_refused(self):
        ryoku_link.resolved_videos.clear()
        search = mock.Mock(return_value=[])
        for _ in range(2):
            with self.assertRaises(ryoku_link.HandoffError):
                ryoku_link.resolve_video("x", "y", 100, search)
        self.assertEqual((search.call_count, ryoku_link.resolved_videos), (2, {}))

    def test_the_cache_forgets_its_oldest_entry_when_full(self):
        ryoku_link.resolved_videos.clear()
        with mock.patch.object(ryoku_link, "CACHE_SIZE", 2):
            for n in range(3):
                ryoku_link.resolve_video(f"t{n}", "c", 100, lambda q, n=n: [{"id": f"{n}" * 11, "title": f"t{n}", "channel": "c", "duration": 100}])
        self.assertEqual(list(ryoku_link.resolved_videos.values()), ["1" * 11, "2" * 11])

    def test_the_search_asks_yt_dlp_with_an_argument_list_and_the_query_after_the_prefix(self):
        out = json.dumps(self.RICK) + "\nnot json\n"
        with mock.patch.object(ryoku_link.shutil, "which", lambda name: "/usr/bin/yt-dlp"), \
                mock.patch.object(ryoku_link.subprocess, "run", mock.Mock(return_value=mock.Mock(stdout=out, returncode=0))) as run:
            self.assertEqual(ryoku_link.search_youtube("--exec id; $(id)"), [self.RICK])
        argv = run.call_args.args[0]
        self.assertEqual(argv[-1], "ytsearch5:--exec id; $(id)")
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_a_missing_yt_dlp_is_said_plainly(self):
        with mock.patch.object(ryoku_link.shutil, "which", lambda name: None), self.assertRaisesRegex(ryoku_link.HandoffError, "yt-dlp is not installed"):
            ryoku_link.search_youtube("x")


class PlaybackPosition(unittest.TestCase):
    def seconds(self, **msg):
        return ryoku_link.playback_seconds({"position_ms": 60_000, **msg}, 600)

    def test_a_paused_session_reports_its_position_as_it_is(self):
        self.assertEqual(self.seconds(playing=False, age_ms=999_000), 60)

    def test_a_playing_session_moves_on_by_the_time_since_its_report_times_the_speed(self):
        self.assertEqual(self.seconds(playing=True, age_ms=30_000, speed=1.0), 90)
        self.assertEqual(self.seconds(playing=True, age_ms=30_000, speed=2.0), 120)
        self.assertEqual(self.seconds(playing=True, age_ms=1_999, speed=1.0), 61)  # whole seconds, rounded down

    def test_it_never_runs_past_the_video(self):
        self.assertEqual(self.seconds(playing=True, age_ms=900_000, speed=1.0), 600)

    def test_a_missing_or_hostile_position_is_unknown_not_zero(self):
        for msg in ({}, {"position_ms": None}, {"position_ms": -1}, {"position_ms": "9; id"}, {"position_ms": True}):
            self.assertIsNone(ryoku_link.playback_seconds(msg, 600), msg)
        for msg in ({"age_ms": -5}, {"speed": 0}, {"speed": "fast"}, {"speed": True}):
            self.assertIsNone(ryoku_link.playback_seconds({"position_ms": 1000, "playing": True, **msg}, 600), msg)


class OpenVideoCommand(unittest.TestCase):
    ROW = {"id": VIDEO, "title": "Some Song", "channel": "Some Band", "duration": 200}
    MSG = {"cmd": "open_video", "title": "Some Song", "channel": "Some Band", "duration_ms": 200_000,
           "position_ms": 10_000, "age_ms": 5_000, "speed": 1.0, "playing": True}

    def send(self, msg, rows):
        sent, opened = [], []
        ryoku_link.resolved_videos.clear()

        async def fake_reply(writer, message):
            sent.append(message)

        async def go():
            with mock.patch.object(ryoku_link, "reply", fake_reply), \
                    mock.patch.object(ryoku_link, "search_youtube", lambda query: rows), \
                    mock.patch.object(ryoku_link, "open_player", lambda url: opened.append(url) or 4):
                await ryoku_link.on_command(None, msg)

        asyncio.run(go())
        return sent, opened

    def test_the_video_is_found_and_opened_at_the_real_position(self):
        sent, opened = self.send(self.MSG, [self.ROW])
        self.assertEqual(opened, [f"https://www.youtube.com/watch?v={VIDEO}&t=15s"])
        self.assertEqual(sent, [{"type": "open_url", "workspace": 4}])

    def test_an_unknown_position_opens_the_start_instead_of_failing(self):
        sent, opened = self.send({**self.MSG, "position_ms": None}, [self.ROW])
        self.assertEqual(opened, [f"https://www.youtube.com/watch?v={VIDEO}&t=0s"])

    def test_no_confident_match_opens_nothing_and_says_how_to_send_by_hand(self):
        sent, opened = self.send(self.MSG, [{**self.ROW, "duration": 999}])
        self.assertEqual(opened, [])
        self.assertEqual(sent, [{"type": "open_url", "error": "could not tell which video this is: use Share > Send to PC"}])

    def test_a_message_without_a_usable_title_or_length_is_refused_before_any_search(self):
        for bad in ({"title": None}, {"title": "   "}, {"title": 5}, {"duration_ms": 0}, {"duration_ms": "9"}, {"duration_ms": -1}, {"duration_ms": 10 ** 12}):
            search = mock.Mock()
            with mock.patch.object(ryoku_link, "search_youtube", search):
                with self.assertRaises(ryoku_link.HandoffError, msg=bad):
                    ryoku_link.video_target({**self.MSG, **bad})
            search.assert_not_called()

    def test_hostile_text_only_ever_becomes_a_search_query_never_part_of_the_address(self):
        queries = []
        hostile = {**self.MSG, "title": "x$(touch /tmp/pwned)`id`;\n\x00reboot", "channel": "\"; rm -rf ~ #"}
        with mock.patch.object(ryoku_link, "search_youtube", lambda q: queries.append(q) or [{"id": VIDEO, "title": ryoku_link.clean_text(hostile["title"]),
                                                                                                 "channel": hostile["channel"], "duration": 200}]):
            ryoku_link.resolved_videos.clear()
            target = ryoku_link.video_target(hostile)
        self.assertEqual(target, f"https://www.youtube.com/watch?v={VIDEO}&t=15s")
        self.assertTrue(queries[0].isprintable(), repr(queries[0]))

    def test_a_lookup_ahead_of_the_tap_makes_the_tap_search_nothing(self):
        ryoku_link.resolved_videos.clear()
        rows = [{"id": VIDEO, "title": "Some Song", "channel": "Some Band", "duration": 200}]
        search = mock.Mock(return_value=rows)

        async def go():
            with mock.patch.object(ryoku_link, "search_youtube", search), mock.patch.object(ryoku_link, "reply", mock.AsyncMock()), \
                    mock.patch.object(ryoku_link, "open_player", lambda url: 4):
                await ryoku_link.on_command(None, {"cmd": "resolve_video", "title": "Some Song", "channel": "Some Band", "duration_ms": 200_000})
                await ryoku_link.on_command(None, self.MSG)

        asyncio.run(go())
        self.assertEqual(search.call_count, 1)

    def test_a_lookup_that_finds_nothing_is_dropped_quietly(self):
        ryoku_link.resolved_videos.clear()
        with mock.patch.object(ryoku_link, "search_youtube", lambda q: []):
            ryoku_link.warm_video(self.MSG)  # must not raise
            ryoku_link.warm_video({"title": 5})
        self.assertEqual(ryoku_link.resolved_videos, {})


if __name__ == "__main__":
    unittest.main()
