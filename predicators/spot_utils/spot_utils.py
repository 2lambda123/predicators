"""Utility functions to interface with the Boston Dynamics Spot robot."""

import functools
import logging
import os
import sys
import time
from typing import Any, Collection, Dict, Optional, Sequence, Set, Tuple, List

import apriltag
import bosdyn.client
import bosdyn.client.estop
import bosdyn.client.lease
import bosdyn.client.util
import cv2
import numpy as np
from bosdyn.api import basic_command_pb2, estop_pb2, geometry_pb2, image_pb2, \
    manipulation_api_pb2
from bosdyn.api.basic_command_pb2 import RobotCommandFeedbackStatus
from bosdyn.client import math_helpers
from bosdyn.client.estop import EstopClient
from bosdyn.client.frame_helpers import BODY_FRAME_NAME, \
    GRAV_ALIGNED_BODY_FRAME_NAME, ODOM_FRAME_NAME, VISION_FRAME_NAME, \
    get_a_tform_b, get_se2_a_tform_b, get_vision_tform_body
from bosdyn.client.image import ImageClient, build_image_request
from bosdyn.client.manipulation_api_client import ManipulationApiClient
from bosdyn.client.robot_command import RobotCommandBuilder, \
    RobotCommandClient, block_until_arm_arrives, blocking_stand
from bosdyn.client.robot_state import RobotStateClient
from bosdyn.client.sdk import Robot
from gym.spaces import Box

from predicators import utils
from predicators.settings import CFG
from predicators.spot_utils.helpers.graph_nav_command_line import \
    GraphNavInterface
from predicators.structs import Array, Image, Object

g_image_click = None
g_image_display = None


def get_memorized_waypoint(obj_name: str) -> Optional[Tuple[str, Array]]:
    """Returns None if the location of the object is unknown.

    Returns a waypoint ID (str) and a (x, y, yaw) offset otherwise.
    """
    graph_nav_loc_to_id = {
        "front_tool_room": "dented-marlin-HZHTzO56529oo0oGfAFHdg==",
        "tool_room_table": "lumpen-squid-p9fT8Ui8TYI7JWQJvfQwKw==",
        "toolbag": "seared-hare-0JBmyRiYHfbxn58ymEwPaQ==",
        "tool_room_tool_stand": "roving-gibbon-3eduef4VV0itZzkpHZueNQ==",
        "tool_room_platform": "comfy-auk-W0iygJ1WJyKR1eB3qe2mlg==",
        "low_wall_rack": "alight-coyote-Nvl0i02Mk7Ds8ax0sj0Hsw==",
        "high_wall_rack": "alight-coyote-Nvl0i02Mk7Ds8ax0sj0Hsw==",
        "extra_room_table": "alight-coyote-Nvl0i02Mk7Ds8ax0sj0Hsw==",
    }
    offsets = {"extra_room_table": np.array([0.0, -0.3, np.pi / 2])}
    if obj_name not in graph_nav_loc_to_id:
        return None
    waypoint_id = graph_nav_loc_to_id[obj_name]
    offset = offsets.get(obj_name, np.zeros(3, dtype=np.float32))
    return (waypoint_id, offset)


obj_name_to_apriltag_id = {
    "hammer": 401,
    "brush": 402,
    "hex_key": 403,
    "hex_screwdriver": 404,
    "toolbag": 405,
    "low_wall_rack": 406,
    "front_tool_room": 407,
    "tool_room_table": 408,
    "extra_room_table": 409,
    "cube": 410,
}

OBJECT_CROPS = {
    # min_x, max_x, min_y, max_y
    "hammer": (160, 450, 160, 350),
    "hex_key": (160, 450, 160, 350),
    "brush": (100, 400, 350, 480),
    "hex_screwdriver": (100, 400, 350, 480),
}

OBJECT_COLOR_BOUNDS = {
    # (min B, min G, min R), (max B, max G, max R)
    "hammer": ((0, 0, 50), (40, 40, 200)),
    "hex_key": ((0, 50, 50), (40, 150, 200)),
    "brush": ((0, 100, 200), (80, 255, 255)),
    "hex_screwdriver": ((0, 0, 50), (40, 40, 200)),
}

OBJECT_GRASP_OFFSET = {
    # dx, dy
    "hammer": (0, 0),
    "hex_key": (0, 50),
    "brush": (0, 0),
    "hex_screwdriver": (0, 0),
}

COMMAND_TIMEOUT = 20.0

CAMERA_NAMES = [
    "hand_color_image", "left_fisheye_image", "right_fisheye_image",
    "frontleft_fisheye_image", "frontright_fisheye_image", "back_fisheye_image"
]


def _find_object_center(img: Image,
                        obj_name: str) -> Optional[Tuple[int, int]]:
    # Copy to make sure we don't modify the image.
    img = img.copy()

    # Crop
    crop_min_x, crop_max_x, crop_min_y, crop_max_y = OBJECT_CROPS[obj_name]
    cropped_img = img[crop_min_y:crop_max_y, crop_min_x:crop_max_x]

    # Mask color.
    lo, hi = OBJECT_COLOR_BOUNDS[obj_name]
    lower = np.array(lo)
    upper = np.array(hi)
    mask = cv2.inRange(cropped_img, lower, upper)

    # Apply blur.
    mask = cv2.GaussianBlur(mask, (5, 5), 0)

    # Connected components with stats.
    nb_components, _, stats, centroids = cv2.connectedComponentsWithStats(
        mask, connectivity=4)

    # Fail if nothing found.
    if nb_components <= 1:
        return None

    # Find the largest non background component.
    # Note: range() starts from 1 since 0 is the background label.
    max_label, _ = max(
        ((i, stats[i, cv2.CC_STAT_AREA]) for i in range(1, nb_components)),
        key=lambda x: x[1])

    cropped_x, cropped_y = map(int, centroids[max_label])

    x = cropped_x + crop_min_x
    y = cropped_y + crop_min_y

    # Apply offset.
    dx, dy = OBJECT_GRASP_OFFSET[obj_name]
    x = np.clip(x + dx, 0, img.shape[1])
    y = np.clip(y + dy, 0, img.shape[0])

    return (x, y)


# pylint: disable=no-member
class _SpotInterface():
    """Implementation of interface with low-level controllers and sensor data
    grabbing for the Spot robot.

    Perception/Sensor Data:
    get_gripper_obs() -> Returns number corresponding to gripper open
                           percentage.

    Controllers:
    navigateToController(objs, [float:dx, float:dy, float:dyaw])
    graspController(objs, [(0:Any,1:Top,-1:Side)])
    placeOntopController(objs, [float:distance])
    """

    def __init__(self) -> None:
        self._hostname = CFG.spot_robot_ip
        self._verbose = False
        self._force_45_angle_grasp = False
        self._force_horizontal_grasp = False
        self._force_squeeze_grasp = False
        self._force_top_down_grasp = False
        self._image_source = "hand_color_image"

        self.hand_x, self.hand_y, self.hand_z = (0.80, 0, 0.45)
        self.hand_x_bounds = (0.3, 0.9)
        self.hand_y_bounds = (-0.5, 0.5)
        self.hand_z_bounds = (0.09, 0.7)
        self.localization_timeout = 10

        self._find_controller_move_queue_idx = 0

        # Try to connect to the robot. If this fails, still maintain the
        # instance for testing, but assert that it succeeded within the
        # controller calls.
        self._connected_to_spot = False
        try:
            self._connect_to_spot()
            self._connected_to_spot = True
        except (bosdyn.client.exceptions.ProxyConnectionError,
                bosdyn.client.exceptions.UnableToConnectToRobotError,
                RuntimeError):
            logging.warning("Could not connect to Spot!")

    def _connect_to_spot(self) -> None:
        # See hello_spot.py for an explanation of these lines.
        bosdyn.client.util.setup_logging(self._verbose)

        self.sdk = bosdyn.client.create_standard_sdk('SesameClient')
        self.robot: Robot = self.sdk.create_robot(self._hostname)
        if not os.environ.get('BOSDYN_CLIENT_USERNAME') or not os.environ.get(
                'BOSDYN_CLIENT_PASSWORD'):
            raise RuntimeError("Spot environment variables unset.")
        bosdyn.client.util.authenticate(self.robot)
        self.robot.time_sync.wait_for_sync()

        assert self.robot.has_arm(
        ), "Robot requires an arm to run this example."

        # Verify the robot is not estopped and that an external application has
        # registered and holds an estop endpoint.
        self.verify_estop(self.robot)

        self.lease_client = self.robot.ensure_client(
            bosdyn.client.lease.LeaseClient.default_service_name)
        self.robot_state_client: RobotStateClient = self.robot.ensure_client(
            RobotStateClient.default_service_name)
        self.robot_command_client: RobotCommandClient = \
            self.robot.ensure_client(RobotCommandClient.default_service_name)
        self.image_client = self.robot.ensure_client(
            ImageClient.default_service_name)
        self.manipulation_api_client = self.robot.ensure_client(
            ManipulationApiClient.default_service_name)
        self.lease_client.take()
        self.lease_keepalive = bosdyn.client.lease.LeaseKeepAlive(
            self.lease_client, must_acquire=True, return_at_exit=True)

        # Create Graph Nav Command Line
        self.upload_filepath = "predicators/spot_utils/bike_env/" + \
            "downloaded_graph/"
        self.graph_nav_command_line = GraphNavInterface(
            self.robot, self.upload_filepath, self.lease_client,
            self.lease_keepalive)

        # Initializing Spot
        self.robot.logger.info(
            "Powering on robot... This may take a several seconds.")
        self.robot.power_on(timeout_sec=20)
        assert self.robot.is_powered_on(), "Robot power on failed."

        self.robot.logger.info("Commanding robot to stand...")
        blocking_stand(self.robot_command_client, timeout_sec=10)
        self.robot.logger.info("Robot standing.")

    def get_camera_images(self) -> Dict[str, Image]:
        """Get all camera images."""
        camera_images: Dict[str, Image] = {}
        for source_name in CAMERA_NAMES:
            img, _ = self.get_single_camera_image(source_name)
            camera_images[source_name] = img
        return camera_images

    def get_single_camera_image(self, source_name: str, to_rgb: bool = False) -> Tuple[Image, Any]:
        """Get a single source camera image and image response."""
        # Get image and camera transform from source_name.
        img_req = build_image_request(
            source_name,
            quality_percent=100,
            pixel_format=image_pb2.Image.PIXEL_FORMAT_RGB_U8)
        image_response = self.image_client.get_image([img_req])

        # Format image before detecting apriltags.
        if image_response[0].shot.image.pixel_format == image_pb2.Image. \
                PIXEL_FORMAT_DEPTH_U16:
            dtype = np.uint16  # type: ignore
        else:
            dtype = np.uint8  # type: ignore
        img = np.fromstring(image_response[0].shot.image.data,
                            dtype=dtype)  # type: ignore
        if image_response[0].shot.image.format == image_pb2.Image.FORMAT_RAW:
            img = img.reshape(image_response[0].shot.image.rows,
                              image_response[0].shot.image.cols)
        else:
            img = cv2.imdecode(img, -1)

        # Convert to RGB color, as some perception models assume RGB format
        # By default, still use BGR to keep backward compability
        if to_rgb:
            img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)

        return (img, image_response)

    def get_objects_in_view(
            self, from_apriltag: bool = True
    ) -> Dict[str, Tuple[float, float, float]]:
        """Get objects currently in view."""
        tag_to_pose: Dict[str,
                          Dict[int,
                               Tuple[float, float,
                                     float]]] = {k: {}
                                                 for k in CAMERA_NAMES}
        for source_name in CAMERA_NAMES:
            # FIXME hardcode for camera names
            if from_apriltag:
                viewable_obj_poses = self.get_apriltag_pose_from_camera(
                    source_name=source_name)
            else:
                viewable_obj_poses = self.get_sam_object_loc_from_camera(
                    source_rgb=source_name,
                    # TODO add depth camera name
                )
            tag_to_pose[source_name].update(viewable_obj_poses)
        apriltag_id_to_obj_name = {
            v: k
            for k, v in obj_name_to_apriltag_id.items()
        }
        camera_to_obj_names_to_poses: Dict[str, Dict[str, Tuple[float, float,
                                                                float]]] = {}
        for source_name in tag_to_pose.keys():
            camera_to_obj_names_to_poses[source_name] = {
                apriltag_id_to_obj_name[t]: p
                for t, p in tag_to_pose[source_name].items()
            }
        return camera_to_obj_names_to_poses

    def get_robot_pose(self) -> Tuple[float, float, float, float]:
        """Get the x, y, z position of the robot body."""
        state = self.get_localized_state()
        gn_origin_tform_body = math_helpers.SE3Pose.from_obj(
            state.localization.seed_tform_body)
        x, y, z = gn_origin_tform_body.transform_point(0.0, 0.0, 0.0)
        yaw = gn_origin_tform_body.rotation.to_yaw()
        return (x, y, z, yaw)

    def actively_construct_initial_object_views(
            self,
            object_names: Set[str]) -> Dict[str, Tuple[float, float, float]]:
        """Walk around and build object views."""
        object_views: Dict[str, Tuple[float, float, float]] = {}
        if CFG.spot_initialize_surfaces_to_default:
            object_views = {
                "tool_room_table":
                (6.939992779470081, -6.21562847222872, 0.030711182602548265),
                "extra_room_table": (8.24384, -6.27615, -0.0035917),
                "low_wall_rack":
                (10.049931203338616, -6.9443170697742, 0.27881268568327966),
                "toolbag":
                (7.043112552148553, -8.198686802340527, -0.18750694527153725)
            }
        waypoints = ["tool_room_table", "low_wall_rack"]
        objects_to_find = object_names - set(object_views.keys())
        obj_name_to_loc = self._scan_for_objects(waypoints, objects_to_find)
        for obj_name in objects_to_find:
            assert obj_name in obj_name_to_loc, \
                f"Did not locate object {obj_name}!"
            object_views[obj_name] = obj_name_to_loc[obj_name]
            logging.info(f"Located object {obj_name}")
        return object_views

    def get_localized_state(self) -> Any:
        """Get localized state from GraphNav client."""
        exec_start, exec_sec = time.perf_counter(), 0.0
        # This needs to be a while loop because get_localization_state
        # sometimes returns null pose if poorly localized. We assert JIC.
        while exec_sec < self.localization_timeout:
            # Localizes robot from larger graph fiducials.
            self.graph_nav_command_line.set_initial_localization_fiducial()
            state = self.graph_nav_command_line.graph_nav_client.\
                get_localization_state()
            exec_sec = time.perf_counter() - exec_start
            if str(state.localization.seed_tform_body) != '':
                break
            time.sleep(1)
        if str(state.localization.seed_tform_body) == '':
            logging.warning("WARNING: Localization timed out!")
        return state

    def get_apriltag_pose_from_camera(
        self,
        source_name: str = "hand_color_image",
        fiducial_size: float = CFG.spot_fiducial_size,
    ) -> Dict[int, Tuple[float, float, float]]:
        """Get the poses of all fiducials in camera view.

        This only works with these camera sources: "hand_color_image",
        "back_fisheye_image", "left_fisheye_image". Also, the fiducial
        size has to be correctly defined in arguments (in mm). Also, it
        only works for tags that start with "40" in their ID.

        Returns a dict mapping the integer of the tag id to an (x, y, z)
        position tuple in the map frame.
        """
        img, image_response = self.get_single_camera_image(source_name)

        # Camera body transform.
        camera_tform_body = get_a_tform_b(
            image_response[0].shot.transforms_snapshot,
            image_response[0].shot.frame_name_image_sensor, BODY_FRAME_NAME)

        # Camera intrinsics for the given source camera.
        intrinsics = image_response[0].source.pinhole.intrinsics

        # Convert the image to grayscale.
        image_grey = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

        # Create apriltag detector and get all apriltag locations.
        options = apriltag.DetectorOptions(families="tag36h11")
        options.refine_pose = 1
        detector = apriltag.Detector(options)
        detections = detector.detect(image_grey)
        obj_poses: Dict[int, Tuple[float, float, float]] = {}
        # For every detection find location in graph_nav frame.
        for detection in detections:
            pose = detector.detection_pose(
                detection,
                (intrinsics.focal_length.x, intrinsics.focal_length.y,
                 intrinsics.principal_point.x, intrinsics.principal_point.y),
                fiducial_size)[0]
            tx, ty, tz, tw = pose[:, -1]
            assert np.isclose(tw, 1.0)
            fiducial_rt_camera_frame = np.array(
                [float(tx) / 1000.0,
                 float(ty) / 1000.0,
                 float(tz) / 1000.0])

            body_tform_fiducial = (
                camera_tform_body.inverse()).transform_point(
                    fiducial_rt_camera_frame[0], fiducial_rt_camera_frame[1],
                    fiducial_rt_camera_frame[2])

            # Get graph_nav to body frame.
            state = self.get_localized_state()
            gn_origin_tform_body = math_helpers.SE3Pose.from_obj(
                state.localization.seed_tform_body)

            # Apply transform to fiducial to body location
            fiducial_rt_gn_origin = gn_origin_tform_body.transform_point(
                body_tform_fiducial[0], body_tform_fiducial[1],
                body_tform_fiducial[2])

            # This only works for small fiducials because of initial size.
            if detection.tag_id in obj_name_to_apriltag_id.values():
                obj_poses[detection.tag_id] = fiducial_rt_gn_origin

        return obj_poses

    def get_sam_object_loc_from_camera(
            self,
            class_name: str or List[str],
            source_rgb: str = "hand_color_image",
            source_depth: str = "hand_depth_in_hand_color_frame",
    ) -> Dict[int, Tuple[float, float, float]]:
        """Get object location in 3D (no orientation) estimated using pretrained SAM model

        Args:
            class_name: name of object class
        """

        assert isinstance(class_name, str) or isinstance(class_name, list)

        # Only support using depth image to obatin location
        # TODO check if they correspond to the same source?
        # TODO check converting to RGB format correctly - SAM needs RGB
        img_rgb, image_response_rgb = self.get_single_camera_image(source_rgb, to_rgb=True)
        img_depth, image_response_depth = self.get_single_camera_image(source_depth)

        res_img = {'rgb': img_rgb, 'depth': img_depth}
        res_response = [image_response_rgb, image_response_depth]

        from perception_utils import get_object_locations_with_sam

        res_locations = get_object_locations_with_sam(
            None,
            classes=[class_name] if isinstance(class_name, str) else class_name,
            in_res_image=res_img,
            in_res_image_responses=res_response
        )

        # TODO transform into the correct reference frame - need to double check
        # Camera body transform.
        camera_tform_body = get_a_tform_b(
            res_response[0].shot.transforms_snapshot,
            res_response[0].shot.frame_name_image_sensor, BODY_FRAME_NAME)

        res_locations_rt_gn_origin = []
        for obj_loc in res_locations:
            object_rt_gn_origin = self.convert_obj_location(camera_tform_body, *obj_loc)
            res_locations_rt_gn_origin.append(object_rt_gn_origin)

        # Use the input class name as the identifier for object(s) and their positions
        return {class_name: res_locations_rt_gn_origin}

    @staticmethod
    def convert_obj_location(self, camera_tform_body, x, y, z):
        body_tform_object = (
            camera_tform_body.inverse()).transform_point(x, y, z)

        # Get graph_nav to body frame.
        state = self.get_localized_state()
        gn_origin_tform_body = math_helpers.SE3Pose.from_obj(
            state.localization.seed_tform_body)

        # Apply transform to object to body location
        object_rt_gn_origin = gn_origin_tform_body.transform_point(
            body_tform_object[0],
            body_tform_object[1],
            body_tform_object[2]
        )

        return object_rt_gn_origin

    @staticmethod
    def rotate_image(image: Image, source_name: str) -> Image:
        """Rotate the image so that it is always displayed upright."""
        if source_name == "frontleft_fisheye_image":
            image = cv2.rotate(image, rotateCode=0)
        elif source_name == "right_fisheye_image":
            image = cv2.rotate(image, rotateCode=1)
        elif source_name == "frontright_fisheye_image":
            image = cv2.rotate(image, rotateCode=0)
        return image

    def get_gripper_obs(self) -> float:
        """Grabs the current observation of relevant quantities from the
        gripper."""
        robot_state = self.robot_state_client.get_robot_state()
        return float(robot_state.manipulator_state.gripper_open_percentage)

    @property
    def params_spaces(self) -> Dict[str, Box]:
        """The parameter spaces for each of the controllers."""
        return {
            "navigate": Box(-5.0, 5.0, (3, )),
            "grasp": Box(-1.0, 1.0, (4, )),
            "placeOnTop": Box(-5.0, 5.0, (3, )),
            "noop": Box(0, 1, (0, ))
        }

    def execute(self, name: str, objects: Sequence[Object],
                params: Array) -> None:
        """Run the controller based on the given name."""
        assert self._connected_to_spot
        if name == "find":
            self._find_controller_move_queue_idx += 1
            return self.findController()
        # Just finished finding.
        self._find_controller_move_queue_idx = 0
        if name == "stow":
            return self.stow_arm()
        if name == "navigate":
            return self.navigateToController(objects, params)
        if name == "grasp":
            return self.graspController(objects, params)
        assert name == "placeOnTop"
        return self.placeOntopController(objects, params)

    def findController(self) -> None:
        """Execute look around."""
        # Execute a hard-coded sequence of movements and hope that one of them
        # puts the lost object in view. This is very specifically designed for
        # the case where an object has fallen in the immediate vicinity.

        # Start by stowing.
        self.stow_arm()

        # First move way back and don't move the hand. This is useful when the
        # object has not actually fallen, but wasn't grasped.
        if self._find_controller_move_queue_idx == 1:
            self.relative_move(-0.75, 0.0, 0.0)
            time.sleep(2.0)
            return

        # Now just look down.
        if self._find_controller_move_queue_idx == 2:
            pass

        # Move to the right.
        elif self._find_controller_move_queue_idx == 3:
            self.relative_move(0.0, 0.0, np.pi / 6)

        # Move to the left.
        elif self._find_controller_move_queue_idx == 4:
            self.relative_move(0.0, 0.0, -np.pi / 6)

        # Soon we should implement asking for help here instead of crashing.
        else:
            prompt = """Please take control of the robot and make the
            object become in its view. Hit the 'Enter' key
            when you're done!"""
            utils.prompt_user(prompt)
            self._find_controller_move_queue_idx = 0
            self.lease_client.take()
            return

        # Move the hand to get a view of the floor.
        self.hand_movement(np.array([0.0, 0.0, 0.0]),
                           keep_hand_pose=False,
                           angle=(np.cos(np.pi / 6), 0, np.sin(np.pi / 6), 0))

        # Sleep for longer to make sure that there is no shaking.
        time.sleep(2.0)

    def navigateToController(self, objs: Sequence[Object],
                             params: Array) -> None:
        """Controller that navigates to specific pre-specified locations.

        Params are [dx, dy, d-yaw (in radians)]
        """
        # Always start by stowing the arm.
        self.stow_arm()

        print("NavigateTo", objs)
        assert len(params) == 3
        assert len(objs) in [2, 3]

        waypoint = ("", np.zeros(3, dtype=np.float32))  # default
        for obj in objs[1:]:
            possible_waypoint = get_memorized_waypoint(obj.name)
            if possible_waypoint is not None:
                waypoint = possible_waypoint
                break
        waypoint_id, offset = waypoint

        params = np.add(params, offset)

        if len(objs) == 3 and objs[2].name == "floor":
            self.navigate_to_position(params)
        else:
            self.navigate_to(waypoint_id, params)

        # Set arm view pose
        # NOTE: time.sleep(1.0) required afer each option execution
        # to allow time for sensor readings to settle.
        if len(objs) == 3:
            if "_table" in objs[2].name:
                self.hand_movement(np.array([0.0, 0.0, 0.0]),
                                   keep_hand_pose=False,
                                   angle=(np.cos(np.pi / 8), 0,
                                          np.sin(np.pi / 8), 0),
                                   open_gripper=False)
                time.sleep(1.0)
                return
            if "floor" in objs[2].name:
                self.hand_movement(np.array([-0.2, 0.0, -0.25]),
                                   keep_hand_pose=False,
                                   angle=(np.cos(np.pi / 7), 0,
                                          np.sin(np.pi / 7), 0),
                                   open_gripper=False)
                time.sleep(1.0)
                return
        self.stow_arm()
        time.sleep(1.0)

    def graspController(self, objs: Sequence[Object], params: Array) -> None:
        """Wrapper method for grasp controller.

        Params are 4 dimensional corresponding to a top-down grasp (1),
        side grasp (-1) or any (0), and dx, dy, dz of post grasp
        position.
        """
        print("Grasp", objs)
        assert len(params) == 4
        assert params[3] in [0, 1, -1]
        if params[3] == 1:
            self._force_horizontal_grasp = False
            self._force_top_down_grasp = True
        elif params[3] == -1:
            self._force_horizontal_grasp = True
            self._force_top_down_grasp = False
        self.arm_object_grasp(objs[1])
        if not np.allclose(params[:3], [0.0, 0.0, 0.0]):
            self.hand_movement(params[:3], open_gripper=False)
        self.stow_arm()
        # NOTE: time.sleep(1.0) required afer each option execution
        # to allow time for sensor readings to settle.
        time.sleep(1.0)

    def placeOntopController(self, objs: Sequence[Object],
                             params: Array) -> None:
        """Wrapper method for placeOnTop controller.

        Params is dx, dy, and dz corresponding to the location of the
        arm from the robot when placing.
        """
        print("PlaceOntop", objs)
        angle = (np.cos(np.pi / 6), 0, np.sin(np.pi / 6), 0)
        assert len(params) == 3
        self.hand_movement(params,
                           keep_hand_pose=False,
                           relative_to_default_pose=False,
                           angle=angle)
        time.sleep(1.0)
        self.stow_arm()
        # NOTE: time.sleep(1.0) required afer each option execution
        # to allow time for sensor readings to settle.
        time.sleep(1.0)

    def _scan_for_objects(
        self, waypoints: Sequence[str], objects_to_find: Collection[str]
    ) -> Dict[str, Tuple[float, float, float]]:
        """Walks around and spins around to find object poses by apriltag."""
        # Stow arm before
        self.stow_arm()
        obj_poses: Dict[str, Tuple[float, float, float]] = {
            "floor": (0.0, 0.0, -1.0)
        }
        for waypoint_name in waypoints:
            if set(objects_to_find).issubset(set(obj_poses)):
                logging.info("All objects located!")
                break
            waypoint = get_memorized_waypoint(waypoint_name)
            assert waypoint is not None
            waypoint_id, offset = waypoint
            self.navigate_to(waypoint_id, offset)
            for _ in range(8):
                objects_in_view: Dict[str, Tuple[float, float, float]] = {}
                objects_in_view_by_camera = self.get_objects_in_view_by_camera(
                )
                for v in objects_in_view_by_camera.values():
                    objects_in_view.update(v)
                obj_poses.update(objects_in_view)
                logging.info("Seen objects:")
                logging.info(set(obj_poses))
                remaining_objects = set(objects_to_find) - set(obj_poses)
                if not remaining_objects:
                    break
                logging.info("Still searching for objects:")
                logging.info(remaining_objects)
                self.relative_move(0.0, 0.0, np.pi / 4)
        return obj_poses

    def verify_estop(self, robot: Any) -> None:
        """Verify the robot is not estopped."""

        client = robot.ensure_client(EstopClient.default_service_name)
        if client.get_status().stop_level != estop_pb2.ESTOP_LEVEL_NONE:
            error_message = "Robot is estopped. Please use an external" + \
                " E-Stop client, such as the estop SDK example, to" + \
                " configure E-Stop."
            robot.logger.error(error_message)
            raise Exception(error_message)

    # NOTE: We want to deprecate this over the long-term!
    def cv_mouse_callback(self, event, x, y, flags, param):  # type: ignore
        """Callback for the click-to-grasp functionality with the Spot API's
        grasping interface."""
        del flags, param
        # pylint: disable=global-variable-not-assigned
        global g_image_click, g_image_display
        clone = g_image_display.copy()
        if event == cv2.EVENT_LBUTTONUP:
            g_image_click = (x, y)
        else:
            # Draw some lines on the image.
            #print('mouse', x, y)
            color = (30, 30, 30)
            thickness = 2
            image_title = 'Click to grasp'
            height = clone.shape[0]
            width = clone.shape[1]
            cv2.line(clone, (0, y), (width, y), color, thickness)
            cv2.line(clone, (x, 0), (x, height), color, thickness)
            cv2.imshow(image_title, clone)

    def add_grasp_constraint(
        self, grasp: manipulation_api_pb2.PickObjectInImage,
        robot_state_client: RobotStateClient
    ) -> manipulation_api_pb2.PickObjectInImage:
        """Method to constrain desirable grasps."""
        # There are 3 types of constraints:
        #   1. Vector alignment
        #   2. Full rotation
        #   3. Squeeze grasp
        #
        # You can specify more than one if you want and they will be
        # OR'ed together.

        # For these options, we'll use a vector alignment constraint.
        use_vector_constraint = self._force_top_down_grasp or \
            self._force_horizontal_grasp

        # Specify the frame we're using.
        grasp.grasp_params.grasp_params_frame_name = VISION_FRAME_NAME

        if use_vector_constraint:
            if self._force_top_down_grasp:
                # Add a constraint that requests that the x-axis of the
                # gripper is pointing in the negative-z direction in the
                # vision frame.

                # The axis on the gripper is the x-axis.
                axis_on_gripper_ewrt_gripper = geometry_pb2.Vec3(x=1, y=0, z=0)

                # The axis in the vision frame is the negative z-axis
                axis_to_align_with_ewrt_vo = geometry_pb2.Vec3(x=0, y=0, z=-1)

            if self._force_horizontal_grasp:
                # Add a constraint that requests that the y-axis of the
                # gripper is pointing in the positive-z direction in the
                # vision frame.  That means that the gripper is
                # constrained to be rolled 90 degrees and pointed at the
                # horizon.

                # The axis on the gripper is the y-axis.
                axis_on_gripper_ewrt_gripper = geometry_pb2.Vec3(x=0, y=1, z=0)

                # The axis in the vision frame is the positive z-axis
                axis_to_align_with_ewrt_vo = geometry_pb2.Vec3(x=0, y=0, z=1)

            # Add the vector constraint to our proto.
            constraint = grasp.grasp_params.allowable_orientation.add()
            constraint.vector_alignment_with_tolerance.\
                axis_on_gripper_ewrt_gripper.\
                    CopyFrom(axis_on_gripper_ewrt_gripper)
            constraint.vector_alignment_with_tolerance.\
                axis_to_align_with_ewrt_frame.\
                    CopyFrom(axis_to_align_with_ewrt_vo)

            # We'll take anything within about 10 degrees for top-down or
            # horizontal grasps.
            constraint.vector_alignment_with_tolerance.\
                threshold_radians = 0.17

        elif self._force_45_angle_grasp:
            # Demonstration of a RotationWithTolerance constraint.
            # This constraint allows you to specify a full orientation you
            # want the hand to be in, along with a threshold.
            # You might want this feature when grasping an object with known
            # geometry and you want to make sure you grasp a specific part
            # of it. Here, since we don't have anything in particular we
            # want to grasp,  we'll specify an orientation that will have the
            # hand aligned with robot and rotated down 45 degrees as an
            # example.

            # First, get the robot's position in the world.
            robot_state = robot_state_client.get_robot_state()
            vision_T_body = get_vision_tform_body(
                robot_state.kinematic_state.transforms_snapshot)

            # Rotation from the body to our desired grasp.
            body_Q_grasp = math_helpers.Quat.from_pitch(0.785398)  # 45 degrees
            vision_Q_grasp = vision_T_body.rotation * body_Q_grasp

            # Turn into a proto
            constraint = grasp.grasp_params.allowable_orientation.add()
            constraint.rotation_with_tolerance.rotation_ewrt_frame.CopyFrom(
                vision_Q_grasp.to_proto())

            # We'll accept anything within +/- 10 degrees
            constraint.rotation_with_tolerance.threshold_radians = 0.17

        elif self._force_squeeze_grasp:
            # Tell the robot to just squeeze on the ground at the given point.
            constraint = grasp.grasp_params.allowable_orientation.add()
            constraint.squeeze_grasp.SetInParent()

        return grasp

    def arm_object_grasp(self, obj: Object) -> None:
        """A simple example of using the Boston Dynamics API to command Spot's
        arm."""
        assert self.robot.is_powered_on(), "Robot power on failed."
        assert basic_command_pb2.StandCommand.Feedback.STATUS_IS_STANDING

        # Take a picture with a camera
        self.robot.logger.debug(f'Getting an image from: {self._image_source}')
        time.sleep(1)
        image_responses = self.image_client.get_image_from_sources(
            [self._image_source])

        if len(image_responses) != 1:
            print(f'Got invalid number of images: {str(len(image_responses))}')
            print(image_responses)
            assert False

        image = image_responses[0]
        if image.shot.image.pixel_format == image_pb2.Image.\
            PIXEL_FORMAT_DEPTH_U16:
            dtype = np.uint16  # type: ignore
        else:
            dtype = np.uint8  # type: ignore
        img = np.fromstring(image.shot.image.data, dtype=dtype)  # type: ignore
        if image.shot.image.format == image_pb2.Image.FORMAT_RAW:
            img = img.reshape(image.shot.image.rows, image.shot.image.cols)
        else:
            img = cv2.imdecode(img, -1)

        # pylint: disable=global-variable-not-assigned, global-statement
        global g_image_click, g_image_display

        if CFG.spot_grasp_use_apriltag:
            # Convert Image to grayscale
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

            # Define the AprilTags detector options and then detect the tags.
            self.robot.logger.info("[INFO] detecting AprilTags...")
            options = apriltag.DetectorOptions(families="tag36h11")
            detector = apriltag.Detector(options)
            results = detector.detect(gray)
            self.robot.logger.info(f"[INFO] {len(results)} AprilTags detected")
            for result in results:
                if result.tag_id == obj_name_to_apriltag_id[obj.name]:
                    g_image_click = result.center

        elif CFG.spot_grasp_use_cv2:
            if obj.name in ["hammer", "hex_key", "brush", "hex_screwdriver"]:
                g_image_click = _find_object_center(img, obj.name)

        if g_image_click is None:
            # Show the image to the user and wait for them to click on a pixel
            self.robot.logger.info('Click on an object to start grasping...')
            image_title = 'Click to grasp'
            cv2.namedWindow(image_title)
            cv2.setMouseCallback(image_title, self.cv_mouse_callback)
            g_image_display = img
            cv2.imshow(image_title, g_image_display)

        while g_image_click is None:
            key = cv2.waitKey(1) & 0xFF
            if key == ord('q') or key == ord('Q'):
                # Quit
                print('"q" pressed, exiting.')
                sys.exit()

        # Uncomment to debug.
        # g_image_display = img.copy()
        # image_title = "Selected grasp"
        # cv2.namedWindow(image_title)
        # cv2.circle(g_image_display, g_image_click, 3, (0, 255, 0), 3)
        # cv2.imshow(image_title, g_image_display)
        # cv2.waitKey(0)

        # pylint: disable=unsubscriptable-object
        self.robot.\
            logger.info(f"Object at ({g_image_click[0]}, {g_image_click[1]})")
        # pylint: disable=unsubscriptable-object
        pick_vec = geometry_pb2.Vec2(x=g_image_click[0], y=g_image_click[1])

        # Build the proto
        grasp = manipulation_api_pb2.PickObjectInImage(
            pixel_xy=pick_vec,
            transforms_snapshot_for_camera=image.shot.transforms_snapshot,
            frame_name_image_sensor=image.shot.frame_name_image_sensor,
            camera_model=image.source.pinhole)

        # Optionally add a grasp constraint.  This lets you tell the robot you
        # only want top-down grasps or side-on grasps.
        grasp = self.add_grasp_constraint(grasp, self.robot_state_client)

        # Stow Arm First
        self.stow_arm()

        # Ask the robot to pick up the object
        grasp_request = manipulation_api_pb2.ManipulationApiRequest(
            pick_object_in_image=grasp)

        # Send the request
        cmd_response = self.manipulation_api_client.manipulation_api_command(
            manipulation_api_request=grasp_request)

        # Get feedback from the robot and execute grasping.
        start_time = time.perf_counter()
        while (time.perf_counter() - start_time) <= COMMAND_TIMEOUT:
            feedback_request = manipulation_api_pb2.\
                ManipulationApiFeedbackRequest(manipulation_cmd_id=\
                    cmd_response.manipulation_cmd_id)

            # Send the request
            response = self.manipulation_api_client.\
                manipulation_api_feedback_command(
                manipulation_api_feedback_request=feedback_request)

            if response.current_state in [manipulation_api_pb2.\
                MANIP_STATE_GRASP_SUCCEEDED, manipulation_api_pb2.\
                MANIP_STATE_GRASP_FAILED]:
                break
        if (time.perf_counter() - start_time) > COMMAND_TIMEOUT:
            logging.info("Timed out waiting for grasp to execute!")

        time.sleep(1.0)
        g_image_click = None
        g_image_display = None
        self.robot.logger.debug('Finished grasp.')

    def stow_arm(self) -> None:
        """A simple example of using the Boston Dynamics API to stow Spot's
        arm."""

        # Allow Stowing and Stow Arm
        grasp_carry_state_override = manipulation_api_pb2.\
            ApiGraspedCarryStateOverride(override_request=3)
        grasp_override_request = manipulation_api_pb2.\
            ApiGraspOverrideRequest(
            carry_state_override=grasp_carry_state_override)
        self.manipulation_api_client.\
            grasp_override_command(grasp_override_request)

        stow_cmd = RobotCommandBuilder.arm_stow_command()
        gripper_close_command = RobotCommandBuilder.\
            claw_gripper_open_fraction_command(0.0)
        # Combine the arm and gripper commands into one RobotCommand
        stow_and_close_command = RobotCommandBuilder.build_synchro_command(
            gripper_close_command, stow_cmd)
        stow_and_close_command_id = self.robot_command_client.robot_command(
            stow_and_close_command)
        self.robot.logger.debug("Stow command issued.")
        block_until_arm_arrives(self.robot_command_client,
                                stow_and_close_command_id, 4.5)

    def hand_movement(
        self,
        params: Array,
        open_gripper: bool = True,
        relative_to_default_pose: bool = True,
        keep_hand_pose: bool = True,
        angle: Tuple[float, float, float,
                     float] = (np.cos(np.pi / 4), 0, np.sin(np.pi / 4), 0)
    ) -> None:
        """Move arm to infront of robot an open gripper."""
        # Move the arm to a spot in front of the robot, and open the gripper.
        assert self.robot.is_powered_on(), "Robot power on failed."
        assert basic_command_pb2.StandCommand.Feedback.STATUS_IS_STANDING

        if keep_hand_pose:
            # Get current hand quaternion.
            robot_state = self.robot_state_client.get_robot_state()
            body_T_hand = get_a_tform_b(
                robot_state.kinematic_state.transforms_snapshot,
                BODY_FRAME_NAME, "hand")
            qw, qx, qy, qz = body_T_hand.rot.w, body_T_hand.rot.x,\
                body_T_hand.rot.y, body_T_hand.rot.z
        else:
            # Set downward place rotation as a quaternion.
            qw, qx, qy, qz = angle
        flat_body_Q_hand = geometry_pb2.Quaternion(w=qw, x=qx, y=qy, z=qz)

        if not relative_to_default_pose:
            x = params[0]  # dx hand
            y = params[1]
            z = params[2]
        else:
            x = self.hand_x + params[0]  # dx hand
            y = self.hand_y + params[1]
            z = self.hand_z + params[2]

        # Here we are making sure the hand pose is within our range and
        # if it is not we are clipping it to be, and then displacing the
        # robot respectively.
        x_clipped = np.clip(x, self.hand_x_bounds[0], self.hand_x_bounds[1])
        y_clipped = np.clip(y, self.hand_y_bounds[0], self.hand_y_bounds[1])
        z_clipped = np.clip(z, self.hand_z_bounds[0], self.hand_z_bounds[1])
        self.relative_move((x - x_clipped), (y - y_clipped), 0.0)
        x = x_clipped
        y = y_clipped
        z = z_clipped

        hand_ewrt_flat_body = geometry_pb2.Vec3(x=x, y=y, z=z)

        flat_body_T_hand = geometry_pb2.SE3Pose(position=hand_ewrt_flat_body,
                                                rotation=flat_body_Q_hand)

        robot_state = self.robot_state_client.get_robot_state()
        odom_T_flat_body = get_a_tform_b(
            robot_state.kinematic_state.transforms_snapshot, ODOM_FRAME_NAME,
            GRAV_ALIGNED_BODY_FRAME_NAME)

        odom_T_hand = odom_T_flat_body * math_helpers.SE3Pose.from_obj(
            flat_body_T_hand)

        # duration in seconds
        seconds = 2

        arm_command = RobotCommandBuilder.arm_pose_command(
            odom_T_hand.x, odom_T_hand.y, odom_T_hand.z, odom_T_hand.rot.w,
            odom_T_hand.rot.x, odom_T_hand.rot.y, odom_T_hand.rot.z,
            ODOM_FRAME_NAME, seconds)

        # Make the close gripper RobotCommand
        gripper_command = RobotCommandBuilder.\
            claw_gripper_open_fraction_command(0.0)

        # Combine the arm and gripper commands into one RobotCommand
        command = RobotCommandBuilder.build_synchro_command(
            gripper_command, arm_command)

        # Send the request
        cmd_id: int = self.robot_command_client.robot_command(command)
        self.robot.logger.debug('Moving arm to position.')

        # Wait until the arm arrives at the goal.
        block_until_arm_arrives(self.robot_command_client, cmd_id, 3.0)

        time.sleep(1.0)

        if not open_gripper:
            gripper_command = RobotCommandBuilder.\
                claw_gripper_open_fraction_command(0.0)
        else:
            gripper_command = RobotCommandBuilder.\
                claw_gripper_open_fraction_command(1.0)

        # Combine the arm and gripper commands into one RobotCommand
        command = RobotCommandBuilder.build_synchro_command(
            gripper_command, arm_command)

        # Send the request
        cmd_id = self.robot_command_client.robot_command(command)
        self.robot.logger.debug('Moving arm to position.')

        # Wait until the arm arrives at the goal.
        block_until_arm_arrives(self.robot_command_client, cmd_id, 3.0)
        time.sleep(1.0)

    def navigate_to(self, waypoint_id: str, params: Array) -> None:
        """Use GraphNavInterface to localize robot and go to a location."""
        # pylint: disable=broad-except
        try:
            # (1) Initialize location
            self.graph_nav_command_line.set_initial_localization_fiducial()
            self.graph_nav_command_line.graph_nav_client.get_localization_state(
            )

            # (2) Navigate to
            self.graph_nav_command_line.navigate_to([waypoint_id])

            # (3) Offset by params
            if not np.allclose(params, [0.0, 0.0, 0.0]):
                self.relative_move(params[0], params[1], params[2])

        except Exception as e:
            logging.info(e)

    def relative_move(self,
                      dx: float,
                      dy: float,
                      dyaw: float,
                      stairs: bool = False) -> bool:
        """Move to relative robot position in body frame."""
        transforms = self.robot_state_client.get_robot_state(
        ).kinematic_state.transforms_snapshot

        # Build the transform for where we want the robot to be
        # relative to where the body currently is.
        body_tform_goal = math_helpers.SE2Pose(x=dx, y=dy, angle=dyaw)
        # We do not want to command this goal in body frame because
        # the body will move, thus shifting our goal. Instead, we
        # transform this offset to get the goal position in the output
        # frame (which will be either odom or vision).
        out_tform_body = get_se2_a_tform_b(transforms, ODOM_FRAME_NAME,
                                           BODY_FRAME_NAME)
        out_tform_goal = out_tform_body * body_tform_goal

        # Command the robot to go to the goal point in the specified
        # frame. The command will stop at the new position.
        robot_cmd = RobotCommandBuilder.synchro_se2_trajectory_point_command(
            goal_x=out_tform_goal.x,
            goal_y=out_tform_goal.y,
            goal_heading=out_tform_goal.angle,
            frame_name=ODOM_FRAME_NAME,
            params=RobotCommandBuilder.mobility_params(stair_hint=stairs))
        cmd_id = self.robot_command_client.robot_command(
            lease=None,
            command=robot_cmd,
            end_time_secs=time.time() + COMMAND_TIMEOUT)
        start_time = time.perf_counter()
        while (time.perf_counter() - start_time) <= COMMAND_TIMEOUT:
            feedback = self.robot_command_client.\
                robot_command_feedback(cmd_id)
            mobility_feedback = feedback.feedback.\
                synchronized_feedback.mobility_command_feedback
            if mobility_feedback.status != \
                RobotCommandFeedbackStatus.STATUS_PROCESSING:
                logging.info("Failed to reach the goal")
                return False
            traj_feedback = mobility_feedback.se2_trajectory_feedback
            if (traj_feedback.status == traj_feedback.STATUS_AT_GOAL
                    and traj_feedback.body_movement_status
                    == traj_feedback.BODY_STATUS_SETTLED):
                logging.info("Arrived at the goal.")
                return True
            time.sleep(1)
        if (time.perf_counter() - start_time) > COMMAND_TIMEOUT:
            logging.info("Timed out waiting for movement to execute!")
        return False

    def navigate_to_position(self, params: Array) -> None:
        """Use GraphNavInterface to localize robot and go to a position."""
        # pylint: disable=broad-except

        try:
            # (1) Initialize location
            self.graph_nav_command_line.set_initial_localization_fiducial()
            self.graph_nav_command_line.graph_nav_client.get_localization_state(
            )

            # (2) Just move
            self.relative_move(params[0], params[1], params[2])

        except Exception as e:
            logging.info(e)


@functools.lru_cache(maxsize=None)
def get_spot_interface() -> _SpotInterface:
    """Ensure that _SpotControllers is only created once."""
    return _SpotInterface()
