#!/usr/bin/env python3
from test_mt5_replacement_pipeline import test_broker_sizing_batch, test_fixed_controls_and_mql_defaults

if __name__ == "__main__":
    test_broker_sizing_batch()
    test_fixed_controls_and_mql_defaults()
    print("PASS three-engine broker sizing and controls")
