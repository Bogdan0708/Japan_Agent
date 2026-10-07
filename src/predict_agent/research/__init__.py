"""Research layer (spec §6): Claude forecasts a market from its rules and the open web,
without the market price. This package never imports the policy, paper-trading or money
modules (enforced by tests/predict/test_cli_isolation.py); only `research.sdk` touches the
optional Claude Agent SDK, and only lazily."""
