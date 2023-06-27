"""Ground-truth NSRTs for the PDDLEnv."""

from typing import Dict, Sequence, Set

import numpy as np
from bosdyn.client import math_helpers

from predicators.envs import get_or_create_env
from predicators.envs.spot_env import SpotEnv
from predicators.ground_truth_models import GroundTruthNSRTFactory
from predicators.spot_utils.spot_utils import get_spot_interface
from predicators.structs import NSRT, Array, GroundAtom, Object, \
    ParameterizedOption, Predicate, State, Type
from predicators.utils import null_sampler


class SpotEnvsGroundTruthNSRTFactory(GroundTruthNSRTFactory):
    """Ground-truth NSRTs for the Spot Env."""

    @classmethod
    def get_env_names(cls) -> Set[str]:
        return {"spot_grocery_env", "spot_bike_env"}

    @staticmethod
    def get_nsrts(env_name: str, types: Dict[str, Type],
                  predicates: Dict[str, Predicate],
                  options: Dict[str, ParameterizedOption]) -> Set[NSRT]:

        _spot_interface = get_spot_interface()

        def move_sampler(state: State, goal: Set[GroundAtom],
                         rng: np.random.Generator,
                         objs: Sequence[Object]) -> Array:
            del goal
            assert len(objs) in [2, 3]
            if objs[1].type.name == "bag":  # pragma: no cover
                return np.array([0.5, 0.0, 0.0])
            # Sample dyaw so that there is some hope of seeing objects from
            # different angles.
            dyaw = rng.uniform(-np.pi / 8, np.pi / 8)
            # For MoveToObjOnFloor
            if len(objs) == 3:
                if objs[2].name == "floor":
                    obj = objs[1]
                    # Get graph_nav to body frame.
                    gn_state = _spot_interface.get_localized_state()
                    gn_origin_tform_body = math_helpers.SE3Pose.from_obj(
                        gn_state.localization.seed_tform_body)

                    # Transform fiducial pose for relative pose.
                    body_tform_fiducial = gn_origin_tform_body.inverse(
                    ).transform_point(state.get(obj, "x"), state.get(obj, "y"),
                                      state.get(obj, "z"))
                    obj_x, obj_y = [
                        body_tform_fiducial[0], body_tform_fiducial[1]
                    ]

                    spot_xy = np.array([0.0, 0.0])
                    obj_xy = np.array([obj_x, obj_y])
                    distance = np.linalg.norm(obj_xy - spot_xy)
                    obj_unit_vector = (obj_xy - spot_xy) / distance

                    distance_to_obj = 1.25
                    new_xy = spot_xy + obj_unit_vector * (distance -
                                                          distance_to_obj)
                    # Find the angle change needed to look at object
                    angle = np.arccos(
                        np.clip(np.dot(np.array([1.0, 0.0]), obj_unit_vector),
                                -1.0, 1.0))
                    # Check which direction with allclose
                    if not np.allclose(
                            obj_unit_vector,
                        [np.cos(angle), np.sin(angle)],
                            atol=0.1):
                        angle = -angle
                    return np.array([new_xy[0], new_xy[1], angle + dyaw])
            return np.array([-0.25, 0.0, dyaw])

        def grasp_sampler(state: State, goal: Set[GroundAtom],
                          rng: np.random.Generator,
                          objs: Sequence[Object]) -> Array:
            del state, goal, rng
            if objs[1].type.name == "bag":  # pragma: no cover
                return np.array([0.0, 0.0, 0.0, -1.0])
            if objs[2].name == "low_wall_rack":  # pragma: no cover
                return np.array([0.0, 0.0, 0.1, 0.0])
            return np.array([0.0, 0.0, 0.0, 0.0])

        def place_sampler(state: State, goal: Set[GroundAtom],
                          rng: np.random.Generator,
                          objs: Sequence[Object]) -> Array:
            del goal

            obj = objs[2]
            if obj.name == "floor":
                # Drop right in front of spot
                return np.array([0.5, 0.0, 0.0])

            # Get graph_nav to body frame.
            gn_state = _spot_interface.get_localized_state()
            gn_origin_tform_body = math_helpers.SE3Pose.from_obj(
                gn_state.localization.seed_tform_body)

            # Apply transform to fiducial pose to get relative body location.
            body_tform_fiducial = gn_origin_tform_body.inverse(
            ).transform_point(state.get(obj, "x"), state.get(obj, "y"),
                              state.get(obj, "z"))
            fiducial_pose = np.array([
                body_tform_fiducial[0], body_tform_fiducial[1],
                _spot_interface.hand_z
            ])
            if objs[2].type.name == "bag":  # pragma: no cover
                return fiducial_pose + np.array([0.1, 0.0, -0.25])
            if "_table" in objs[2].name:
                dx = rng.uniform(0.19, 0.21)
                dy = rng.uniform(0.08, 0.22)
                dz = rng.uniform(-0.61, -0.59)

                # Oracle values for slanted table.
                # dx = 0.2
                # dy = 0.15
                # dz = -0.6

                return fiducial_pose + np.array([dx, dy, dz])
            return fiducial_pose + np.array([0.0, 0.0, 0.0])

        env = get_or_create_env(env_name)
        assert isinstance(env, SpotEnv)

        nsrts = set()

        for strips_op in env.strips_operators:
            option = options[strips_op.name]
            if "MoveTo" in strips_op.name:
                nsrt = strips_op.make_nsrt(
                    option=option,
                    option_vars=strips_op.parameters,
                    sampler=move_sampler,
                )
            elif "Grasp" in strips_op.name:
                nsrt = strips_op.make_nsrt(
                    option=option,
                    option_vars=strips_op.parameters,
                    sampler=grasp_sampler,
                )
            elif "Place" in strips_op.name:
                nsrt = strips_op.make_nsrt(
                    option=option,
                    option_vars=strips_op.parameters,
                    sampler=place_sampler,
                )
            else:
                nsrt = strips_op.make_nsrt(
                    option=option,
                    option_vars=strips_op.parameters,
                    sampler=null_sampler,
                )
            nsrts.add(nsrt)

        return nsrts
