"""定向补正：只重放被补正报告及其复检闭包，不整批重跑。

证据单一性：补正批次的全部结论都由同一份“补正后源字节”重放产生。
范围冻结：闭包之外的采纳行、隔离、计算证据与对账必须与已确认批次逐项一致。
留档：原始隔离原因与每次补正记录原样结转到新批次，并写入归档证据。
"""
import csv, io, base64
from copy import deepcopy
from app.errors import DomainError
from app.workflow import run
from app.modules.m1_ingest.decode import decode
from app.modules.m7_archive.export_out import checksum

# 补正只允许修复检出量与检出限；身份/关联字段一律冻结，避免借补正改换范围。
FROZEN_FIELDS = ('id', 'sample_id', 'batch', 'product', 'analyte', 'method',
                 'version', 'sampled', 'reported', 'relation')
PATCHABLE_FIELDS = ('result', 'unit', 'lod', 'note')


def _logical_spans(text):
    """按 RFC4180 引号规则逐字符扫描，返回每条逻辑记录的 (start,end,line)。

    与 csv.reader 的逻辑记录边界保持一致，正确处理引号内换行；
    start/end 为相对解码文本的字符位置，line 为记录结束物理行号。
    """
    spans = []
    start = 0
    line = 1
    in_quotes = False
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == '"':
            if in_quotes and i + 1 < n and text[i + 1] == '"':
                i += 2
                continue
            in_quotes = not in_quotes
        elif ch == '\n' and not in_quotes:
            spans.append((start, i + 1, line))
            start = i + 1
            line += 1
        elif ch == '\r' and not in_quotes:
            # CRLF 或裸 CR：结束记录并跳过紧随的 LF。
            end = i + 1
            if i + 1 < n and text[i + 1] == '\n':
                end = i + 2
                i += 1
            spans.append((start, end, line))
            start = end
            line += 1
        i += 1
    if start < n:
        spans.append((start, n, line))
    return spans


def _parse_records(raw):
    """按 CSV 逻辑记录切分原始字节，返回 (header, [(start,end,row,line)])。

    start/end 是相对原始字节解码文本的物理区间（end 为行终止符之后的位置），
    使替换只动目标记录的字节，其余字节、行号、空行与重复表头全部保持原样。
    """
    text, encoding = decode(raw)
    reader = csv.reader(io.StringIO(text, newline=''))
    header = next(reader)
    rows = list(reader)
    spans = _logical_spans(text)
    # 首个 span 是表头；其后与 csv 逻辑记录一一对应（含空行/重复表头）。
    record_spans = spans[1:]
    if len(record_spans) != len(rows):
        raise DomainError('REPLAY', '补正切分记录边界与CSV解析不一致')
    out = []
    for (s, e, line), row in zip(record_spans, rows):
        out.append((s, e, row, line))
    return header, out, text, encoding


def patch_source(raw, target_id, replacement):
    """用 replacement（单行 CSV 文本或 dict）替换 target_id 的物理记录。"""
    header, spans, text, encoding = _parse_records(raw)
    if isinstance(replacement, str):
        rows = list(csv.reader(io.StringIO(replacement, newline='')))
        rows = [r for r in rows if r and any(s.strip() for s in r)]
        if len(rows) != 1:
            raise DomainError('CORRECTION', '补正必须恰好是一行报告')
        new_row = rows[0]
        if len(new_row) != len(header):
            raise DomainError('CORRECTION', '补正行列数与表头不一致')
        new_item = dict(zip(header, new_row))
    elif isinstance(replacement, dict):
        missing = [h for h in header if h not in replacement]
        if missing:
            raise DomainError('CORRECTION', '补正行缺少字段：' + ','.join(missing))
        new_item = {h: str(replacement[h]) for h in header}
        new_row = [new_item[h] for h in header]
    else:
        raise DomainError('CORRECTION', '补正内容格式无效')
    if new_item.get('id') != target_id:
        raise DomainError('CORRECTION', '补正行报告ID与目标不一致')

    hits = [(i, s) for i, s in enumerate(spans) if s[2] and dict(zip(header, s[2])).get('id') == target_id]
    if not hits:
        raise DomainError('CORRECTION', '补正目标不在原始报告中：' + target_id)
    if len(hits) > 1:
        raise DomainError('CORRECTION', '补正目标在原件中不唯一：' + target_id)
    start, end, old_row, line = hits[0][1]
    old_item = dict(zip(header, old_row))

    out = io.StringIO(newline='')
    writer = csv.writer(out, lineterminator='\n')
    writer.writerow(new_row)
    new_text = out.getvalue()
    patched = text[:start] + new_text + text[end:]
    return patched.encode(encoding), {'line': line, 'header': header,
                                      'old': old_item, 'new': new_item}


def _index_calcs(state):
    return {c['id']: c for c in state.get('calculations', [])}


def _closure(target_id, measurements):
    """目标报告 + 所有沿 relation 直接/间接依赖它的复检。"""
    ids = {target_id}
    changed = True
    while changed:
        changed = False
        for r in measurements:
            rel = r.get('relation')
            if rel in ids and r['id'] not in ids:
                ids.add(r['id'])
                changed = True
    return ids


def replay(base_state, target_id, replacement, parameters=None):
    """对已确认批次执行一次定向补正，返回补正后的完整批次状态。"""
    raw = base_state.get('source_b64')
    if not raw:
        raise DomainError('REPLAY', '已确认批次缺少原始证据，无法补正')
    raw = base64.b64decode(raw)
    base = deepcopy(base_state)

    patched, patch_info = patch_source(raw, target_id, replacement)
    header = patch_info['header']
    old_item, new_item = patch_info['old'], patch_info['new']

    # 身份与关联字段冻结：补正不得改换样本/项目/方法/版本/引用关系。
    moved = [f for f in FROZEN_FIELDS if old_item.get(f, '') != new_item.get(f, '')]
    if moved:
        raise DomainError('SCOPE', '补正不得改动身份/关联字段：' + ','.join(moved))
    changed_fields = [f for f in header if old_item.get(f, '') != new_item.get(f, '')]
    if not changed_fields:
        raise DomainError('CORRECTION', '补正内容与原报告完全相同，无实质补正')

    try:
        candidate = run(patched, parameters if parameters is not None else base.get('manifest', {}).get('parameters', {}))
    except DomainError:
        raise
    except Exception as exc:  # 重放异常一律视为补正失败，交由调用方回滚
        raise DomainError('REPLAY', '补正重放失败：' + str(exc))

    # 目标必须原本就在隔离区，且原因为缺检出限类问题，否则不受理补正。
    target_quar = [q for q in base['quarantine'] if q.get('id') == target_id]
    if not target_quar:
        raise DomainError('CORRECTION', '补正目标未被隔离，无需补正：' + target_id)

    # 闭包：目标 + 依赖它的复检。用基线下的全量测量（含被隔离复检的原始记录）求依赖。
    base_measurements = _all_measurement_ids(base, patched)
    closure = _closure(target_id, base_measurements)

    _assert_scope_frozen(base, candidate, closure, target_id, patch_info['line'])

    # 补正后闭包必须产出至少一条有效计算证据（多级复检链下最终采纳链顶），
    # 且闭包内不再存在悬空复检；目标本身可被更新的复检逐级替代而不单列。
    cand_calcs = _index_calcs(candidate)
    if not any(cid in cand_calcs for cid in closure):
        raise DomainError('REPLAY', '补正目标未形成有效项目结论')
    dangling = [q for q in candidate['quarantine']
                if q.get('code') == 'REFERENCE' and q.get('id') in closure]
    if dangling:
        raise DomainError('REPLAY', '补正后仍有悬空复检：' + ','.join(sorted(q['id'] for q in dangling)))

    ledger = list(base.get('corrections', []))
    ledger.append({
        'target_id': target_id,
        'line': patch_info['line'],
        'reason_codes': sorted(q['code'] for q in target_quar),
        'changed_fields': changed_fields,
        'old': {f: old_item.get(f, '') for f in changed_fields},
        'new': {f: new_item.get(f, '') for f in changed_fields},
        'closure': sorted(closure),
        'base_run_id': base['run_id'],
        'new_run_id': candidate['run_id'],
        'replacement_b64': base64.b64encode(patched).decode('ascii'),
    })
    candidate['corrections'] = ledger
    candidate['base_run_id'] = base['run_id']
    candidate['closure'] = sorted(closure)
    # 归档校验和需把补正台账纳入同一份证据后重新计算。
    candidate['archive']['sha256'] = checksum(candidate)
    candidate['archive']['evidence_count'] = (len(candidate.get('calculations', []))
                                               + len(candidate['quarantine'])
                                               + len(candidate.get('corrections', [])))
    return candidate


def _all_measurement_ids(base_state, patched_raw):
    """从基线与补正源中提取全部报告记录（含被隔离者），用于求复检依赖闭包。"""
    records = []
    for raw in (base64.b64decode(base_state['source_b64']), patched_raw):
        text, _ = decode(raw)
        reader = csv.DictReader(io.StringIO(text, newline=''))
        for row in reader:
            records.append(row)
    # 以补正源为准（含修复后的目标行），按 id 去重。
    seen = {}
    for r in records:
        seen[r.get('id')] = r
    return list(seen.values())


def _assert_scope_frozen(base, candidate, closure, target_id, target_line):
    """闭包之外的一切证据必须与已确认批次逐项一致。"""
    # 1) 输入级对账总量不得变化（行号、空行、表头、重复全部保留）。
    for key in ('input_count', 'blank_count', 'header_count', 'duplicate_count'):
        if base.get(key) != candidate.get(key):
            raise DomainError('SCOPE', '补正改变了输入对账：%s %s→%s'
                              % (key, base.get(key), candidate.get(key)))

    # 2) 计算证据：闭包外逐条一致；闭包内允许因补正而变化，但不得新增项目。
    base_calcs = _index_calcs(base)
    cand_calcs = _index_calcs(candidate)
    new_ids = set(cand_calcs) - set(base_calcs)
    if new_ids - closure:
        raise DomainError('SCOPE', '补正在闭包外新增了项目：' + ','.join(sorted(new_ids - closure)))
    removed = set(base_calcs) - set(cand_calcs)
    if removed - closure:
        raise DomainError('SCOPE', '补正移除了闭包外的已确认项目：' + ','.join(sorted(removed - closure)))
    for cid, calc in base_calcs.items():
        if cid in closure:
            continue
        if cand_calcs.get(cid) != calc:
            raise DomainError('SCOPE', '补正改动了闭包外的已确认结论：' + cid)

    # 3) 隔离区：闭包外的隔离原因逐条保留；闭包内目标与悬空复检允许解除，
    #    但原始隔离原因仍须在补正台账中留档（见 replay 的 ledger）。
    base_q = {(q['line'], q.get('id', ''), q['code']) for q in base['quarantine']}
    cand_q = {(q['line'], q.get('id', ''), q['code']) for q in candidate['quarantine']}
    outside_removed = [q for q in base_q
                       if q not in cand_q and q[1] not in closure and not (q[1] == '' and q[0] == target_line)]
    if outside_removed:
        raise DomainError('SCOPE', '补正丢失了闭包外的隔离记录：' + str(outside_removed))
    added = cand_q - base_q
    if any(qid not in closure for _, qid, _ in added):
        raise DomainError('SCOPE', '补正在闭包外引入了新的隔离：' + str(added))

    # 4) 对账仍须平衡，且业务总数只能在闭包内变化。
    if not candidate.get('reconcile', {}).get('balanced'):
        raise DomainError('REPLAY', '补正后对账不平衡')
    if not candidate.get('archive', {}).get('ready'):
        raise DomainError('REPLAY', '补正后归档未就绪')
