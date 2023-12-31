"""Interface for spot sweeping skill."""

import numpy as np
from bosdyn.client import math_helpers
from bosdyn.client.sdk import Robot

from predicators.spot_utils.skills.spot_hand_move import \
    move_hand_to_relative_pose
from predicators.spot_utils.skills.spot_stow_arm import stow_arm


def sweep(robot: Robot, sweep_start_pose: math_helpers.SE3Pose, move_dx: float,
          move_dy: float) -> None:
    """Sweep in the xy plane, starting at the start pose and then moving."""
    # First, move the hand to the start pose.
    move_hand_to_relative_pose(robot, sweep_start_pose)
    # Calculate the end pose.
    relative_hand_move = math_helpers.SE3Pose(x=move_dx,
                                              y=move_dy,
                                              z=0,
                                              rot=math_helpers.Quat())
    sweep_end_pose = relative_hand_move * sweep_start_pose
    # Move the hand to the end pose.
    move_hand_to_relative_pose(robot, sweep_end_pose)
    # Stow arm to finish.
    stow_arm(robot)


if __name__ == "__main__":
    # Run this file alone to test manually.
    # Make sure to pass in --spot_robot_ip.

    # NOTE: this test assumes that the robot is standing in front of a table
    # that has a soda can on it. The test starts by running object detection to
    # get the pose of the soda can. Then the robot opens its gripper and pauses
    # until a brush is put in the gripper, with the bristles facing down and
    # forward. The robot should then brush the soda can to the right.

    # pylint: disable=ungrouped-imports
    from bosdyn.client import create_standard_sdk
    from bosdyn.client.lease import LeaseClient, LeaseKeepAlive
    from bosdyn.client.util import authenticate

    from predicators import utils
    from predicators.settings import CFG
    from predicators.spot_utils.perception.perception_structs import \
        LanguageObjectDetectionID
    from predicators.spot_utils.skills.spot_find_objects import \
        init_search_for_objects
    from predicators.spot_utils.skills.spot_hand_move import close_gripper, \
        open_gripper
    from predicators.spot_utils.skills.spot_navigation import go_home, \
        navigate_to_relative_pose
    from predicators.spot_utils.spot_localization import SpotLocalizer
    from predicators.spot_utils.utils import get_graph_nav_dir, \
        get_relative_se2_from_se3, get_spot_home_pose, verify_estop

    def _run_manual_test() -> None:
        # Put inside a function to avoid variable scoping issues.
        args = utils.parse_args(env_required=False,
                                seed_required=False,
                                approach_required=False)
        utils.update_config(args)

        # Get constants.
        hostname = CFG.spot_robot_ip
        path = get_graph_nav_dir()

        sdk = create_standard_sdk('SweepSkillTestClient')
        robot = sdk.create_robot(hostname)
        authenticate(robot)
        verify_estop(robot)
        lease_client = robot.ensure_client(LeaseClient.default_service_name)
        lease_client.take()
        lease_keepalive = LeaseKeepAlive(lease_client,
                                         must_acquire=True,
                                         return_at_exit=True)
        robot.time_sync.wait_for_sync()
        localizer = SpotLocalizer(robot, path, lease_client, lease_keepalive)
        localizer.localize()

        # Go home.
        go_home(robot, localizer)
        localizer.localize()

        # Find the soda can.
        soda_detection_id = LanguageObjectDetectionID("soda can")
        detections, _ = init_search_for_objects(robot, localizer,
                                                {soda_detection_id})
        soda_pose = detections[soda_detection_id]

        # Move the hand to the side so that the brush can face forward.
        hand_side_pose = math_helpers.SE3Pose(x=0.80,
                                              y=0.0,
                                              z=0.25,
                                              rot=math_helpers.Quat.from_yaw(
                                                  -np.pi / 2))
        move_hand_to_relative_pose(robot, hand_side_pose)

        # Ask for the brush.
        open_gripper(robot)
        input("Put the brush in the robot's gripper, then press enter")
        close_gripper(robot)

        # Move to in front of the soda can.
        stow_arm(robot)
        pre_sweep_nav_distance = 1.0
        home_pose = get_spot_home_pose()
        pre_sweep_nav_angle = home_pose.angle - np.pi
        localizer.localize()
        robot_pose = localizer.get_last_robot_pose()
        rel_pose = get_relative_se2_from_se3(robot_pose, soda_pose,
                                             pre_sweep_nav_distance,
                                             pre_sweep_nav_angle)
        navigate_to_relative_pose(robot, rel_pose)
        localizer.localize()

        # Calculate sweep parameters.
        robot_pose = localizer.get_last_robot_pose()
        start_dx = 0.0
        start_dy = 0.25
        start_dz = 0.0
        start_x = soda_pose.x - robot_pose.x + start_dx
        start_y = soda_pose.y - robot_pose.y + start_dy
        start_z = soda_pose.z - robot_pose.z + start_dz
        sweep_start_pose = math_helpers.SE3Pose(x=start_x,
                                                y=start_y,
                                                z=start_z,
                                                rot=math_helpers.Quat.from_yaw(
                                                    -np.pi / 2))
        # Calculate the yaw and distance for the sweep.
        sweep_move_dx = 0.0
        sweep_move_dy = -0.5

        # Execute the sweep.
        sweep(robot, sweep_start_pose, sweep_move_dx, sweep_move_dy)

        # Stow to finish.
        stow_arm(robot)

    _run_manual_test()
