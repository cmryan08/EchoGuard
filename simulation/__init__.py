"""EchoGuard simulation mode: clearly synthetic CSI-like data (see generator.py).

Synthetic data must never be used as evidence of real-world accuracy.
"""

from .generator import (PARAM_RANGES, SCENARIOS, SIM_MAC, CSISimulator, SimParams, SimulationSource,
                        scenario_segments)

__all__ = ["PARAM_RANGES", "SCENARIOS", "SIM_MAC", "CSISimulator", "SimParams", "SimulationSource",
           "scenario_segments"]
