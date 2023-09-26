"""Base class for an explorer."""

import abc
import itertools
import logging
from typing import List, Set

import numpy as np
from gym.spaces import Box

from predicators.settings import CFG
from predicators.structs import Action, ExplorationStrategy, Object, \
    ParameterizedOption, Predicate, State, Task, Type

_RNG_COUNT = itertools.count()  # make sure RNG changes per instantiation


class BaseExplorer(abc.ABC):
    """Creates a policy and termination function for exploring in a task.

    The explorer is created at the beginning of every interaction cycle
    with the latest predicates and options.
    """

    def __init__(self, predicates: Set[Predicate],
                 options: Set[ParameterizedOption], types: Set[Type],
                 action_space: Box, train_tasks: List[Task],
                 max_steps_before_termination: int) -> None:
        self._predicates = predicates
        self._options = options
        self._types = types
        self._action_space = action_space
        self._train_tasks = train_tasks
        self._max_steps_before_termination = max_steps_before_termination
        self._set_seed(CFG.seed)

    @classmethod
    @abc.abstractmethod
    def get_name(cls) -> str:
        """Get the unique name of this explorer."""
        raise NotImplementedError("Override me!")

    def get_exploration_strategy(
        self,
        train_task_idx: int,
        timeout: int,
    ) -> ExplorationStrategy:
        """Wrap the base exploration strategy."""

        policy, termination_fn = self._get_exploration_strategy(
            train_task_idx, timeout)

        # Add wrapper for spot environments. Note that there is unfortunately
        # a lot of shared code between this and the spot wrapper approach,
        # but there are a few detailed differences that make refactoring not
        # easy, so I'm punting it for now.
        need_stow = False

        def wrapped_policy(state: State) -> Action:  # pragma: no cover
            nonlocal need_stow

            if "spot" not in CFG.env:
                return policy(state)

            # If some objects are lost, find them.
            lost_objects: Set[Object] = set()
            for obj in state:
                if "lost" in obj.type.feature_names and \
                    state.get(obj, "lost") > 0.5:
                    lost_objects.add(obj)
            # Need to find the objects.
            if lost_objects:
                logging.info(
                    f"[Explorer Spot Wrapper] Lost objects: {lost_objects}")
                # Reset the base approach policy.
                need_stow = True
                raise NotImplementedError("Coming soon!")
            # Found the objects. Stow the arm before replanning.
            if need_stow:
                logging.info(
                    "[Explorer Spot Wrapper] Lost objects found, stowing.")
                need_stow = False
                raise NotImplementedError("Coming soon!")
            # Give control back to base policy.
            logging.info(
                "[Explorer Spot Wrapper] Giving control to base policy.")
            return policy(state)

        # Terminate after the given number of steps.
        remaining_steps = self._max_steps_before_termination

        def wrapped_termination_fn(state: State) -> bool:
            nonlocal remaining_steps
            if termination_fn(state):
                logging.info("[Base Explorer] terminating due to term fn")
                return True
            if remaining_steps <= 0:
                logging.info("[Base Explorer] terminating due to max steps")
                return True
            steps_taken = self._max_steps_before_termination - remaining_steps
            actual_remaining_steps = min(
                remaining_steps,
                CFG.max_num_steps_interaction_request - steps_taken)
            logging.info(
                "[Base Explorer] not yet terminating (remaining steps: "
                f"{actual_remaining_steps})")
            remaining_steps -= 1
            return False

        return wrapped_policy, wrapped_termination_fn

    @abc.abstractmethod
    def _get_exploration_strategy(
        self,
        train_task_idx: int,
        timeout: int,
    ) -> ExplorationStrategy:
        """Given a train task idx, create an ExplorationStrategy, which is a
        tuple of a policy and a termination function."""
        raise NotImplementedError("Override me!")

    def _set_seed(self, seed: int) -> None:
        """Reset seed and rng."""
        self._seed = seed
        self._rng = np.random.default_rng(self._seed + next(_RNG_COUNT))
