"""Rebuild the reading copy from the authoritative docs. No network or app changes."""
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
CHAPTERS=['00_FREEZE.md','01_product.md','02_architecture.md','03_repository_map.md','04_execution.md','05_knowledge.md','06_security.md','07_learning_evaluation.md','08_api_ui.md','09_data.md','10_operations.md','11_delivery.md','12_integration_details.md','13_codegraph.md','14_BOUNDARIES_AND_FAILURES.md','16_MERGE_EXISTING.md','DECISIONS.md','SOURCES.md']
def render(root:Path=ROOT)->str:
    return '# AETHER V1.4 · 完整开工规格\n\n由本包docs章节合并的阅读版。维护时以docs章节为源，冻结裁决由docs/00_FREEZE.md优先。\n\n'+'\n\n---\n\n'.join((root/'docs'/n).read_text(encoding='utf-8') for n in CHAPTERS).strip()+'\n'
if __name__=='__main__':
    (ROOT/'MASTER_SPEC.md').write_text(render(),encoding='utf-8')
    print('Rebuilt MASTER_SPEC.md')
