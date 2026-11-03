"""Exercise the real scanner loops without importing or starting the bot.

Only Python's standard library is needed. RPC, persistence and scheduling are
in-memory fakes; no Telegram, Web3 dependency, network or state file is used.
Run: python3 -m unittest discover -s tests -v
"""
import ast
import asyncio
import copy
from pathlib import Path
from types import SimpleNamespace
import unittest


class StopObservation(BaseException):
    pass


SOURCE = Path(__file__).resolve().parents[1] / "bot.py"
FUNCTIONS = {
    node.name: node
    for node in ast.parse(SOURCE.read_text()).body
    if isinstance(node, ast.AsyncFunctionDef)
    and node.name in ("new_contract_loop", "poll_loop")
}


async def observe(name, initial_cursor, heads, sleep_limit):
    key = "last_block_new_contracts" if name == "new_contract_loop" else "last_block"
    state = {} if initial_cursor is None else {key: initial_cursor}
    saves, blocks, ranges, sleeps = [], [], [], []
    readings = iter(heads)

    class Eth:
        @property
        def block_number(self):
            value = next(readings)
            if isinstance(value, Exception):
                raise value
            return value

        def get_block(self, number, full_transactions):
            if full_transactions is not True:
                raise AssertionError("full transactions required")
            blocks.append(number)
            return {"transactions": []}

    async def to_thread(function, *args, **kwargs):
        return function(*args, **kwargs)

    async def sleep(delay):
        sleeps.append(delay)
        if len(sleeps) >= sleep_limit:
            raise StopObservation()

    def fetch_logs(start, end, _addresses):
        ranges.append([start, end])
        return []

    env = {
        "Application": object,
        "asyncio": SimpleNamespace(to_thread=to_thread, sleep=sleep),
        "w3": SimpleNamespace(eth=Eth()),
        "load_state": lambda: state,
        "save_state": lambda value: saves.append(copy.deepcopy(value)),
        "log": SimpleNamespace(exception=lambda *_: None, info=lambda *_: None),
        "NEW_CONTRACT_BLOCK_SPAN": 2,
        "NEW_CONTRACT_POLL_INTERVAL": 7,
        "MAX_BLOCK_SPAN": 2,
        "POLL_INTERVAL": 11,
        "load_contracts": lambda: {},
        "fetch_logs": fetch_logs,
    }
    module = ast.fix_missing_locations(
        ast.Module(body=[FUNCTIONS[name]], type_ignores=[])
    )
    exec(compile(module, str(SOURCE), "exec"), env)
    try:
        await env[name](object())
    except StopObservation:
        pass
    return key, state, saves, blocks, ranges, sleeps


class StartupCursorTests(unittest.TestCase):
    def check_scenario(self, name, scenario):
        failure = ConnectionError("synthetic startup failure")
        scenarios = {
            "healthy_fresh": (None, [10, 10], 1, 10, [], 1),
            "first_read_fails": (None, [failure, 10, 10], 2, 10, [], 1),
            "repeated_failures": (None, [failure, failure, 10, 10], 3, 10, [], 1),
            "existing_cursor": (8, [10], 1, 10, [9, 10], 1),
            "explicit_zero_cursor": (0, [2], 1, 2, [1, 2], 1),
            "follow_new_block": (None, [10, 11], 1, 11, [11], 2),
            "genesis_is_valid": (None, [0, 1], 1, 1, [1], 2),
            "unavailable_startup": (None, [failure, failure, failure], 3, None, [], 0),
        }
        initial, heads, limit, expected_cursor, expected_blocks, expected_saves = scenarios[scenario]
        key, state, saves, blocks, ranges, sleeps = asyncio.run(
            observe(name, initial, heads, limit)
        )
        self.assertEqual(state.get(key), expected_cursor)
        self.assertEqual(len(saves), expected_saves)
        if initial is None and expected_cursor is not None:
            self.assertEqual(saves[0][key], next(x for x in heads if isinstance(x, int)))
        if name == "new_contract_loop":
            self.assertEqual(blocks, expected_blocks)
            self.assertEqual(ranges, [])
            self.assertEqual(sleeps, [7] * limit)
        else:
            expected_ranges = [] if not expected_blocks else [[expected_blocks[0], expected_blocks[-1]]]
            self.assertEqual(ranges, expected_ranges)
            self.assertEqual(blocks, [])
            self.assertEqual(sleeps, [11] * limit)


def make_test(name, scenario):
    def test(self):
        self.check_scenario(name, scenario)
    return test


for loop in ("new_contract_loop", "poll_loop"):
    for case in (
        "healthy_fresh", "first_read_fails", "repeated_failures", "existing_cursor",
        "explicit_zero_cursor", "follow_new_block", "genesis_is_valid", "unavailable_startup",
    ):
        setattr(StartupCursorTests, f"test_{loop}_{case}", make_test(loop, case))


if __name__ == "__main__":
    unittest.main()
