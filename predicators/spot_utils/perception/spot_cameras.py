"""Utility functions for capturing images from Spot's cameras."""
from typing import Collection, Dict, Type

import cv2
import numpy as np
from bosdyn.api import image_pb2
from bosdyn.client.frame_helpers import BODY_FRAME_NAME, get_a_tform_b
from bosdyn.client.image import ImageClient, build_image_request
from bosdyn.client.sdk import Robot
from numpy.typing import NDArray

from predicators.spot_utils.perception.structs import RGBDImageWithContext

ROTATION_ANGLE = {
    'hand_color_image': 0,
    'back_fisheye_image': 0,
    'frontleft_fisheye_image': -78,
    'frontright_fisheye_image': -102,
    'left_fisheye_image': 0,
    'right_fisheye_image': 180
}
RGB_TO_DEPTH_CAMERAS = {
    "hand_color_image": "hand_depth_in_hand_color_frame",
    "left_fisheye_image": "left_depth_in_visual_frame",
    "right_fisheye_image": "right_depth_in_visual_frame",
    "frontleft_fisheye_image": "frontleft_depth_in_visual_frame",
    "frontright_fisheye_image": "frontright_depth_in_visual_frame",
    "back_fisheye_image": "back_depth_in_visual_frame"
}


def capture_images(
    robot: Robot,
    camera_names: Collection[str],
    quality_percent: int = 100,
) -> Dict[str, RGBDImageWithContext]:
    """Build an image request and get the responses."""
    image_client = robot.ensure_client(ImageClient.default_service_name)

    rgbds: Dict[str, RGBDImageWithContext] = {}

    # Package all the requests together.
    img_reqs: image_pb2.ImageRequest = []
    for camera_name in camera_names:

        # Build RGB image request.
        if "hand" in camera_name:
            rgb_pixel_format = None
        else:
            rgb_pixel_format = image_pb2.Image.PIXEL_FORMAT_RGB_U8  # pylint: disable=no-member
        rgb_img_req = build_image_request(camera_name,
                                          quality_percent=quality_percent,
                                          pixel_format=rgb_pixel_format)
        img_reqs.append(rgb_img_req)

        # Build depth image request.
        depth_camera_name = RGB_TO_DEPTH_CAMERAS[camera_name]
        depth_img_req = build_image_request(depth_camera_name,
                                            quality_percent=quality_percent,
                                            pixel_format=None)
        img_reqs.append(depth_img_req)

    # Send the request.
    responses = image_client.get_image(img_reqs)
    name_to_response = {r.source.name: r for r in responses}

    # Build RGBDImageWithContexts.
    for camera in camera_names:
        rgb_img_resp = name_to_response[camera]
        depth_img_resp = name_to_response[RGB_TO_DEPTH_CAMERAS[camera]]
        rgb_img = _image_response_to_image(rgb_img_resp)
        depth_img = _image_response_to_image(depth_img_resp)
        # Create transform.
        camera_tform_body = get_a_tform_b(
            rgb_img_resp.shot.transforms_snapshot,
            rgb_img_resp.shot.frame_name_image_sensor, BODY_FRAME_NAME)
        body_tform_camera = camera_tform_body.inverse()
        # Extract RGB camera intrinsics.
        rot = ROTATION_ANGLE[camera_name]
        intrinsics = rgb_img_resp.source.pinhole.intrinsics
        depth_scale = depth_img_resp.source.depth_scale
        # Finish RGBDImageWithContext.
        rgbd = RGBDImageWithContext(rgb_img, depth_img, rot, camera_name,
                                    body_tform_camera, intrinsics, depth_scale)
        rgbds[camera] = rgbd

    return rgbds


def _image_response_to_image(
    image_response: image_pb2.ImageResponse, ) -> NDArray:
    """Extract an image from an image response.

    The type of image (rgb, depth, etc.) is detected based on the
    format.
    """
    # pylint: disable=no-member
    pixel_format = image_response.shot.image.pixel_format
    if pixel_format == image_pb2.Image.PIXEL_FORMAT_DEPTH_U16:
        dtype: Type[np.unsignedinteger] = np.uint16
    else:
        dtype = np.uint8

    # Read from the buffer.
    raw_image = image_response.shot.image
    img = np.frombuffer(raw_image.data, dtype=dtype)
    if image_response.shot.image.format == image_pb2.Image.FORMAT_RAW:
        img = img.reshape(raw_image.rows, raw_image.cols)
    else:
        img = cv2.imdecode(img, -1)

    # Convert BGR to RGB.
    if pixel_format in [
            image_pb2.Image.PIXEL_FORMAT_RGB_U8,
            image_pb2.Image.PIXEL_FORMAT_RGBA_U8
    ]:
        img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)

    # Squeeze to remove last channel for depth and grayscale images.
    img = img.squeeze()

    return img


if __name__ == "__main__":
    # Run this file alone to test manually.
    # Make sure to pass in --spot_robot_ip.

    # pylint: disable=ungrouped-imports
    import imageio.v2 as iio
    from bosdyn.client import create_standard_sdk
    from bosdyn.client.lease import LeaseClient
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

        sdk = create_standard_sdk('SpotCameraTestClient')
        robot = sdk.create_robot(hostname)
        authenticate(robot)
        verify_estop(robot)
        lease_client = robot.ensure_client(LeaseClient.default_service_name)
        lease_client.take()
        robot.time_sync.wait_for_sync()

        # Take pictures out of all the cameras.
        for camera in RGB_TO_DEPTH_CAMERAS:
            print(f"Capturing image from {camera}")
            rgbd = capture_images(robot, [camera])[camera]
            outfile = f"{camera}_manual_test_rgb_output.png"
            iio.imsave(outfile, rgbd.rgb)
            print(f"Wrote out to {outfile}")
            outfile = f"{camera}_manual_test_depth_output.png"
            iio.imsave(outfile, 255 * rgbd.depth)
            print(f"Wrote out to {outfile}")

    _run_manual_test()
