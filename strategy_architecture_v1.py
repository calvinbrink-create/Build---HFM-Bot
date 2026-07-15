"""Historical import name for the single active strategy engine.

The implementation is only in strategies.architecture. This compatibility
module contains no second evaluator.
"""
from strategies.architecture import *  # noqa: F401,F403
from strategies.architecture import __all__
