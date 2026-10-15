"""Validate specification consistency only. Does not test the product or any upstream service."""
from __future__ import annotations
import argparse, json, sys, zipfile
from pathlib import Path
from typing import Any
import yaml
ROOT=Path(__file__).resolve().parents[1]
DEPENDENCY_PARTS={'.venv','node_modules','.git','.next','data','secrets','__pycache__','.cache','.hermes'}

def is_dependency_path(path:Path)->bool:
    return any(part in DEPENDENCY_PARTS for part in path.parts)

def read_yaml(path:Path)->Any:
    with path.open(encoding='utf-8') as f:return yaml.safe_load(f)

def require(condition:bool, message:str)->None:
    if not condition:raise ValueError(message)

def graph_order(tickets:list[dict])->list[str]:
    entries={t['id']:t for t in tickets}
    require(len(entries)==len(tickets),'Duplicate task ID')
    for t in tickets:
        require(set(t['depends_on']).issubset(entries),f"Unknown dependency for {t['id']}")
    ordered=[]; pending=set(entries)
    while pending:
        ready=sorted(t for t in pending if set(entries[t]['depends_on']).issubset(ordered))
        require(bool(ready),'Task dependency cycle')
        ordered.extend(ready);pending.difference_update(ready)
    return ordered

def verify(root:Path=ROOT)->dict[str,Any]:
    tasks=read_yaml(root/'tasks/backlog.yaml')['tickets']
    order=graph_order(tasks);byid={x['id']:x for x in tasks}
    for task in tasks:
        require(task['scope'] in {'core','selected_optional','live'},'Invalid task scope')
        require(task['status'] in {'not_started','in_progress','blocked','done'},'Invalid task status')
        require(bool(task['acceptance']) and bool(task['deliverables']),f"Empty task {task['id']}")
        for doc in task['read']:require((root/doc).is_file(),f'Missing task doc {doc}')
        if task['scope']=='core':
            require(all(byid[d]['scope']=='core' for d in task['depends_on']),f"Core depends on optional/live: {task['id']}")
    deferred=read_yaml(root/'tasks/deferred.yaml')
    require(deferred['auto_execute'] is False,'Deferred research may not auto-execute')
    known=set(byid)|{x['id'] for x in deferred['items']}
    mappings=read_yaml(root/'tasks/legacy_mapping.yaml'); mids=[x['old_id'] for x in mappings['items']]
    require(len(mids)==len(set(mids))==mappings['old_v12_count']+mappings['old_v13_count'],'Incomplete or duplicate legacy mappings')
    for m in mappings['items']:require(bool(m['targets']) and set(m['targets']).issubset(known),f"Bad legacy target {m['old_id']}")
    for milestone in range(12):
        for n in range(1,6):
            key=f'M{milestone:02d}-{n:02d}'
            require(any(m['old_id']==key and any(t.startswith('F') for t in m['targets']) for m in mappings['items']),f'Original core lost {key}')
    cases=[json.loads(line) for line in (root/'evals/dev_cases.jsonl').read_text().splitlines() if line.strip()]
    require(len(cases)==len(tasks) and {c['ticket_id'] for c in cases}==set(byid),'Development case/task coverage drift')
    for case in cases:
        task=byid[case['ticket_id']]
        require(case['assertions']==task['acceptance'],'Development acceptance drift')
        require(case['required_artifacts']==task['deliverables'],'Development artifacts drift')
        require(case['execution_scope']==task['scope'],'Development scope drift')
        require(case['kind']=='acceptance_specification_not_implemented_test','Cases must not claim implemented tests')
    api=read_yaml(root/'contracts/openapi.yaml');staged=read_yaml(root/'contracts/endpoint_stages.yaml')['paths']
    require(len(staged)==len(api['paths']), 'API path coverage length mismatch')
    require({x['path'] for x in staged}==set(api['paths']), 'API path mismatch')
    verbs={'get','post','put','patch','delete','head','options'}
    for row in staged:
        require(set(row['methods'])==set(api['paths'][row['path']])&verbs,'API verb mismatch')
        require(row['implemented_by'] in byid,'Unknown API task')
    policy=read_yaml(root/'config/policy.example.yaml');features=read_yaml(root/'config/feature_matrix.yaml')
    require(policy['limits']['max_depth']==features['limits']['max_dependency_depth']==32,'DAG depth configuration drift')
    require(features['limits']['max_recursive_delegation']==0,'Recursive delegation not frozen off')
    require(features['features']['memory']['selected']=='native_postgres','Default memory drift')
    require(features['features']['codegraph']['enabled'] is False,'CodeGraph auto enabled')
    require(features['features']['external_writes']['enabled'] is False,'External write enabled in example')
    require(features['features']['learning']['automatic_promotion'] is False,'Learning may not self-promote')
    cg=read_yaml(root/'config/codegraph.example.yaml')
    require(cg['product_enabled'] is False and cg['security']['telemetry_env']['CODEGRAPH_TELEMETRY']=='0','Unsafe CodeGraph example')
    require(json.loads((root/'config/upstream.lock.example.json').read_text())['is_actual_lock'] is False,'Example claims installed lock')
    # Parse all active structured artifacts and check internal JSON pointers.
    parsed=0
    for path in sorted(root.rglob('*')):
        if not path.is_file() or 'archive' in path.parts or is_dependency_path(path):continue
        if path.suffix=='.json':obj=json.loads(path.read_text());parsed+=1
        elif path.suffix in {'.yaml','.yml'}:obj=read_yaml(path);parsed+=1
        elif path.suffix=='.jsonl':
            for line in path.read_text().splitlines():
                if line.strip():json.loads(line)
            parsed+=1;continue
        else:continue
        def check_refs(node:Any)->None:
            if isinstance(node,dict):
                ref=node.get('$ref')
                if isinstance(ref,str) and ref.startswith('#/'):
                    cursor=obj
                    for part in ref[2:].split('/'):
                        part=part.replace('~1','/').replace('~0','~')
                        require(isinstance(cursor,dict) and part in cursor,f'Broken reference {ref} in {path.name}')
                        cursor=cursor[part]
                for child in node.values():check_refs(child)
            elif isinstance(node,list):
                for child in node:check_refs(child)
        check_refs(obj)
    archives=[]
    for path in sorted((root/'archive').glob('*.zip')):
        with zipfile.ZipFile(path) as z:
            require(z.testzip() is None, f'Corrupt archive {path.name}')
            for info in z.infolist():
                require(not info.filename.startswith('/') and '..' not in Path(info.filename).parts,'Unsafe archived path')
        archives.append(path.name)
    agents=[path for path in root.rglob('AGENTS.md') if not is_dependency_path(path)]
    require(agents==[root/'AGENTS.md'],'Competing active AGENTS.md files')
    from importlib.util import spec_from_file_location,module_from_spec
    spec=spec_from_file_location('spec_builder',root/'scripts/rebuild_spec.py')
    mod=module_from_spec(spec);spec.loader.exec_module(mod)
    require((root/'MASTER_SPEC.md').read_text()==mod.render(root),'MASTER_SPEC drift: run scripts/rebuild_spec.py')
    return {'validation':'specification_consistency_only','result':'passed','task_count':len(tasks),'core_task_count':sum(t['scope']=='core' for t in tasks),'legacy_mapping_count':len(mids),'api_paths':len(staged),'api_operations':sum(len(r['methods']) for r in staged),'parsed_structured_files':parsed,'archives_checked':archives,'topological_order':order,'product_integration':'not_run','live_models':'not_run','benchmark_superiority':'not_evaluated'}

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--write-report',action='store_true');args=parser.parse_args()
    try:
        result=verify();print(json.dumps(result,ensure_ascii=False,indent=2))
        if args.write_report:(ROOT/'reports/freeze-validation.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    except Exception as exc:
        print(f'FAIL: {exc}',file=sys.stderr);sys.exit(1)
