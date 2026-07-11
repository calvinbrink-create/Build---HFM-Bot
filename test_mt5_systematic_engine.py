#!/usr/bin/env python3
from test_mt5_replacement_pipeline import test_cost_policy, test_tick_engine_routing

if __name__ == "__main__":
    test_tick_engine_routing()
    test_cost_policy()
    print("PASS shared replacement execution policy")
