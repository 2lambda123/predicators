"""Structs for perception."""

from dataclasses import dataclass
from typing import Any, Tuple

import numpy as np
from bosdyn.api.geometry_pb2 import FrameTreeSnapshot
from bosdyn.client import math_helpers
from numpy.typing import NDArray
from scipy import ndimage


@dataclass
class RGBDImageWithContext:
    """An RGBD image with context including the pose and intrinsics of the
    camera."""
    rgb: NDArray[np.uint8]
    depth: NDArray[np.uint16]
    image_rot: float
    camera_name: str
    world_tform_camera: math_helpers.SE3Pose
    depth_scale: float
    transforms_snapshot: FrameTreeSnapshot
    frame_name_image_sensor: str
    camera_model: Any  # bosdyn.api.image_pb2.PinholeModel, but not available

    @property
    def rotated_rgb(self) -> NDArray[np.uint8]:
        """The image rotated to be upright."""
        return ndimage.rotate(self.rgb, self.image_rot, reshape=False)


@dataclass(frozen=True)
class ObjectDetectionID:
    """A unique identifier for an object that is to be detected."""


@dataclass(frozen=True)
class AprilTagObjectDetectionID(ObjectDetectionID):
    """An ID for an object to be detected from an april tag.

    The object center is defined to be the center of the april tag plus
    offset.
    """
    april_tag_number: int
    offset_transform: math_helpers.SE3Pose


@dataclass(frozen=True)
class LanguageObjectDetectionID(ObjectDetectionID):
    """An ID for an object to be detected with a vision-language model."""
    language_id: str


@dataclass(frozen=True)
class SegmentedBoundingBox:
    """Intermediate return value from vision-language models."""
    bounding_box: Tuple[float, float, float, float]
    mask: NDArray[np.uint8]
    score: float
