import unittest

from aether_worker.planning import accept_actor_decision, compile_plan, schedule_ready


def chain(count: int) -> list[dict]:
    nodes = []
    for index in range(count):
        dependencies = [] if index == 0 else [f"n{index - 1}"]
        nodes.append({"node_id": f"n{index}", "role": "research", "objective": f"step {index}", "dependencies": dependencies})
    return nodes


class PlanningTests(unittest.TestCase):
    def test_32_node_chain_is_accepted(self) -> None:
        order = compile_plan(chain(32))
        self.assertEqual(len(order), 32)
        self.assertEqual(order[0], "n0")
        self.assertEqual(order[-1], "n31")

    def test_33_nodes_and_cycles_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            compile_plan(chain(33))
        with self.assertRaises(ValueError):
            compile_plan([
                {"node_id": "a", "role": "research", "objective": "a", "dependencies": ["b"]},
                {"node_id": "b", "role": "research", "objective": "b", "dependencies": ["a"]},
            ])

    def test_explicit_depth_3_is_still_rejected(self) -> None:
        with self.assertRaises(ValueError):
            compile_plan(chain(4), max_depth=3)

    def test_reads_are_capped_at_two_and_writes_are_serial(self) -> None:
        nodes = [
            {"node_id": "r1", "role": "research", "objective": "r1", "dependencies": []},
            {"node_id": "r2", "role": "research", "objective": "r2", "dependencies": []},
            {"node_id": "r3", "role": "research", "objective": "r3", "dependencies": []},
        ]
        self.assertEqual(schedule_ready(nodes, set(), set()), ["r1", "r2"])
        writes = [
            {"node_id": "c1", "role": "coding", "objective": "c1", "dependencies": [], "resource_ids": ["repo"]},
            {"node_id": "c2", "role": "coding", "objective": "c2", "dependencies": [], "resource_ids": ["repo"]},
        ]
        self.assertEqual(schedule_ready(writes, set(), set()), ["c1"])

    def test_actor_cannot_mark_success(self) -> None:
        with self.assertRaises(ValueError):
            accept_actor_decision({"kind": "mark_success"})


if __name__ == "__main__":
    unittest.main()
