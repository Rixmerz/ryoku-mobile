"""Run with: python3 -m unittest discover -s pc"""

import asyncio
import os
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


def monitors(pc_workspace=6, phone_workspace=None):
    out = [{"name": "eDP-1", "focused": True, "activeWorkspace": {"id": pc_workspace}, "x": 0, "y": 0, "width": 1920, "height": 1080}]
    if phone_workspace is not None:
        out.append({"name": ryoku_link.OUTPUT, "focused": False, "activeWorkspace": {"id": phone_workspace}, "x": 1536, "y": 0, "width": 864, "height": 1920})
    return out


def fake_hyprctl(mons, workspaces):
    def run(*args):
        return {"monitors": mons, "workspaces": workspaces, "cursorpos": {"x": 10, "y": 20}}[args[0]]
    return run


class ShowWorkspace(unittest.TestCase):
    def setUp(self):
        self.view = ryoku_link.PhoneView(None, 1080, 2400)

    def show(self, ws, mons, workspaces):
        with mock.patch.object(ryoku_link, "hyprctl", fake_hyprctl(mons, workspaces)), \
                mock.patch.object(ryoku_link, "dispatch", return_value=True) as d:
            return self.view.show(ws), d

    def test_workspace_on_the_pc_monitor_is_refused_without_touching_anything(self):
        shown, d = self.show(6, monitors(6, 4), [{"id": 6, "monitor": "eDP-1"}])
        self.assertFalse(shown)
        d.assert_not_called()

    def test_free_workspace_is_pulled_on_the_phone_output_and_focus_goes_back(self):
        shown, d = self.show(3, monitors(6, 4), [{"id": 3, "monitor": "eDP-1"}, {"id": 6, "monitor": "eDP-1"}, {"id": 4, "monitor": ryoku_link.OUTPUT}])
        self.assertTrue(shown)
        steps = d.call_args.args
        self.assertEqual(steps[0], ryoku_link.focus_monitor(ryoku_link.OUTPUT))
        self.assertIn(ryoku_link.focus_workspace(3, on_current_monitor=True), steps)
        self.assertEqual(steps[-2], ryoku_link.focus_monitor("eDP-1"))  # seat handed back (then the cursor)
        self.assertEqual(self.view.pulled, {3: ("eDP-1", 6)})

    def test_leaving_a_pulled_workspace_sends_it_home_and_restores_the_pc_view(self):
        self.view.pulled = {3: ("eDP-1", 6)}
        shown, d = self.show(5, monitors(6, 3), [{"id": 3, "monitor": ryoku_link.OUTPUT}, {"id": 5, "monitor": "eDP-1"}, {"id": 6, "monitor": "eDP-1"}])
        self.assertTrue(shown)
        steps = d.call_args.args
        self.assertEqual(steps[1:5], tuple(ryoku_link.PhoneView._send_home("eDP-1", 6)))
        self.assertEqual(self.view.pulled, {5: ("eDP-1", 6)})


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

    def test_video_connection_without_an_open_view_is_closed_untouched(self):
        class Writer:
            closed = False

            def close(self):
                self.closed = True

        writer = Writer()
        with mock.patch.object(ryoku_link, "view", None):
            asyncio.run(ryoku_link.handle_video(None, writer))
        self.assertTrue(writer.closed)


if __name__ == "__main__":
    unittest.main()
