"""Ground-truth options for PDDL environments."""

from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple

import numpy as np
from bosdyn.client import math_helpers
from bosdyn.client.sdk import Robot
from gym.spaces import Box

from predicators import utils
from predicators.envs import get_or_create_env
from predicators.envs.spot_env import SpotRearrangementEnv, \
    _object_to_top_down_geom, get_detection_id_for_object, get_robot
from predicators.ground_truth_models import GroundTruthOptionFactory
from predicators.settings import CFG
from predicators.spot_utils.perception.object_detection import \
    get_grasp_pixel, get_last_detected_objects
from predicators.spot_utils.perception.perception_structs import \
    RGBDImageWithContext
from predicators.spot_utils.perception.spot_cameras import \
    get_last_captured_images
from predicators.spot_utils.skills.spot_grasp import grasp_at_pixel
from predicators.spot_utils.skills.spot_hand_move import close_gripper, \
    gaze_at_relative_pose, move_hand_to_relative_pose, open_gripper
from predicators.spot_utils.skills.spot_navigation import \
    navigate_to_absolute_pose, navigate_to_relative_pose
from predicators.spot_utils.skills.spot_place import place_at_relative_position
from predicators.spot_utils.skills.spot_stow_arm import stow_arm
from predicators.spot_utils.skills.spot_sweep import sweep
from predicators.spot_utils.spot_localization import SpotLocalizer
from predicators.spot_utils.utils import DEFAULT_HAND_LOOK_DOWN_POSE, \
    DEFAULT_HAND_LOOK_STRAIGHT_DOWN_POSE, \
    DEFAULT_HAND_LOOK_STRAIGHT_DOWN_POSE_HIGH, get_relative_se2_from_se3
from predicators.structs import Action, Array, Object, ParameterizedOption, \
    Predicate, State, Type

###############################################################################
#            Helper functions for chaining multiple spot skills               #
###############################################################################


def navigate_to_relative_pose_and_gaze(robot: Robot,
                                       rel_pose: math_helpers.SE2Pose,
                                       localizer: SpotLocalizer,
                                       gaze_target: math_helpers.Vec3) -> None:
    """Navigate to a pose and then gaze at a specific target afterwards."""
    # Stow first.
    stow_arm(robot)
    # First navigate to the pose.
    navigate_to_relative_pose(robot, rel_pose)
    # Get the relative gaze target based on the new robot pose.
    localizer.localize()
    robot_pose = localizer.get_last_robot_pose()
    # Transform this to the body frame.
    rel_gaze_target_body = robot_pose.inverse().transform_vec3(gaze_target)
    # Then gaze.
    gaze_at_relative_pose(robot, rel_gaze_target_body)


def _grasp_at_pixel_and_stow(robot: Robot, img: RGBDImageWithContext,
                             pixel: Tuple[int, int],
                             grasp_rot: Optional[math_helpers.Quat]) -> None:
    # Grasp.
    grasp_at_pixel(robot, img, pixel, grasp_rot=grasp_rot)
    # Stow.
    stow_arm(robot)


def _place_at_relative_position_and_stow(
        robot: Robot, rel_pose: math_helpers.SE3Pose) -> None:
    # Place.
    place_at_relative_position(robot, rel_pose)
    # Now, move the arm back slightly. We do this because if we're
    # placing an object directly onto a table instead of dropping it,
    # then stowing/moving the hand immediately after might cause
    # us to knock the object off the table.
    slightly_back_and_up_pose = math_helpers.SE3Pose(
        x=rel_pose.x - 0.15,
        y=rel_pose.y,
        z=rel_pose.z + 0.1,
        rot=math_helpers.Quat.from_pitch(np.pi / 3))
    move_hand_to_relative_pose(robot, slightly_back_and_up_pose)
    # Stow.
    stow_arm(robot)


def _drop_and_stow(robot: Robot) -> None:
    # First, move the arm to a position from which the object will drop.
    move_hand_to_relative_pose(robot, DEFAULT_HAND_LOOK_DOWN_POSE)
    # Open the hand.
    open_gripper(robot)
    # Stow.
    stow_arm(robot)


def _drop_at_relative_position_and_look(
        robot: Robot, rel_pose: math_helpers.SE3Pose) -> None:
    # Place.
    place_at_relative_position(robot, rel_pose)
    # Close the gripper.
    close_gripper(robot)
    # Look straight down.
    move_hand_to_relative_pose(robot, DEFAULT_HAND_LOOK_STRAIGHT_DOWN_POSE)


def _move_closer_and_drop_at_relative_position_and_look(
        robot: Robot, rel_pose: math_helpers.SE3Pose) -> None:
    # First, check if we're too far away in distance or angle
    # to place.
    dist_to_object = np.sqrt(rel_pose.x * rel_pose.x + rel_pose.y * rel_pose.y)
    if dist_to_object > 0.75:
        pose_to_nav_to = math_helpers.SE2Pose(
            rel_pose.x - (0.75 * rel_pose.x / dist_to_object),
            rel_pose.y - (0.75 * rel_pose.y / dist_to_object), 0.0)
        navigate_to_relative_pose(robot, pose_to_nav_to)
    # Place with a new rel_pose!
    rel_pose.x = rel_pose.x - pose_to_nav_to.x
    rel_pose.y = rel_pose.y - pose_to_nav_to.y
    place_at_relative_position(robot, rel_pose)
    # Close the gripper.
    close_gripper(robot)
    # Look straight down.
    move_hand_to_relative_pose(robot,
                               DEFAULT_HAND_LOOK_STRAIGHT_DOWN_POSE_HIGH)


def _drag_and_release(robot: Robot, rel_pose: math_helpers.SE2Pose) -> None:
    # First navigate to the pose.
    navigate_to_relative_pose(robot, rel_pose)
    # Open the gripper.
    open_gripper(robot)
    # Move the gripper up a little bit.
    move_hand_to_relative_pose(robot, DEFAULT_HAND_LOOK_STRAIGHT_DOWN_POSE)
    # Stow the arm.
    stow_arm(robot)
    # Move backward to avoid hitting the chair subsequently.
    navigate_to_relative_pose(robot, math_helpers.SE2Pose(0.0, -0.5, 0.0))


def _move_to_absolute_pose_and_place_stow(
        robot: Robot, localizer: SpotLocalizer,
        absolute_pose: math_helpers.SE2Pose) -> None:
    # Move to the absolute pose.
    navigate_to_absolute_pose(robot, localizer, absolute_pose)
    # Place in front.
    place_rel_pose = math_helpers.SE3Pose(x=0.80,
                                          y=0.0,
                                          z=0.0,
                                          rot=math_helpers.Quat.from_pitch(
                                              np.pi / 2))
    _place_at_relative_position_and_stow(robot, place_rel_pose)


###############################################################################
#                    Helper parameterized option policies                     #
###############################################################################


def _move_to_target_policy(name: str, distance_param_idx: int,
                           yaw_param_idx: int, robot_obj_idx: int,
                           target_obj_idx: int, do_gaze: bool, state: State,
                           memory: Dict, objects: Sequence[Object],
                           params: Array) -> Action:

    del memory  # not used

    robot, localizer, _ = get_robot()

    distance = params[distance_param_idx]
    yaw = params[yaw_param_idx]

    robot_obj = objects[robot_obj_idx]
    robot_pose = utils.get_se3_pose_from_state(state, robot_obj)

    target_obj = objects[target_obj_idx]
    target_pose = utils.get_se3_pose_from_state(state, target_obj)

    rel_pose = get_relative_se2_from_se3(robot_pose, target_pose, distance,
                                         yaw)
    target_height = state.get(target_obj, "height")
    gaze_target = math_helpers.Vec3(target_pose.x, target_pose.y,
                                    target_pose.z + target_height / 2)

    if not do_gaze:
        fn: Callable = navigate_to_relative_pose
        fn_args: Tuple = (robot, rel_pose)
    else:
        fn = navigate_to_relative_pose_and_gaze
        fn_args = (robot, rel_pose, localizer, gaze_target)

    return utils.create_spot_env_action(name, objects, fn, fn_args)


def _grasp_policy(name: str, target_obj_idx: int, state: State, memory: Dict,
                  objects: Sequence[Object], params: Array) -> Action:
    del memory, params  # not used

    robot, _, _ = get_robot()

    target_obj = objects[target_obj_idx]

    # Special case: if we're running dry, the image won't be used.
    if CFG.spot_run_dry:
        pixel = (0, 0)
        img: Optional[RGBDImageWithContext] = None
    else:
        target_detection_id = get_detection_id_for_object(target_obj)
        rgbds = get_last_captured_images()
        _, artifacts = get_last_detected_objects()
        hand_camera = "hand_color_image"
        img = rgbds[hand_camera]
        pixel = get_grasp_pixel(rgbds, artifacts, target_detection_id,
                                hand_camera)

    # Grasp from the top-down.
    grasp_rot = math_helpers.Quat.from_pitch(np.pi / 2)
    # If the target object is reasonably large, don't try to stow!
    target_obj_volume = state.get(target_obj, "height") * \
        state.get(target_obj, "length") * state.get(target_obj, "width")
    if target_obj_volume > CFG.spot_grasp_stow_volume_threshold:
        fn: Callable = grasp_at_pixel
    else:
        fn = _grasp_at_pixel_and_stow

    return utils.create_spot_env_action(name, objects, fn,
                                        (robot, img, pixel, grasp_rot))


###############################################################################
#                   Concrete parameterized option policies                    #
###############################################################################


def _move_to_hand_view_object_policy(state: State, memory: Dict,
                                     objects: Sequence[Object],
                                     params: Array) -> Action:
    name = "MoveToHandViewObject"
    distance_param_idx = 0
    yaw_param_idx = 1
    robot_obj_idx = 0
    target_obj_idx = 1
    do_gaze = True
    return _move_to_target_policy(name, distance_param_idx, yaw_param_idx,
                                  robot_obj_idx, target_obj_idx, do_gaze,
                                  state, memory, objects, params)


def _move_to_body_view_object_policy(state: State, memory: Dict,
                                     objects: Sequence[Object],
                                     params: Array) -> Action:
    name = "MoveToBodyViewObject"
    distance_param_idx = 0
    yaw_param_idx = 1
    robot_obj_idx = 0
    target_obj_idx = 1
    do_gaze = False
    return _move_to_target_policy(name, distance_param_idx, yaw_param_idx,
                                  robot_obj_idx, target_obj_idx, do_gaze,
                                  state, memory, objects, params)


def _move_to_reach_object_policy(state: State, memory: Dict,
                                 objects: Sequence[Object],
                                 params: Array) -> Action:
    name = "MoveToReachObject"
    distance_param_idx = 0
    yaw_param_idx = 1
    robot_obj_idx = 0
    target_obj_idx = 1
    do_gaze = False
    return _move_to_target_policy(name, distance_param_idx, yaw_param_idx,
                                  robot_obj_idx, target_obj_idx, do_gaze,
                                  state, memory, objects, params)


def _pick_object_from_top_policy(state: State, memory: Dict,
                                 objects: Sequence[Object],
                                 params: Array) -> Action:
    name = "PickObjectFromTop"
    target_obj_idx = 1
    return _grasp_policy(name, target_obj_idx, state, memory, objects, params)


def _place_object_on_top_policy(state: State, memory: Dict,
                                objects: Sequence[Object],
                                params: Array) -> Action:
    del memory  # not used

    name = "PlaceObjectOnTop"
    robot_obj_idx = 0
    surface_obj_idx = 2

    robot, _, _ = get_robot()

    dx, dy, dz = params

    robot_obj = objects[robot_obj_idx]
    robot_pose = utils.get_se3_pose_from_state(state, robot_obj)

    surface_obj = objects[surface_obj_idx]
    surface_pose = utils.get_se3_pose_from_state(state, surface_obj)

    # Special case: the robot is already on top of the surface (because it is
    # probably the floor). When this happens, just drop the object.
    surface_geom = _object_to_top_down_geom(surface_obj, state)
    if surface_geom.contains_point(robot_pose.x, robot_pose.y):
        return utils.create_spot_env_action(name, objects, _drop_and_stow,
                                            (robot, ))

    # The dz parameter is with respect to the top of the container.
    surface_half_height = state.get(surface_obj, "height") / 2
    surface_rel_pose = robot_pose.inverse() * surface_pose
    place_rel_pos = math_helpers.Vec3(x=surface_rel_pose.x + dx,
                                      y=surface_rel_pose.y + dy,
                                      z=surface_rel_pose.z + dz +
                                      surface_half_height)

    return utils.create_spot_env_action(name, objects,
                                        _place_at_relative_position_and_stow,
                                        (robot, place_rel_pos))


def _drop_object_inside_policy(state: State, memory: Dict,
                               objects: Sequence[Object],
                               params: Array) -> Action:
    del memory  # not used

    name = "DropObjectInside"
    robot_obj_idx = 0
    container_obj_idx = 2

    robot, _, _ = get_robot()

    dx, dy, dz = params

    robot_obj = objects[robot_obj_idx]
    robot_pose = utils.get_se3_pose_from_state(state, robot_obj)

    container_obj = objects[container_obj_idx]
    container_pose = utils.get_se3_pose_from_state(state, container_obj)
    # The dz parameter is with respect to the top of the container.
    container_half_height = state.get(container_obj, "height") / 2

    container_rel_pose = robot_pose.inverse() * container_pose
    place_z = container_rel_pose.z + container_half_height + dz
    place_rel_pos = math_helpers.Vec3(x=container_rel_pose.x + dx,
                                      y=container_rel_pose.y + dy,
                                      z=place_z)

    return utils.create_spot_env_action(name, objects,
                                        _drop_at_relative_position_and_look,
                                        (robot, place_rel_pos))


def _move_and_drop_object_inside_policy(state: State, memory: Dict,
                                        objects: Sequence[Object],
                                        params: Array) -> Action:
    del memory  # not used

    name = "MoveAndDropObjectInside"
    robot_obj_idx = 0
    container_obj_idx = 2

    robot, _, _ = get_robot()

    dx, dy, dz = params

    robot_obj = objects[robot_obj_idx]
    robot_pose = utils.get_se3_pose_from_state(state, robot_obj)

    container_obj = objects[container_obj_idx]
    container_pose = utils.get_se3_pose_from_state(state, container_obj)
    # The dz parameter is with respect to the top of the container.
    container_half_height = state.get(container_obj, "height") / 2

    container_rel_pose = robot_pose.inverse() * container_pose
    place_z = container_rel_pose.z + container_half_height + dz
    place_rel_pos = math_helpers.Vec3(x=container_rel_pose.x + dx,
                                      y=container_rel_pose.y + dy,
                                      z=place_z)

    return utils.create_spot_env_action(
        name, objects, _move_closer_and_drop_at_relative_position_and_look,
        (robot, place_rel_pos))


def _drag_to_unblock_object_policy(state: State, memory: Dict,
                                   objects: Sequence[Object],
                                   params: Array) -> Action:
    del state, memory  # not used

    name = "DragToUnblockObject"
    robot, _, _ = get_robot()
    dx, dy, dyaw = params
    move_rel_pos = math_helpers.SE2Pose(dx, dy, angle=dyaw)

    return utils.create_spot_env_action(name, objects, _drag_and_release,
                                        (robot, move_rel_pos))


def _sweep_into_container_policy(state: State, memory: Dict,
                                 objects: Sequence[Object],
                                 params: Array) -> Action:
    del memory  # not used

    name = "SweepIntoContainer"
    robot_obj_idx = 0
    target_obj_idx = 2

    robot, _, _ = get_robot()

    start_dx, start_dy = params

    robot_obj = objects[robot_obj_idx]
    robot_pose = utils.get_se3_pose_from_state(state, robot_obj)

    target_obj = objects[target_obj_idx]
    target_pose = utils.get_se3_pose_from_state(state, target_obj)
    start_dz = 0.0
    start_x = target_pose.x - robot_pose.x + start_dx
    start_y = target_pose.y - robot_pose.y + start_dy
    start_z = target_pose.z - robot_pose.z + start_dz
    sweep_start_pose = math_helpers.SE3Pose(x=start_x,
                                            y=start_y,
                                            z=start_z,
                                            rot=math_helpers.Quat.from_yaw(
                                                -np.pi / 2))
    # Calculate the yaw and distance for the sweep.
    sweep_move_dx = start_dx
    sweep_move_dy = -(2 * start_dy)

    # Execute the sweep.
    return utils.create_spot_env_action(
        name, objects, sweep,
        (robot, sweep_start_pose, sweep_move_dx, sweep_move_dy))


def _prepare_container_for_sweeping_policy(state: State, memory: Dict,
                                           objects: Sequence[Object],
                                           params: Array) -> Action:
    del memory  # not used

    name = "PrepareContainerForSweeping"
    target_obj_idx = 2

    robot, localizer, _ = get_robot()

    dx, dy, dyaw = params

    target_obj = objects[target_obj_idx]
    target_pose = utils.get_se3_pose_from_state(state, target_obj)
    absolute_move_pose = math_helpers.SE2Pose(target_pose.x + dx,
                                              target_pose.y + dy, dyaw)

    return utils.create_spot_env_action(name, objects,
                                        _move_to_absolute_pose_and_place_stow,
                                        (robot, localizer, absolute_move_pose))


###############################################################################
#                       Parameterized option factory                          #
###############################################################################

_OPERATOR_NAME_TO_PARAM_SPACE = {
    "MoveToReachObject": Box(-np.inf, np.inf, (2, )),  # rel dist, dyaw
    "MoveToHandViewObject": Box(-np.inf, np.inf, (2, )),  # rel dist, dyaw
    "MoveToBodyViewObject": Box(-np.inf, np.inf, (2, )),  # rel dist, dyaw
    "PickObjectFromTop": Box(0, 1, (0, )),
    "PlaceObjectOnTop": Box(-np.inf, np.inf, (3, )),  # rel dx, dy, dz
    "DropObjectInside": Box(-np.inf, np.inf, (3, )),  # rel dx, dy, dz
    "DropObjectInsideContainerOnTop": Box(-np.inf, np.inf,
                                          (3, )),  # rel dx, dy, dz
    "DragToUnblockObject": Box(-np.inf, np.inf, (3, )),  # rel dx, dy, dyaw
    "SweepIntoContainer": Box(-np.inf, np.inf, (2, )),  # rel dx, dy
    "PrepareContainerForSweeping": Box(-np.inf, np.inf, (3, )),  # dx, dy, dyaw
}

_OPERATOR_NAME_TO_POLICY = {
    "MoveToReachObject": _move_to_reach_object_policy,
    "MoveToHandViewObject": _move_to_hand_view_object_policy,
    "MoveToBodyViewObject": _move_to_body_view_object_policy,
    "PickObjectFromTop": _pick_object_from_top_policy,
    "PlaceObjectOnTop": _place_object_on_top_policy,
    "DropObjectInside": _drop_object_inside_policy,
    "DropObjectInsideContainerOnTop": _move_and_drop_object_inside_policy,
    "DragToUnblockObject": _drag_to_unblock_object_policy,
    "SweepIntoContainer": _sweep_into_container_policy,
    "PrepareContainerForSweeping": _prepare_container_for_sweeping_policy,
}


class _SpotParameterizedOption(utils.SingletonParameterizedOption):
    """A parameterized option for spot.

    NOTE: parameterized options MUST be singletons in order to avoid nasty
    issues with the expected atoms monitoring.

    Also note that we need to define the policies outside the class, rather
    than pass the policies into the class, to avoid pickling issues via bosdyn.
    """

    def __init__(self, operator_name: str, types: List[Type]) -> None:
        params_space = _OPERATOR_NAME_TO_PARAM_SPACE[operator_name]
        policy = _OPERATOR_NAME_TO_POLICY[operator_name]
        super().__init__(operator_name, policy, types, params_space)

    def __getnewargs__(self) -> Tuple:
        """Avoid pickling issues with bosdyn functions."""
        return (self.name, self.types)

    def __getstate__(self) -> Dict:
        """Avoid pickling issues with bosdyn functions."""
        return {"name": self.name}


class SpotCubeEnvGroundTruthOptionFactory(GroundTruthOptionFactory):
    """Ground-truth options for Spot environments."""

    @classmethod
    def get_env_names(cls) -> Set[str]:
        return {
            "spot_cube_env",
            "spot_soda_table_env",
            "spot_soda_bucket_env",
            "spot_soda_chair_env",
            "spot_soda_sweep_env",
            "spot_ball_and_cup_sticky_table_env",
            "spot_brush_shelf_env",
        }

    @classmethod
    def get_options(cls, env_name: str, types: Dict[str, Type],
                    predicates: Dict[str, Predicate],
                    action_space: Box) -> Set[ParameterizedOption]:
        # Note that these are 1:1 with the operators.
        env = get_or_create_env(env_name)
        assert isinstance(env, SpotRearrangementEnv)

        options: Set[ParameterizedOption] = set()
        for operator in env.strips_operators:
            operator_types = [p.type for p in operator.parameters]
            option = _SpotParameterizedOption(operator.name, operator_types)
            options.add(option)

        return options
