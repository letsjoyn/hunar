import json
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
with open(project_root / 'data' / 'deeds_111.json', 'r', encoding='utf-8') as f:
    deeds = json.load(f)

print(f"Total deeds: {len(deeds)}")
for i, d in enumerate(deeds, 1):
    c = d['code']
    b = d['book']
    cls = d['classification']
    dt = d['type']
    sd = d['sd']
    td = d['td']
    rf = d['rf']
    rf_min = d['rf_min']
    rf_max = d['rf_max']
    art = d['article']
    print(f"{i:3d}. {c} | Bk {b} | {cls[:16]:16} | {dt[:35]:35} | SD: {sd:12} | TD: {td:5} | RF: {rf:12} | {rf_min}/{rf_max} | Art: {art}")

