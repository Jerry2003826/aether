"""Reference/pack checks, not live application integration tests."""
import unittest
from pathlib import Path
import yaml
from pydantic import ValidationError
from reference.models import Plan,TaskNode
from reference.core import validate_plan,ready_nodes
from scripts.verify_freeze import verify,graph_order
ROOT=Path(__file__).resolve().parents[1]

def chain(count:int)->Plan:
    return Plan(nodes=[TaskNode(node_id=f'n{i:02d}',role='research',objective=f'Step {i}',dependencies=[f'n{i-1:02d}'] if i else []) for i in range(count)])

class LongHorizonReferenceTests(unittest.TestCase):
    def test_default_eight_step_plan_is_valid(self):
        self.assertEqual(validate_plan(chain(8)),[f'n{i:02d}' for i in range(8)])
    def test_default_32_step_plan_is_valid(self):
        self.assertEqual(len(validate_plan(chain(32))),32)
    def test_33_nodes_is_rejected(self):
        with self.assertRaises(ValidationError):chain(33)
    def test_explicit_small_depth_is_still_enforced(self):
        with self.assertRaises(ValueError):validate_plan(chain(4),max_depth=3)
    def test_cycle_still_rejected(self):
        p=chain(8); nodes=list(p.nodes);nodes[0]=nodes[0].model_copy(update={'dependencies':['n07']})
        with self.assertRaises(ValueError):validate_plan(Plan(nodes=nodes))
    def test_only_next_verified_dependency_is_ready(self):
        self.assertEqual(ready_nodes(chain(8),{'n00','n01','n02','n03'},set()),['n04'])
    def test_running_parent_does_not_release_child(self):
        self.assertEqual(ready_nodes(chain(8),{'n00'}, {'n01'}),[])
    def test_depth_configuration_consistent(self):
        cfg=yaml.safe_load((ROOT/'config/policy.example.yaml').read_text())
        self.assertEqual(cfg['limits']['max_depth'],32)

class PackChecks(unittest.TestCase):
    def test_dependency_checker_rejects_cycle(self):
        with self.assertRaises(ValueError):graph_order([{'id':'a','depends_on':['b']},{'id':'b','depends_on':['a']}])
    def test_dependency_checker_rejects_missing(self):
        with self.assertRaises(ValueError):graph_order([{'id':'a','depends_on':['x']}])
    def test_dependency_checker_rejects_duplicate(self):
        with self.assertRaises(ValueError):graph_order([{'id':'a','depends_on':[]},{'id':'a','depends_on':[]}])
    def test_full_spec_consistency(self):
        self.assertEqual(verify(ROOT)['result'],'passed')

if __name__=='__main__':unittest.main()
