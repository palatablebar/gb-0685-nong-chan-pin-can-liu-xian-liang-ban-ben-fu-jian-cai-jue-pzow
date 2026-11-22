from flask import Blueprint, jsonify, request, render_template
from app.config import TITLE, MODULES
from app.errors import DomainError
from app import store
from app.modules.m8_correction import service
from app.modules.m8_correction.rules import FROZEN_FIELDS, PATCHABLE_FIELDS

bp = Blueprint('m8_correction', __name__)


def _target_rows(state):
    """可补正的隔离报告（缺检出限等可修复原因），附冻结/可改字段提示。"""
    seen = {}
    for q in state.get('quarantine', []):
        if q.get('id'):
            seen.setdefault(q['id'], []).append(q['code'])
    return [{'id': rid, 'reason_codes': sorted(codes)} for rid, codes in sorted(seen.items())]


@bp.get('/m8_correction')
def screen():
    latest = store.history()
    state = store.get(latest[0]['id']) if latest else None
    return render_template('m8_correction.html', title=TITLE, short='residuelimit',
                           modules=MODULES, current=8, data=state, run=state,
                           history=latest, targets=_target_rows(state) if state else [],
                           frozen=FROZEN_FIELDS, patchable=PATCHABLE_FIELDS)


@bp.get('/api/m8_correction/targets')
def targets():
    state = _latest_state()
    return jsonify({'run_id': state['run_id'], 'targets': _target_rows(state),
                    'corrections': state.get('corrections', [])})


@bp.post('/api/m8_correction/preview')
def preview():
    base, target_id, replacement, params = _request_parts()
    candidate = service.execute(base, target_id, replacement, params)
    return jsonify(_summary(base, candidate))


@bp.post('/api/m8_correction/apply')
def apply():
    base, target_id, replacement, params = _request_parts()
    # 幂等：同一基线 + 同一补正内容只产生一个批次，直接复用已存结果，
    # 不依赖 latest 指针（补正后 latest 已指向新批次）。
    existing = store.find_correction(base['run_id'], target_id, replacement)
    if existing:
        return jsonify(existing)
    candidate = service.execute(base, target_id, replacement, params)
    saved = store.save_correction(base['run_id'], candidate, target_id, replacement)
    return jsonify(saved)


def _latest_state():
    latest = store.history()
    if not latest:
        raise DomainError('NOT_FOUND', '暂无已确认批次可补正', 404)
    return store.get(latest[0]['id'])


def _resolve_base(run_id, target_id):
    """定位补正基线。

    显式 run_id 优先；否则取 latest。若 latest 已是补正批次而目标已被补正
    （不再处于隔离区），沿 base_run_id 回溯到原始基线，保证“再次重跑同一份
    补正”命中同一基线并幂等，而不是误判为无实质补正。
    """
    state = store.get(run_id) if run_id else _latest_state()
    quarantined = {q.get('id') for q in state.get('quarantine', [])}
    seen = set()
    while target_id not in quarantined and state.get('base_run_id') and state['run_id'] not in seen:
        seen.add(state['run_id'])
        state = store.get(state['base_run_id'])
        quarantined = {q.get('id') for q in state.get('quarantine', [])}
    return state


def _request_parts():
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        raise DomainError('PAYLOAD', '请求必须是对象')
    target_id = str(data.get('target_id', '')).strip()
    if not target_id:
        raise DomainError('CORRECTION', '请指定补正目标报告ID')
    base = _resolve_base(str(data['run_id']).strip() if data.get('run_id') else '', target_id)
    if 'csv' in data and data['csv'] is not None:
        replacement = data['csv']
    elif 'row' in data and isinstance(data['row'], dict):
        replacement = data['row']
    else:
        raise DomainError('CORRECTION', '请提供补正后的单行报告（csv 或 row）')
    return base, target_id, replacement, data.get('parameters')


def _summary(base, candidate):
    closure = set(candidate.get('closure', []))
    base_calc = {c['id']: c for c in base.get('calculations', [])}
    affected = []
    for cid in sorted(closure):
        calc = next((c for c in candidate['calculations'] if c['id'] == cid), None)
        before = base_calc.get(cid)
        if calc:
            affected.append({'id': cid, 'code': calc['code'],
                             'before': before['code'] if before else 'ISOLATED',
                             'replaces': calc.get('replaces', '')})
        else:
            # 目标被更新的复检替代：目标本身不再单列，但仍在闭包内留痕。
            affected.append({'id': cid, 'code': 'SUPERSEDED',
                             'before': before['code'] if before else 'ISOLATED',
                             'replaces': ''})
    return {
        'base_run_id': base['run_id'],
        'new_run_id': candidate['run_id'],
        'closure': candidate.get('closure', []),
        'affected': affected,
        'business_before': base.get('business', {}),
        'business_after': candidate.get('business', {}),
        'grades_after': candidate.get('reconcile', {}).get('grades', []),
        'balanced': candidate.get('reconcile', {}).get('balanced', False),
        'quarantine_before': len(base.get('quarantine', [])),
        'quarantine_after': len(candidate.get('quarantine', [])),
        'corrections': candidate.get('corrections', []),
    }
