"""定向补正（M8）测试。

场景：correction.csv 中 00010、00020 因缺检出限被隔离，
复检 00011 引用 00010 而悬空（REFERENCE）。补正只发来 00010 一行，
系统须把 00010 及其复检闭包 00011 一并重放，其余已确认结论冻结。
"""
import csv
import io
import pytest
from app.workflow import load, run
from app.errors import DomainError
from app.modules.m8_correction import service
from app.modules.m8_correction.rules import patch_source, _parse_records, _closure
from app.modules.m7_archive.export_out import bundle, checksum

PATCH_00010 = ('00010,S901,B901,苹果,P01,LC-MS/MS,1,2026-08-01,'
               '2026-08-02,<0.001,mg/kg,0.001,,缺检出限（补正目标）')
PATCH_00020 = ('00020,S902,B902,苹果,P01,LC-MS/MS,1,2026-08-01,'
               '2026-08-02,<0.001,mg/kg,0.001,,x')


@pytest.fixture
def base():
    return run(load('correction.csv'), {})


def test_base_scenario_matches_narrative(base):
    # 两行缺 LOD 被隔离，另有一条复检引用其中一行而悬空。
    codes = {(q['id'], q['code']) for q in base['quarantine']}
    assert ('00010', 'MISSING') in codes
    assert ('00020', 'MISSING') in codes
    assert ('00011', 'REFERENCE') in codes
    assert base['reconcile']['balanced'] is True


def test_patch_only_changes_target_bytes(base):
    import base64
    raw = base64.b64decode(base['source_b64'])
    patched, info = patch_source(raw, '00010', PATCH_00010)
    header, spans, text, _ = _parse_records(raw)
    target = next(s for s in spans if dict(zip(header, s[2])).get('id') == '00010')
    start, end = target[0], target[1]
    new_text = patched.decode('utf-8-sig')
    replacement = new_text[start:len(new_text) - (len(text) - end)]
    # 重组：前缀 + 替换段 + 后缀，与补丁结果完全一致，证明仅目标记录被改动。
    assert new_text == text[:start] + replacement + text[end:]
    assert replacement.strip().split(',')[0] == '00010'
    assert info['line'] == 5 and info['old']['lod'] == 'N/A' and info['new']['lod'] == '0.001'


def test_replay_resolves_target_and_dependent_retest_closure(base):
    cand = service.execute(base, '00010', PATCH_00010)
    assert set(cand['closure']) == {'00010', '00011'}
    # 悬空复检被救活并替代目标，判为 PASS（0.004 ≤ 0.005）。
    calc = next(c for c in cand['calculations'] if c['id'] == '00011')
    assert calc['code'] == 'PASS' and calc['replaces'] == '00010'
    assert not any(q['id'] == '00011' and q['code'] == 'REFERENCE' for q in cand['quarantine'])


def test_outside_closure_conclusions_are_frozen(base):
    before = {c['id']: c for c in base['calculations']}
    cand = service.execute(base, '00010', PATCH_00010)
    for cid, calc in before.items():
        assert next(c for c in cand['calculations'] if c['id'] == cid) == calc
    # 另一行缺 LOD 不在闭包内，隔离原样保留。
    assert any(q['id'] == '00020' and q['code'] == 'MISSING' for q in cand['quarantine'])


def test_replay_does_not_add_projects_or_change_input_totals(base):
    cand = service.execute(base, '00010', PATCH_00010)
    for key in ('input_count', 'blank_count', 'header_count', 'duplicate_count'):
        assert cand[key] == base[key]
    # 仅闭包内两个项目进入判定，业务总数从 2 增至 3（目标被复检替代只计一项）。
    assert cand['business']['total'] == base['business']['total'] + 1
    assert cand['reconcile']['balanced'] is True


def test_batch_grade_only_derived_from_corrected_evidence(base):
    cand = service.execute(base, '00010', PATCH_00010)
    grades = {g['batch']: g['code'] for g in cand['reconcile']['grades']}
    # 已签的 B001 结论不变；新闭环 B901 依同一份补正证据判 PASS。
    assert grades['B001'] == 'FAIL' and grades['B901'] == 'PASS'


def test_frozen_identity_fields_cannot_be_altered(base):
    tampered = PATCH_00010.replace('S901', 'S999')
    with pytest.raises(DomainError) as e:
        service.execute(base, '00010', tampered)
    assert e.value.code == 'SCOPE'


def test_correction_target_must_be_quarantined(base):
    not_quarantined = PATCH_00010.replace('00010', '00001').replace('S901', 'S001').replace('B901', 'B001')
    with pytest.raises(DomainError) as e:
        service.execute(base, '00001', not_quarantined)
    assert e.value.code == 'CORRECTION'


def test_unknown_target_rejected(base):
    with pytest.raises(DomainError) as e:
        service.execute(base, '99999', PATCH_00010.replace('00010', '99999'))
    assert e.value.code == 'CORRECTION'


def test_failed_correction_leaves_base_intact_and_returns_error(base):
    # LOD 给 0：重放后目标仍无法形成有效结论，整笔补正失败。
    bad = PATCH_00010.replace('<0.001,mg/kg,0.001', '0.001,mg/kg,0')
    with pytest.raises(DomainError):
        service.execute(base, '00010', bad)
    # 失败不产生任何状态变更（service 纯函数，base 未被写库）。
    assert len(base['quarantine']) == 3


def test_repeated_replay_is_idempotent_and_scope_stable(base):
    a = service.execute(base, '00010', PATCH_00010)
    b = service.execute(base, '00010', PATCH_00010)
    assert a['run_id'] == b['run_id']
    assert a['calculations'] == b['calculations']
    assert a['business'] == b['business']


def test_correction_ledger_and_original_quarantine_are_archived(base):
    cand = service.execute(base, '00010', PATCH_00010)
    entry = cand['corrections'][0]
    assert entry['target_id'] == '00010'
    assert entry['reason_codes'] == ['MISSING']
    assert entry['changed_fields'] == ['result', 'lod']
    assert entry['old']['lod'] == 'N/A' and entry['new']['lod'] == '0.001'
    text = bundle(cand)
    assert '补正' in text and '补正链' in text
    # 原始隔离原因（00020 仍隔离）与补正记录都进同一份归档证据。
    assert '00020' in text and 'MISSING' in text
    assert checksum(cand) == cand['archive']['sha256']
    assert cand['archive']['ready'] is True


def test_dict_replacement_equivalent_to_csv_line(base):
    import base64
    from app.modules.m1_ingest.decode import decode
    raw = base64.b64decode(base['source_b64'])
    header = next(csv.reader(io.StringIO(decode(raw)[0])))
    row = dict(zip(header, next(csv.reader(io.StringIO(PATCH_00010)))))
    cand = service.execute(base, '00010', row)
    assert cand['closure'] == ['00010', '00011']


def test_closure_helpers_follow_indirect_relations():
    measurements = [
        {'id': 'A', 'relation': ''},
        {'id': 'B', 'relation': 'A'},
        {'id': 'C', 'relation': 'B'},
        {'id': 'D', 'relation': ''},
    ]
    assert _closure('A', measurements) == {'A', 'B', 'C'}
    assert _closure('D', measurements) == {'D'}


def test_multilevel_retest_chain_replays_to_chain_head():
    header = ('id,sample_id,batch,product,analyte,method,version,sampled,'
              'reported,result,unit,lod,relation,note')
    rows = [
        'R1,S1,B1,苹果,P01,LC-MS/MS,1,2026-08-01,2026-08-02,0.01,mg/kg,N/A,,根缺LOD',
        'R2,S1,B1,苹果,P01,LC-MS/MS,2,2026-08-01,2026-08-03,0.004,mg/kg,0.001,R1,一级复检',
        'R3,S1,B1,苹果,P01,LC-MS/MS,3,2026-08-01,2026-08-04,0.003,mg/kg,0.001,R2,二级复检',
    ]
    base = run(('\n'.join([header] + rows) + '\n').encode(), {})
    assert {q['id'] for q in base['quarantine']} == {'R1', 'R2', 'R3'}
    patch = ('R1,S1,B1,苹果,P01,LC-MS/MS,1,2026-08-01,2026-08-02,'
             '<0.001,mg/kg,0.001,,根补正')
    cand = service.execute(base, 'R1', patch)
    # 整条依赖链进入闭包，最终采纳链顶 R3，无悬空残留。
    assert set(cand['closure']) == {'R1', 'R2', 'R3'}
    assert cand['quarantine'] == []
    assert [(c['id'], c['code'], c['replaces']) for c in cand['calculations']] == [('R3', 'PASS', 'R2')]
    assert cand['reconcile']['balanced'] is True


def test_invalid_correction_that_yields_no_conclusion_is_rejected(base):
    bad = PATCH_00010.replace('<0.001,mg/kg,0.001', '0.001,mg/kg,0')
    with pytest.raises(DomainError) as e:
        service.execute(base, '00010', bad)
    assert e.value.code == 'REPLAY'


# ---------- HTTP / 存储层 ----------

def test_http_apply_preview_idempotent_and_rollback(client):
    base = client.post('/api/runs', json={'sample': 'correction.csv'}).get_json()
    # 预演不落盘。
    preview = client.post('/api/m8_correction/preview',
                          json={'target_id': '00010', 'csv': PATCH_00010}).get_json()
    assert set(preview['closure']) == {'00010', '00011'}
    assert client.get('/api/runs').get_json()[0]['id'] == base['run_id']
    # 提交产生新批次，latest 切换，旧批次保留。
    new = client.post('/api/m8_correction/apply',
                      json={'target_id': '00010', 'csv': PATCH_00010}).get_json()
    assert client.get('/api/runs').get_json()[0]['id'] == new['run_id']
    assert any(h['id'] == base['run_id'] for h in client.get('/api/runs').get_json())
    # 再次重跑同一份补正：幂等，不新增批次（应用启动自带 dirty 批次，故只核对本场景两个批次都在且无第三个补正批次）。
    again = client.post('/api/m8_correction/apply',
                        json={'target_id': '00010', 'csv': PATCH_00010}).get_json()
    assert again['run_id'] == new['run_id']
    ids = [h['id'] for h in client.get('/api/runs').get_json()]
    assert ids.count(new['run_id']) == 1 and base['run_id'] in ids


def test_http_failed_correction_keeps_old_result(client):
    client.post('/api/runs', json={'sample': 'correction.csv'})
    latest_before = client.get('/api/runs').get_json()[0]['id']
    tampered = PATCH_00020.replace('S902', 'S777')
    r = client.post('/api/m8_correction/apply', json={'target_id': '00020', 'csv': tampered})
    assert r.status_code == 422 and r.get_json()['code'] == 'SCOPE'
    assert client.get('/api/runs').get_json()[0]['id'] == latest_before


def test_http_chained_corrections_carry_full_ledger(client):
    client.post('/api/runs', json={'sample': 'correction.csv'})
    r1 = client.post('/api/m8_correction/apply',
                     json={'target_id': '00010', 'csv': PATCH_00010}).get_json()
    r2 = client.post('/api/m8_correction/apply',
                     json={'target_id': '00020', 'csv': PATCH_00020}).get_json()
    assert r2['base_run_id'] == r1['run_id']
    assert [x['target_id'] for x in r2['corrections']] == ['00010', '00020']
    assert r2['quarantine'] == [] and r2['business']['total'] == 4
    # 中间批次与原始批次都仍可取回，结论未被改写。
    mid = client.get('/api/runs/' + r1['run_id']).get_json()
    assert [q['id'] for q in mid['quarantine']] == ['00020']


def test_m8_page_and_targets(client):
    client.post('/api/runs', json={'sample': 'correction.csv'})
    assert client.get('/m8_correction').status_code == 200
    targets = client.get('/api/m8_correction/targets').get_json()['targets']
    assert {t['id'] for t in targets} == {'00010', '00011', '00020'}


def test_archive_export_contains_correction_evidence(client):
    client.post('/api/runs', json={'sample': 'correction.csv'})
    new = client.post('/api/m8_correction/apply',
                      json={'target_id': '00010', 'csv': PATCH_00010}).get_json()
    export = client.get('/api/archive/' + new['run_id'] + '/export.csv')
    text = export.data.decode('utf-8-sig')
    assert export.status_code == 200
    rows = list(csv.reader(io.StringIO(text)))
    kinds = {r[0] for r in rows}
    assert {'运行清单', '补正链', '对账', '计算', '隔离', '补正'} <= kinds
