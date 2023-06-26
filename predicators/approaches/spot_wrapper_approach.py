"""An approach that wraps a base approach for a spot environment. Detects when
objects have been "lost" and executes a specific controller to "find" them.
Passes control back to the main approach once all objects are not lost.

Assumes that some objects in the environment have a feature called "lost" that
is 1.0 if the object is lost and 0.0 otherwise. This feature should be tracked
by a perceiver.

For now, the "find" policy is represented with a single action that is
extracted from the environment.
"""

import logging
from functools import lru_cache
from typing import Any, Callable, List, Optional, Set

from gym.spaces import Box

from predicators.approaches import BaseApproach, BaseApproachWrapper
from predicators.envs import get_or_create_env
from predicators.envs.spot_env import SpotEnv
from predicators.settings import CFG
from predicators.structs import Action, Object, ParameterizedOption, \
    Predicate, State, Task, Type


class SpotWrapperApproach(BaseApproachWrapper):
    """Always "find" if some object is lost."""

    def __init__(self, base_approach: BaseApproach,
                 initial_predicates: Set[Predicate],
                 initial_options: Set[ParameterizedOption], types: Set[Type],
                 action_space: Box, train_tasks: List[Task]) -> None:
        super().__init__(base_approach, initial_predicates, initial_options,
                         types, action_space, train_tasks)
        self._base_approach_has_control = False  # for execution monitoring

    @classmethod
    def get_name(cls) -> str:
        return "spot_wrapper"

    @property
    def is_learning_based(self) -> bool:
        return self._base_approach.is_learning_based

    def _solve(self, task: Task, timeout: int) -> Callable[[State], Action]:

        # Maintain policy from the base approach.
        base_approach_policy: Optional[Callable[[State], Action]] = None
        self._base_approach_has_control = False
        need_stow = False

        def _policy(state: State) -> Action:
            nonlocal base_approach_policy, need_stow
            # If some objects are lost, find them.
            lost_objects: Set[Object] = set()
            for obj in state:
                if "lost" in obj.type.feature_names and \
                    state.get(obj, "lost") > 0.5:
                    lost_objects.add(obj)
            # Need to find the objects.
            if lost_objects:
                logging.info(f"[Spot Wrapper] Lost objects: {lost_objects}")
                # Reset the base approach policy.
                base_approach_policy = None
                need_stow = True
                self._base_approach_has_control = False
                return self._get_special_action("find")
            # Found the objects. Stow the arm before replanning.
            if need_stow:
                logging.info("[Spot Wrapper] Lost objects found, stowing.")
                base_approach_policy = None
                need_stow = False
                self._base_approach_has_control = False
                return self._get_special_action("stow")
            # Check if we need to re-solve.
            if base_approach_policy is None:
                logging.info("[Spot Wrapper] Replanning with base approach.")
                cur_task = Task(state, task.goal)
                base_approach_policy = self._base_approach.solve(
                    cur_task, timeout)
                self._base_approach_has_control = True
                # Need to call this once here to fix off-by-one issue.
                atom_seq = self._base_approach.get_execution_monitoring_info()
                assert all(a.holds(state) for a in atom_seq[0])
            # Use the base policy.
            return base_approach_policy(state)

        return _policy

    @lru_cache(maxsize=None)
    def _get_special_action(self, action_name: str) -> Action:
        env = get_or_create_env(CFG.env)
        assert isinstance(env, SpotEnv)
        # In the future, may want to make this object-specific.
        return env.get_special_action(action_name)

    def get_execution_monitoring_info(self) -> List[Any]:
        if self._base_approach_has_control:
            return self._base_approach.get_execution_monitoring_info()
        return []
