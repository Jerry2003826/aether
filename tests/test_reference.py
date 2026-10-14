"""Reference tests only. They are not integration, sandbox or live-model tests."""
import hashlib
import math
import unittest
from datetime import datetime, timedelta, timezone
from pydantic import ValidationError
from reference.models import *
from reference.core import *

NOW=datetime(2026,10,14,12,39,tzinfo=timezone.utc)
HASH='a'*64

def goal(**changes):
    data=dict(project_id='project-test',task_id='task-test',contract_version=1,
              goal='Produce a report from authorized sources',required_artifacts=['report.md'],
              criteria=[AcceptanceCriterion(criterion_id='report',template='artifact_exists',description='Report exists')],
              created_at=NOW)
    data.update(changes)
    return GoalContract(**data)

def intent(**changes):
    data=dict(project_id='project-test',task_id='task-test',intent_id='intent-1',
              tool_id='github.create_draft_pr',arguments={'title':'test','branch':'topic'},
              resource_id='repo-1',resource_version='base-1',payload_sha256=HASH,
              risk=RiskClass.R2,idempotency=IdempotencyClass.RECONCILABLE_WRITE,
              max_cost_micro_usd=0,capability_id='cap-1')
    data.update(changes)
    return OperationIntent(**data)

def grant(i=None,**changes):
    i=i or intent()
    data=dict(approval_id='approval-1',project_id=i.project_id,task_id=i.task_id,
              intent_digest=intent_digest(i),issued_to_owner='owner-1',expires_at=NOW+timedelta(minutes=5),decision='approved')
    data.update(changes)
    return ApprovalGrant(**data)

def metrics(**changes):
    data=dict(independent_task_families=50,success_improvement=.10,paired_ci_lower=.03,
              cost_ratio=1.02,new_safety_failures=0,mandatory_regressions=0,evaluator_report_sha256=HASH)
    data.update(changes)
    return PromotionMetrics(**data)

class ContractTests(unittest.TestCase):
    def test_goal_roundtrip(self):
        a=goal();self.assertEqual(GoalContract.model_validate_json(a.model_dump_json()),a)
    def test_goal_requires_mandatory_criterion(self):
        with self.assertRaises(ValidationError):goal(criteria=[AcceptanceCriterion(criterion_id='x',template='artifact_exists',description='x',mandatory=False)])
    def test_duplicate_criteria_rejected(self):
        c=AcceptanceCriterion(criterion_id='x',template='artifact_exists',description='x')
        with self.assertRaises(ValidationError):goal(criteria=[c,c])
    def test_suite_requires_hash(self):
        with self.assertRaises(ValidationError):AcceptanceCriterion(criterion_id='x',template='trusted_test_suite',description='x')
    def test_unknown_fields_rejected(self):
        with self.assertRaises(ValidationError):goal(mark_success=True)
    def test_unknown_decision_rejected(self):
        with self.assertRaises(ValidationError):Decision(kind='mark_success')
    def test_tool_decision_requires_calls(self):
        with self.assertRaises(ValidationError):Decision(kind='call_tools')
    def test_candidate_requires_artifact(self):
        with self.assertRaises(ValidationError):Decision(kind='submit_candidate')
    def test_negative_and_float_money_rejected(self):
        for value in [-1,2.5,True]:
            with self.subTest(value=value),self.assertRaises(ValidationError):BudgetSpec(max_cost_micro_usd=value)
    def test_naive_time_rejected(self):
        with self.assertRaises(ValidationError):goal(created_at=datetime(2026,1,1))
    def test_unknown_usage_not_zero(self):
        with self.assertRaises(ValidationError):ModelResult(request_id='r',output={},usage_status='unknown',input_tokens=None,output_tokens=None,actual_cost_micro_usd=0,provider_request_id=None)
    def test_known_usage_complete(self):
        with self.assertRaises(ValidationError):ModelResult(request_id='r',output={},usage_status='known',input_tokens=1,output_tokens=2,actual_cost_micro_usd=None,provider_request_id=None)

class PlanTests(unittest.TestCase):
    def make(self,edges):
        return Plan(nodes=[TaskNode(node_id=n,role='research',objective=n,dependencies=d) for n,d in edges])
    def test_topological_order(self):self.assertEqual(validate_plan(self.make([('b',['a']),('a',[])])),['a','b'])
    def test_cycle(self):
        with self.assertRaises(ValueError):validate_plan(self.make([('a',['b']),('b',['a'])]))
    def test_missing(self):
        with self.assertRaises(ValueError):validate_plan(self.make([('a',['x'])]))
    def test_duplicate_node(self):
        with self.assertRaises(ValueError):validate_plan(self.make([('a',[]),('a',[])]))
    def test_depth(self):
        with self.assertRaises(ValueError):validate_plan(self.make([('a',[]),('b',['a']),('c',['b']),('d',['c'])]), max_depth=3)
    def test_only_verified_dependencies_ready(self):
        p=self.make([('a',[]),('b',['a'])]);self.assertEqual(ready_nodes(p,set(),{'a'}),[]);self.assertEqual(ready_nodes(p,{'a'},set()),['b'])
    def test_terminal_state_cannot_resume(self):self.assertFalse(allowed_transition(TaskState.SUCCEEDED,TaskState.RUNNING))
    def test_actor_cannot_directly_succeed(self):self.assertFalse(allowed_transition(TaskState.RUNNING,TaskState.SUCCEEDED))

class ApprovalTests(unittest.TestCase):
    def test_correct_grant(self):self.assertTrue(approval_matches(intent(),grant(),NOW,'owner-1',set()))
    def test_changed_args(self):self.assertFalse(approval_matches(intent(arguments={'title':'changed'}),grant(),NOW,'owner-1',set()))
    def test_changed_payload(self):self.assertFalse(approval_matches(intent(payload_sha256='b'*64),grant(),NOW,'owner-1',set()))
    def test_changed_resource_version(self):self.assertFalse(approval_matches(intent(resource_version='base-2'),grant(),NOW,'owner-1',set()))
    def test_expiry(self):self.assertFalse(approval_matches(intent(),grant(expires_at=NOW),NOW,'owner-1',set()))
    def test_replay(self):self.assertFalse(approval_matches(intent(),grant(),NOW,'owner-1',{'approval-1'}))
    def test_other_owner(self):self.assertFalse(approval_matches(intent(),grant(),NOW,'owner-2',set()))
    def test_other_project(self):self.assertFalse(approval_matches(intent(project_id='other'),grant(),NOW,'owner-1',set()))
    def test_unknown_non_idempotent_never_retries(self):self.assertEqual(recovery_action(OperationState.UNKNOWN,IdempotencyClass.NON_IDEMPOTENT),'manual_review_no_automatic_retry')
    def test_reconcilable_does_not_blind_retry(self):self.assertEqual(recovery_action(OperationState.DISPATCHED,IdempotencyClass.RECONCILABLE_WRITE),'reconcile_before_any_retry')
    def test_confirmed_returns_existing(self):self.assertEqual(recovery_action(OperationState.CONFIRMED,IdempotencyClass.NON_IDEMPOTENT),'return_existing')

class BudgetTests(unittest.TestCase):
    def test_reserve_cap(self):
        b=BudgetLedger(100);self.assertTrue(b.reserve('a',80));self.assertFalse(b.reserve('b',30));self.assertEqual(b.available,20)
    def test_reserve_duplicate_not_double_held(self):
        b=BudgetLedger(100);b.reserve('a',80);b.reserve('a',80);self.assertEqual(b.available,20)
    def test_changed_reservation_rejected(self):
        b=BudgetLedger(100);b.reserve('a',80)
        with self.assertRaises(ValueError):b.reserve('a',70)
    def test_unknown_holds_budget(self):
        b=BudgetLedger(100);b.reserve('a',80);b.mark_unknown('a');self.assertEqual(b.available,20)
        with self.assertRaises(ValueError):b.release_proven_not_sent('a',proof_not_sent=True)
    def test_no_release_without_proof(self):
        b=BudgetLedger(100);b.reserve('a',80)
        with self.assertRaises(ValueError):b.release_proven_not_sent('a',proof_not_sent=False)
    def test_settle_exactly_once(self):
        b=BudgetLedger(100);b.reserve('a',80);b.settle('a',50);b.settle('a',50);self.assertEqual(b.spent,50);self.assertEqual(b.available,50)
    def test_conflicting_settlement_rejected(self):
        b=BudgetLedger(100);b.reserve('a',80);b.settle('a',50)
        with self.assertRaises(ValueError):b.settle('a',51)
    def test_record_actual_overrun(self):
        b=BudgetLedger(100);b.reserve('a',80);b.settle('a',120);self.assertEqual(b.spent,120);self.assertFalse(b.reserve('b',1))

class RetrievalTests(unittest.TestCase):
    def test_rrf_matches_formula(self):
        scores=dict(reciprocal_rank_fusion([['a','b'],['b','c']]));self.assertAlmostEqual(scores['b'],1/62+1/61)
    def test_rrf_duplicate_not_overweighted(self):self.assertEqual(reciprocal_rank_fusion([['a','a','b']]),reciprocal_rank_fusion([['a','b']]))
    def test_rrf_empty(self):self.assertEqual(reciprocal_rank_fusion([]),[])
    def test_citation_unicode(self):
        t='中文🙂 evidence';c=Citation(source_version_id='v',source_sha256=hashlib.sha256(t.encode()).hexdigest(),start=2,end=3,quote='🙂');self.assertTrue(citation_matches(t,c))
    def test_citation_wrong_hash_rejected(self):
        c=Citation(source_version_id='v',source_sha256=HASH,start=0,end=2,quote='hi');self.assertFalse(citation_matches('hi',c))
    def test_citation_wrong_quote_rejected(self):
        t='hello';c=Citation(source_version_id='v',source_sha256=hashlib.sha256(t.encode()).hexdigest(),start=0,end=2,quote='xx');self.assertFalse(citation_matches(t,c))
    def test_propensities_sum_to_one(self):
        ids=['a','b','c'];p=[epsilon_greedy_probability(x,'a',ids,.05) for x in ids];self.assertAlmostEqual(sum(p),1);self.assertAlmostEqual(p[0],.95+.05/3)
    def test_zero_exploration(self):self.assertEqual(epsilon_greedy_probability('b','a',['a','b'],0),0)
    def test_paths_reject_escape(self):
        for path in ['/etc/passwd','../secret','a/../../b',r'..\x','C:/x','a\x00b'.replace('\\x00','\x00')]:
            with self.subTest(path=path),self.assertRaises(ValueError):validate_relative_export_path(path)
    def test_relative_export_path(self):self.assertEqual(validate_relative_export_path('src/file.py'),'src/file.py')

class VerificationTests(unittest.TestCase):
    def result(self,c,**changes):
        data=dict(criterion_id='report',verdict='PASS',artifact_sha256=HASH,contract_sha256=contract_digest(c),verifier_identity='verifier-1',evidence_ids=['evidence-1'],reason='verified',checked_at=NOW);data.update(changes);return VerificationResult(**data)
    def test_trusted_current_evidence(self):
        c=goal();self.assertTrue(verified_completion(c,HASH,[self.result(c)],{'evidence-1':'verifier-1'}))
    def test_old_artifact_no_success(self):
        c=goal();self.assertFalse(verified_completion(c,'b'*64,[self.result(c)],{'evidence-1':'verifier-1'}))
    def test_old_contract_no_success(self):
        c=goal();self.assertFalse(verified_completion(goal(contract_version=2),HASH,[self.result(c)],{'evidence-1':'verifier-1'}))
    def test_untrusted_origin_no_success(self):
        c=goal();self.assertFalse(verified_completion(c,HASH,[self.result(c,verifier_identity='actor')],{'evidence-1':'verifier-1'}))
    def test_missing_or_ambiguous_evidence_no_success(self):
        c=goal();r=self.result(c);self.assertFalse(verified_completion(c,HASH,[],{}));self.assertFalse(verified_completion(c,HASH,[r,r],{'evidence-1':'verifier-1'}))
    def test_unverifiable_no_success(self):
        c=goal();self.assertFalse(verified_completion(c,HASH,[self.result(c,verdict='UNVERIFIABLE')],{'evidence-1':'verifier-1'}))
    def test_hash_canonicalizes_key_order(self):self.assertEqual(canonical_sha256({'a':1,'b':2}),canonical_sha256({'b':2,'a':1}))
    def test_hash_rejects_nan(self):
        with self.assertRaises(ValueError):canonical_sha256({'a':math.nan})

class LearningTests(unittest.TestCase):
    def test_good_skill_only_eligible(self):self.assertEqual(promotion_gate(metrics()),'ELIGIBLE_NOT_DEPLOYED')
    def test_small_sample_not_promoted(self):self.assertEqual(promotion_gate(metrics(independent_task_families=10)),'INSUFFICIENT_EVIDENCE')
    def test_uncertain_effect_not_promoted(self):self.assertEqual(promotion_gate(metrics(paired_ci_lower=0)),'INSUFFICIENT_EVIDENCE')
    def test_missing_cost_not_promoted(self):self.assertEqual(promotion_gate(metrics(cost_ratio=None)),'INSUFFICIENT_EVIDENCE')
    def test_safety_failure_rejected(self):self.assertEqual(promotion_gate(metrics(new_safety_failures=1)),'REJECTED')
    def test_expensive_skill_rejected(self):self.assertEqual(promotion_gate(metrics(cost_ratio=1.2)),'REJECTED')
    def test_mandatory_regression_rejected(self):self.assertEqual(promotion_gate(metrics(mandatory_regressions=1)),'REJECTED')

if __name__=='__main__':unittest.main()
