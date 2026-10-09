"""Camera wires (Camera In -> virtual cameras).

A *camera* feed is its own port kind: data-plane like audio, but paired
only with another camera port, and - because PipeWire has no null-video-
sink to dummy around - wired by the consuming node's own stream rather
than by daemon-made port pairs (the stream's target-object binding to the
camera is the visible link in the PipeWire graph).  Two sinks exist: a
PipeWire-native Virtual Cam (a pw-loopback republishing the feed as a
Video/Source) and a V4L2 Virtual Cam (a GStreamer pipeline writing a
v4l2loopback device).  These tests drive the daemon command layer
directly, with the process classes faked out so no real PipeWire or
GStreamer is touched.
"""

import pytest

from central import pwnodes
from central.main import PatchSpaceDaemon
from gui import node_specs
from tests.test_pwnodes import FakeGraph


class FakeProc:
    instances = []
    commands = []

    def __init__(self, name, command=("pw-loopback",), settle=0.0, **kw):
        self.name = name
        self.node_id = None
        self.alive = False
        self.owns_process = False
        FakeProc.instances.append(self)

    def create(self, command, quiet=False):
        FakeProc.commands.append(list(command))
        self.alive = True
        self.owns_process = True
        return True

    @property
    def is_alive(self):
        return self.alive

    def stuck(self, grace_s):
        return False

    def destroy(self):
        self.alive = False


@pytest.fixture(autouse=True)
def _fake_processes(monkeypatch):
    FakeProc.instances.clear()
    FakeProc.commands.clear()
    monkeypatch.setattr(pwnodes, "OwnedPwProcess", FakeProc)


def _ok(d, **cmd):
    resp = d.handle_command(cmd)
    assert resp["status"] == "ok", resp
    return resp


def _camera_pair(sink_type="v4l2_virtual_camera",
                 camera_name="v4l2_input.test_cam",
                 sink_config=None):
    """A Camera In wired into one of the two virtual-camera sinks."""
    d = PatchSpaceDaemon()
    _ok(d, command="add_node", node_type="camera_input", node_id="cam",
        config={"device_name": camera_name})
    _ok(d, command="add_node", node_type=sink_type, node_id="vc",
        config=sink_config or {"output_device": "/dev/video99"})
    _ok(d, command="add_edge", from_node="cam", to_node="vc",
        from_port="out", to_port="in")
    return d


# ---------------------------------------------------------------------------
# The kind itself (three mirrors: daemon, specs, repair)
# ---------------------------------------------------------------------------


def test_camera_pairs_only_with_camera():
    d = PatchSpaceDaemon()
    for node_id, node_type in (("cam", "camera_input"),
                               ("vc", "virtual_camera"),
                               ("vol", "volume"),
                               ("split", "splitter")):
        _ok(d, command="add_node", node_type=node_type, node_id=node_id)
    # A camera output only accepts a camera input...
    resp = d.handle_command(
        {"command": "add_edge", "from_node": "cam", "to_node": "vol",
         "from_port": "out", "to_port": "in"}
    )
    assert resp["status"] == "error"
    # ...and a camera input takes no audio.
    resp = d.handle_command(
        {"command": "add_edge", "from_node": "split", "to_node": "vc",
         "from_port": "out", "to_port": "in"}
    )
    assert resp["status"] == "error"
    # The camera-to-camera edge itself is fine.
    _ok(d, command="add_edge", from_node="cam", to_node="vc",
        from_port="out", to_port="in")


def test_a_virtual_camera_takes_exactly_one_feed():
    d = _camera_pair(sink_type="virtual_camera", sink_config={})
    _ok(d, command="add_node", node_type="camera_input", node_id="cam2",
        config={"device_name": "v4l2_input.other_cam"})
    resp = d.handle_command(
        {"command": "add_edge", "from_node": "cam2", "to_node": "vc",
         "from_port": "out", "to_port": "in"}
    )
    assert resp["status"] == "error"
    assert "already driven" in resp.get("message", "")


def test_gui_mirrors_the_camera_port_kind():
    assert node_specs.port_kind("camera_input", "out", "out") == "camera"
    assert node_specs.port_kind("virtual_camera", "in", "in") == "camera"
    assert node_specs.port_kind("v4l2_virtual_camera", "in", "in") == "camera"
    assert node_specs.ports_compatible(
        "camera_input", "out", "virtual_camera", "in")
    assert node_specs.ports_compatible(
        "camera_input", "out", "v4l2_virtual_camera", "in")
    assert not node_specs.ports_compatible("camera_input", "out", "volume", "in")
    assert not node_specs.ports_compatible("splitter", "out", "virtual_camera", "in")
    # The labels Kyle named the two sinks.
    assert node_specs.spec_for("virtual_camera").label == "Virtual Cam"
    assert node_specs.spec_for("v4l2_virtual_camera").label == "V4L2 Virtual Cam"


# ---------------------------------------------------------------------------
# The PipeWire-native Virtual Cam (pw-loopback republisher)
# ---------------------------------------------------------------------------


def test_virtual_camera_republishes_the_feed_in_pipewire():
    d = _camera_pair(sink_type="virtual_camera",
                     sink_config={"device_label": "My Cam"})
    d.space.supervise()
    assert len(FakeProc.commands) == 1
    command = FakeProc.commands[0]
    assert command[0] == "gst-launch-1.0"
    # The capture side binds to the camera (that binding is the visible
    # link)...
    assert "target-object=v4l2_input.test_cam" in command
    # ...the publish side is camera-shaped: name + class/role on the
    # stream properties (bare GstStructure values - no spaces allowed
    # there), the user's label on client-name (which pipewiresink turns
    # into node.description, what app pickers show).
    assert "client-name=My Cam" in command
    props = next(a for a in command if a.startswith("stream-properties="))
    assert "node.name=patchspace_vc" in props
    assert "media.class=Video/Source" in props
    assert "media.role=Camera" in props
    assert d.space.nodes["vc"].structural_ok()
    # A camera edge never becomes daemon-made port pairs.
    assert d.space._edge_links == {}


def test_virtual_camera_needs_no_output_device():
    d = _camera_pair(sink_type="virtual_camera", sink_config={})
    d.space.supervise()
    # Unlike the v4l2 one it publishes straight into PipeWire, so a wired
    # camera is all it takes - no device to pick.
    assert len(FakeProc.commands) == 1
    assert d.space.nodes["vc"].structural_ok()


def test_virtual_camera_bridges_raw_v4l2_feeds():
    # A raw v4l2 feed has no PipeWire node to capture from - but the
    # pipeline reads it with v4l2src and publishes it into PipeWire all
    # the same, which is the whole bridge.
    d = PatchSpaceDaemon()
    _ok(d, command="add_node", node_type="camera_input", node_id="cam",
        config={"source_mode": "v4l2", "device_path": "/dev/video7"})
    _ok(d, command="add_node", node_type="virtual_camera", node_id="vc",
        config={"device_label": "My Cam"})
    _ok(d, command="add_edge", from_node="cam", to_node="vc",
        from_port="out", to_port="in")
    d.space.supervise()
    command = FakeProc.commands[0]
    assert "v4l2src" in command
    assert "device=/dev/video7" in command
    assert not any(a.startswith("target-object=") for a in command)
    assert "client-name=My Cam" in command


# ---------------------------------------------------------------------------
# The V4L2 Virtual Cam (GStreamer -> loopback device)
# ---------------------------------------------------------------------------


def test_v4l2_virtual_camera_spawns_one_pipeline_for_its_source():
    d = _camera_pair()
    d.space.supervise()
    assert len(FakeProc.commands) == 1
    command = FakeProc.commands[0]
    assert command[0] == "gst-launch-1.0"
    assert "target-object=v4l2_input.test_cam" in command
    assert "device=/dev/video99" in command
    # The orphan sweep recognises the pipeline by the node's backing
    # name riding in argv (the v4l2sink element name).
    assert any(a.startswith("name=patchspace_vc") for a in command)
    assert d.space.nodes["vc"].structural_ok()
    assert d.space._edge_links == {}


def test_no_pipeline_without_a_camera_or_an_output_device():
    d = PatchSpaceDaemon()
    _ok(d, command="add_node", node_type="v4l2_virtual_camera", node_id="vc")
    d.space.supervise()
    assert FakeProc.commands == []
    assert d.space.nodes["vc"].structural_ok()  # idle is healthy

    _ok(d, command="add_node", node_type="camera_input", node_id="cam",
        config={"device_name": "v4l2_input.test_cam"})
    _ok(d, command="add_edge", from_node="cam", to_node="vc",
        from_port="out", to_port="in")
    d.space.supervise()
    # Camera wired but still no output device: still nothing runs.
    assert FakeProc.commands == []


def test_rewiring_replaces_the_interior():
    d = _camera_pair(camera_name="v4l2_input.first_cam")
    d.space.supervise()
    assert "target-object=v4l2_input.first_cam" in FakeProc.commands[0]
    first = d.space.nodes["vc"].backings[0]

    _ok(d, command="add_node", node_type="camera_input", node_id="cam2",
        config={"device_name": "v4l2_input.second_cam"})
    _ok(d, command="remove_edge",
        edge_id=pwnodes.PatchSpace._edge_id("cam", "vc", "in", "out"))
    _ok(d, command="add_edge", from_node="cam2", to_node="vc",
        from_port="out", to_port="in")
    d.space.supervise()

    assert "target-object=v4l2_input.second_cam" in FakeProc.commands[-1]
    assert first.is_alive is False  # the old interior was swapped out
    assert d.space.nodes["vc"].structural_ok()


def test_v4l2_mode_reads_the_raw_device():
    d = PatchSpaceDaemon()
    _ok(d, command="add_node", node_type="camera_input", node_id="cam",
        config={"source_mode": "v4l2", "device_path": "/dev/video7"})
    _ok(d, command="add_node", node_type="v4l2_virtual_camera", node_id="vc",
        config={"output_device": "/dev/video99"})
    _ok(d, command="add_edge", from_node="cam", to_node="vc",
        from_port="out", to_port="in")
    d.space.supervise()
    command = FakeProc.commands[0]
    assert "v4l2src" in command
    assert "device=/dev/video7" in command
    assert not any(a.startswith("target-object=") for a in command)


def test_setting_the_output_device_restarts_the_pipeline():
    d = _camera_pair(sink_config={"output_device": "/dev/video98"})
    d.space.supervise()
    assert "device=/dev/video98" in FakeProc.commands[0]
    _ok(d, command="set_node_property", node_id="vc",
        property="output_device", value="/dev/video97")
    d.space.supervise()
    assert "device=/dev/video97" in FakeProc.commands[-1]
    assert d.space.nodes["vc"].structural_ok()


# ---------------------------------------------------------------------------
# Pickers
# ---------------------------------------------------------------------------


def _graph_with_video_bits():
    g = FakeGraph()
    g._nodes[10] = {"info": {"props": {
        "node.name": "v4l2_input.test_cam",
        "node.description": "Test Cam",
        "media.class": "Video/Source",
    }}}
    g._nodes[11] = {"info": {"props": {
        "node.name": "v4l2_device.cap_out",
        "device.api": "v4l2",
        "api.v4l2.path": "/dev/video3",
        "api.v4l2.cap.card": "Capture Card",
        "api.v4l2.cap.capabilities": "0x84a00001",
    }}}
    g._nodes[12] = {"info": {"props": {
        "node.name": "v4l2_device.loop",
        "device.api": "v4l2",
        "api.v4l2.path": "/dev/video10",
        "api.v4l2.cap.card": "VirtualCam0",
        "api.v4l2.cap.capabilities": "0x04000002",
    }}}
    return g


def test_get_cameras_lists_pw_sources_and_raw_capture_devices():
    d = PatchSpaceDaemon()
    d.graph = _graph_with_video_bits()
    d.space.graph = d.graph
    resp = _ok(d, command="get_cameras")
    assert resp["cameras"] == [
        {"name": "v4l2_input.test_cam", "description": "Test Cam"}
    ]
    # The raw v4l2 list offers only capture-capable devices: the loopback
    # (output-only caps) must not appear as an input.
    assert [c["path"] for c in resp["v4l2_inputs"]] == ["/dev/video3"]


def test_get_camera_outputs_lists_output_devices():
    d = PatchSpaceDaemon()
    d.graph = _graph_with_video_bits()
    d.space.graph = d.graph
    resp = _ok(d, command="get_camera_outputs")
    assert [(c["path"], c["description"]) for c in resp["camera_outputs"]] == [
        ("/dev/video10", "VirtualCam0")
    ]
