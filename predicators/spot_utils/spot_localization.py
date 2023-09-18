"""Interface for localizing spot in a previously mapped environment.

The main point of this code is to provide a consistent world frame within and
between runs. That way, absolute positions of objects that are saved in task
files remain accurate, even when starting the robot from a different location.

Before using this interface:

1. Print out and tape april tags around the environment. The tags are here:
   https://support.bostondynamics.com/s/article/About-Fiducials
2. Run the interactive script from the spot SDK to create a map, while walking
   the spot around the environment. The script is here:
   https://github.com/boston-dynamics/spot-sdk/blob/master/python/examples/
   graph_nav_command_line/recording_command_line.py
3. Save the map files to spot_utils / graph_nav_maps / <your new env name>
4. Set --spot_graph_nav_map to your new env name.
"""

import logging
import time
from pathlib import Path
from typing import Dict

from bosdyn.api.graph_nav import map_pb2, nav_pb2
from bosdyn.client import ResponseError, TimedOutError, math_helpers
from bosdyn.client.frame_helpers import get_odom_tform_body
from bosdyn.client.graph_nav import GraphNavClient
from bosdyn.client.lease import LeaseClient, LeaseKeepAlive
from bosdyn.client.sdk import Robot

from predicators.spot_utils.utils import get_robot_state

_MAP_INITIALIZED = False


class LocalizationFailure(Exception):
    """Raised when localization fails."""


class SpotLocalizer:
    """Localizes spot in a previously mapped environment."""

    def __init__(self, robot: Robot, upload_path: Path,
                 lease_client: LeaseClient,
                 lease_keepalive: LeaseKeepAlive) -> None:
        self._robot = robot
        self._upload_path = upload_path
        self._lease_client = lease_client
        self._lease_keepalive = lease_keepalive

        # Force trigger timesync.
        self._robot.time_sync.wait_for_sync()

        # Create the client for the Graph Nav main service.
        self.graph_nav_client = self._robot.ensure_client(
            GraphNavClient.default_service_name)

        # Upload graph and snapshots on start.
        self._upload_graph_and_snapshots()

        # Initialize robot pose, which will be updated in localize().
        self._robot_pose = math_helpers.SE3Pose(0, 0, 0, math_helpers.Quat())

        # Run localize once to start.
        self.localize()

    def _upload_graph_and_snapshots(self) -> None:
        """Upload the graph and snapshots to the robot."""
        # pylint: disable=no-member
        logging.info("Loading the graph from disk into local storage...")
        # Load the graph from disk.
        with open(self._upload_path / "graph", "rb") as f:
            data = f.read()
            current_graph = map_pb2.Graph()
            current_graph.ParseFromString(data)
            logging.info(f"Loaded graph has {len(current_graph.waypoints)} "
                         f"waypoints and {len(current_graph.edges)} edges")
        # Load the waypoint snapshots from disk.
        waypoint_path = self._upload_path / "waypoint_snapshots"
        waypoint_snapshots: Dict[str, map_pb2.WaypointSnapshot] = {}
        for waypoint in current_graph.waypoints:
            with open(waypoint_path / waypoint.snapshot_id, "rb") as f:
                waypoint_snapshot = map_pb2.WaypointSnapshot()
                waypoint_snapshot.ParseFromString(f.read())
                waypoint_snapshots[waypoint_snapshot.id] = waypoint_snapshot
        # Load the edge snapshots from disk.
        edge_path = self._upload_path / "edge_snapshots"
        edge_snapshots: Dict[str, map_pb2.EdgeSnapshot] = {}
        for edge in current_graph.edges:
            if len(edge.snapshot_id) == 0:
                continue
            with open(edge_path / edge.snapshot_id, "rb") as f:
                edge_snapshot = map_pb2.EdgeSnapshot()
                edge_snapshot.ParseFromString(f.read())
                edge_snapshots[edge_snapshot.id] = edge_snapshot
        # Upload the graph to the robot.
        logging.info("Uploading the graph and snapshots to the robot...")
        true_if_empty = not len(current_graph.anchoring.anchors)
        response = self.graph_nav_client.upload_graph(
            graph=current_graph, generate_new_anchoring=true_if_empty)
        # Upload the snapshots to the robot.
        for snapshot_id in response.unknown_waypoint_snapshot_ids:
            waypoint_snapshot = waypoint_snapshots[snapshot_id]
            self.graph_nav_client.upload_waypoint_snapshot(waypoint_snapshot)
        for snapshot_id in response.unknown_edge_snapshot_ids:
            edge_snapshot = edge_snapshots[snapshot_id]
            self.graph_nav_client.upload_edge_snapshot(edge_snapshot)

    def get_last_robot_pose(self) -> math_helpers.SE3Pose:
        """Get the last estimated robot pose.

        Does not localize.
        """
        return self._robot_pose

    def localize(self,
                 num_retries: int = 10,
                 retry_wait_time: float = 1.0) -> None:
        """Re-localize the robot and return the current SE3Pose of the body.

        It's good practice to call this periodically to avoid drift
        issues. April tags need to be in view.
        """
        global _MAP_INITIALIZED  # pylint: disable=global-statement
        robot_state = get_robot_state(self._robot)
        current_odom_tform_body = get_odom_tform_body(
            robot_state.kinematic_state.transforms_snapshot).to_proto()
        localization = nav_pb2.Localization()
        try:
            if not _MAP_INITIALIZED:
                self.graph_nav_client.set_localization(
                    initial_guess_localization=localization,
                    ko_tform_body=current_odom_tform_body)
            localization_state = self.graph_nav_client.get_localization_state()
            transform = localization_state.localization.seed_tform_body
            if str(transform) == "":
                raise LocalizationFailure("Received empty localization state.")
        except (ResponseError, TimedOutError, LocalizationFailure) as e:
            # Retry or fail.
            if num_retries <= 0:
                msg = f"Localization failed permanently: {e}."
                logging.warning(msg)
                raise LocalizationFailure(msg)
            logging.warning("Localization failed once, retrying.")
            time.sleep(retry_wait_time)
            return self.localize(num_retries=num_retries - 1,
                                 retry_wait_time=retry_wait_time)
        logging.info("Localization succeeded.")
        _MAP_INITIALIZED = True
        self._robot_pose = math_helpers.SE3Pose.from_proto(transform)
        return None


if __name__ == "__main__":
    # Run this file alone to test manually.
    # Make sure to pass in --spot_robot_ip.

    # pylint: disable=ungrouped-imports
    from bosdyn.client import create_standard_sdk
    from bosdyn.client.util import authenticate

    from predicators import utils
    from predicators.settings import CFG
    from predicators.spot_utils.utils import verify_estop

    def _run_manual_test() -> None:
        # Put inside a function to avoid variable scoping issues.
        args = utils.parse_args(env_required=False,
                                seed_required=False,
                                approach_required=False)
        utils.update_config(args)

        # Get constants.
        hostname = CFG.spot_robot_ip
        upload_dir = Path(__file__).parent / "graph_nav_maps"
        path = upload_dir / CFG.spot_graph_nav_map

        sdk = create_standard_sdk('GraphNavTestClient')
        robot = sdk.create_robot(hostname)
        authenticate(robot)
        verify_estop(robot)
        lease_client = robot.ensure_client(LeaseClient.default_service_name)
        lease_client.take()
        lease_keepalive = LeaseKeepAlive(lease_client,
                                         must_acquire=True,
                                         return_at_exit=True)

        assert path.exists()
        localizer = SpotLocalizer(robot, path, lease_client, lease_keepalive)
        while True:
            input("Move the robot to a new location, then press enter.")
            lease_client.take()
            localizer.localize()
            robot_pose = localizer.get_last_robot_pose()
            print("Robot pose:", robot_pose)

    _run_manual_test()
