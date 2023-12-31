"""Test cases for pybullet_robots."""

import numpy as np
import pybullet as p
import pytest

from predicators import utils
from predicators.pybullet_helpers.geometry import Pose
from predicators.pybullet_helpers.inverse_kinematics import \
    pybullet_inverse_kinematics
from predicators.pybullet_helpers.joint import get_kinematic_chain
from predicators.pybullet_helpers.link import BASE_LINK, get_link_pose, \
    get_link_state
from predicators.pybullet_helpers.robots import \
    create_single_arm_pybullet_robot
from predicators.pybullet_helpers.robots.fetch import FetchPyBulletRobot
from predicators.pybullet_helpers.robots.panda import PandaPyBulletRobot
from predicators.settings import CFG


@pytest.fixture(scope="module", name="scene_attributes")
def _setup_pybullet_test_scene():
    """Creates a PyBullet scene with a fetch robot.

    Initialized only once for efficiency.
    """
    scene = {}

    physics_client_id = p.connect(p.DIRECT)
    scene["physics_client_id"] = physics_client_id

    p.resetSimulation(physicsClientId=physics_client_id)

    fetch_id = p.loadURDF(
        utils.get_env_asset_path("urdf/fetch_description/robots/fetch.urdf"),
        useFixedBase=True,
        physicsClientId=physics_client_id)
    scene["fetch_id"] = fetch_id

    base_pose = [0.75, 0.7441, 0.0]
    base_orientation = [0., 0., 0., 1.]
    p.resetBasePositionAndOrientation(fetch_id,
                                      base_pose,
                                      base_orientation,
                                      physicsClientId=physics_client_id)
    reconstructed_pose = get_link_pose(fetch_id, BASE_LINK, physics_client_id)
    assert reconstructed_pose.allclose(Pose(base_pose, base_orientation))

    joint_names = [
        p.getJointInfo(fetch_id, i,
                       physicsClientId=physics_client_id)[1].decode("utf-8")
        for i in range(
            p.getNumJoints(fetch_id, physicsClientId=physics_client_id))
    ]
    ee_id = joint_names.index('gripper_axis')
    scene["ee_id"] = ee_id
    scene["ee_orientation"] = [1., 0., -1., 0.]

    scene["robot_home"] = [1.35, 0.75, 0.75]

    arm_joints = get_kinematic_chain(fetch_id,
                                     ee_id,
                                     physics_client_id=physics_client_id)
    scene["initial_joints_states"] = p.getJointStates(
        fetch_id, arm_joints, physicsClientId=physics_client_id)

    yield scene

    # Disconnect from physics server so it does not linger
    p.disconnect(physics_client_id)


def test_get_kinematic_chain(scene_attributes):
    """Tests for get_kinematic_chain()."""
    arm_joints = get_kinematic_chain(
        scene_attributes["fetch_id"],
        scene_attributes["ee_id"],
        physics_client_id=scene_attributes["physics_client_id"])
    # Fetch arm has 7 DOF.
    assert len(arm_joints) == 7


def test_pybullet_inverse_kinematics(scene_attributes):
    """Tests for pybullet_inverse_kinematics()."""
    arm_joints = get_kinematic_chain(
        scene_attributes["fetch_id"],
        scene_attributes["ee_id"],
        physics_client_id=scene_attributes["physics_client_id"])

    # Reset the joint states to their initial values.
    def _reset_joints():
        for joint, joints_state in zip(
                arm_joints, scene_attributes["initial_joints_states"]):
            position, velocity, _, _ = joints_state
            p.resetJointState(
                scene_attributes["fetch_id"],
                joint,
                targetValue=position,
                targetVelocity=velocity,
                physicsClientId=scene_attributes["physics_client_id"])

    target_position = scene_attributes["robot_home"]
    # With validate = False, one call to IK is not good enough.
    _reset_joints()
    joint_positions = pybullet_inverse_kinematics(
        scene_attributes["fetch_id"],
        scene_attributes["ee_id"],
        target_position,
        scene_attributes["ee_orientation"],
        arm_joints,
        physics_client_id=scene_attributes["physics_client_id"],
        validate=False)
    for joint, joint_val in zip(arm_joints, joint_positions):
        p.resetJointState(
            scene_attributes["fetch_id"],
            joint,
            targetValue=joint_val,
            physicsClientId=scene_attributes["physics_client_id"])
    ee_link_state = get_link_state(scene_attributes["fetch_id"],
                                   scene_attributes["ee_id"],
                                   scene_attributes["physics_client_id"])
    assert not np.allclose(
        ee_link_state[4], target_position, atol=CFG.pybullet_ik_tol)
    # With validate = True, IK does work.
    _reset_joints()
    joint_positions = pybullet_inverse_kinematics(
        scene_attributes["fetch_id"],
        scene_attributes["ee_id"],
        target_position,
        scene_attributes["ee_orientation"],
        arm_joints,
        physics_client_id=scene_attributes["physics_client_id"],
        validate=True)
    for joint, joint_val in zip(arm_joints, joint_positions):
        p.resetJointState(
            scene_attributes["fetch_id"],
            joint,
            targetValue=joint_val,
            physicsClientId=scene_attributes["physics_client_id"])
    ee_link_state = get_link_state(scene_attributes["fetch_id"],
                                   scene_attributes["ee_id"],
                                   scene_attributes["physics_client_id"])
    assert np.allclose(ee_link_state[4],
                       target_position,
                       atol=CFG.pybullet_ik_tol)
    # With validate = True, if the position is impossible to reach, an error
    # is raised.
    target_position = [
        target_position[0], target_position[1], target_position[2] + 100.0
    ]
    with pytest.raises(Exception) as e:
        pybullet_inverse_kinematics(
            scene_attributes["fetch_id"],
            scene_attributes["ee_id"],
            target_position,
            scene_attributes["ee_orientation"],
            arm_joints,
            physics_client_id=scene_attributes["physics_client_id"],
            validate=True)
    assert "Inverse kinematics failed to converge." in str(e)


def test_fetch_pybullet_robot(physics_client_id):
    """Tests for FetchPyBulletRobot()."""
    ee_home_position = (1.35, 0.75, 0.75)
    ee_orn = p.getQuaternionFromEuler([0.0, np.pi / 2, -np.pi])
    ee_home_pose = Pose(ee_home_position, ee_orn)
    base_pose = Pose((0.75, 0.7441, 0.0))
    robot = FetchPyBulletRobot(ee_home_pose, physics_client_id, base_pose)
    assert robot.get_name() == "fetch"
    assert robot.arm_joint_names == [
        'shoulder_pan_joint', 'shoulder_lift_joint', 'upperarm_roll_joint',
        'elbow_flex_joint', 'forearm_roll_joint', 'wrist_flex_joint',
        'wrist_roll_joint', 'l_gripper_finger_joint', 'r_gripper_finger_joint'
    ]
    assert np.allclose(robot.action_space.low, robot.joint_lower_limits)
    assert np.allclose(robot.action_space.high, robot.joint_upper_limits)
    # The robot arm is 7 DOF and the left and right fingers are appended last.
    assert robot.left_finger_joint_idx == 7
    assert robot.right_finger_joint_idx == 8

    robot_state = np.array(ee_home_position + tuple(ee_orn) +
                           (robot.open_fingers, ),
                           dtype=np.float32)
    robot.reset_state(robot_state)
    recovered_state = robot.get_state()
    assert np.allclose(robot_state[:3], recovered_state[:3], atol=1e-3)
    assert np.isclose(robot_state[-1], recovered_state[-1], atol=1e-3)
    assert np.allclose(robot.get_joints(),
                       robot.initial_joint_positions,
                       atol=1e-2)

    ee_delta = (-0.01, 0.0, 0.01)
    ee_target_position = np.add(ee_home_position, ee_delta)
    ee_target = Pose(ee_target_position, ee_orn)
    joint_target = robot.inverse_kinematics(ee_target, validate=False)
    f_value = 0.03
    joint_target[robot.left_finger_joint_idx] = f_value
    joint_target[robot.right_finger_joint_idx] = f_value
    action_arr = np.array(joint_target, dtype=np.float32)

    # Not a valid control mode.
    utils.reset_config({"pybullet_control_mode": "not a real control mode"})
    with pytest.raises(NotImplementedError) as e:
        robot.set_motors(action_arr)
    assert "Unrecognized pybullet_control_mode" in str(e)

    # Reset control mode.
    utils.reset_config({"pybullet_control_mode": "reset"})
    robot.set_motors(action_arr)  # just make sure it doesn't crash

    # Position control mode.
    utils.reset_config({"pybullet_control_mode": "position"})
    robot.set_motors(action_arr)
    for _ in range(CFG.pybullet_sim_steps_per_action):
        p.stepSimulation(physicsClientId=physics_client_id)
    recovered_ee_pos = robot.get_state()[:3]

    # IK is currently not precise enough to increase this tolerance.
    assert np.allclose(ee_target_position, recovered_ee_pos, atol=1e-2)
    # Test forward kinematics.
    fk_result = robot.forward_kinematics(action_arr)
    assert np.allclose(fk_result.position, ee_target_position, atol=1e-2)

    # Check link_from_name
    assert robot.link_from_name("gripper_link")
    with pytest.raises(ValueError):
        robot.link_from_name("non_existent_link")


def test_create_single_arm_pybullet_robot(physics_client_id):
    """Tests for create_single_arm_pybullet_robot()."""
    physics_client_id = p.connect(p.DIRECT)

    # Fetch
    robot = create_single_arm_pybullet_robot("fetch", physics_client_id)
    assert isinstance(robot, FetchPyBulletRobot)
    assert robot.tool_link_name == "gripper_link"

    # Panda
    robot = create_single_arm_pybullet_robot("panda", physics_client_id)
    assert isinstance(robot, PandaPyBulletRobot)

    # Unknown robot
    with pytest.raises(NotImplementedError) as e:
        create_single_arm_pybullet_robot("not a real robot", physics_client_id)
    assert "Unrecognized robot name" in str(e)
