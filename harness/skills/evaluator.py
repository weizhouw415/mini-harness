"""Run a trusted environment-specific evaluator without executing skill documents."""

import asyncio
from collections.abc import Awaitable, Callable

from .loader import EVALUATION_LEASE_SECONDS, SkillError, SkillStore


async def evaluate_skill(
    store: SkillStore,
    name: str,
    version: str,
    environment: str,
    evaluator: Callable[[dict, str], Awaitable[dict]],
) -> dict:
    """Evaluate a candidate using a caller-supplied isolated workflow."""
    skill, token = store.start_evaluation(name, version, environment)
    try:
        result = await asyncio.wait_for(evaluator(skill, environment), EVALUATION_LEASE_SECONDS - 1)
        return store.finish_evaluation(name, version, environment, token, result)
    except asyncio.CancelledError:
        store.finish_evaluation(name, version, environment, token, {"passed": False, "error": "cancelled"})
        raise
    except (SkillError, TimeoutError) as exc:
        error = "invalid evaluator result" if isinstance(exc, SkillError) else "evaluation timed out"
        return store.finish_evaluation(name, version, environment, token, {"passed": False, "error": error})
    except Exception as exc:
        return store.finish_evaluation(
            name, version, environment, token, {"passed": False, "error": type(exc).__name__}
        )
