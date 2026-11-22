import io,csv,json,hashlib,base64

def bundle(state):
    out=io.StringIO(newline='');writer=csv.writer(out)
    writer.writerow(['记录类型','批次','来源行','业务ID','原因码','内容'])
    rid=state.get('run_id','preview')
    writer.writerow(['运行清单',rid,'','','',json.dumps(state.get('manifest',{}),ensure_ascii=False,sort_keys=True)])
    if state.get('base_run_id'):
        writer.writerow(['补正链',rid,'','','',json.dumps({'base_run_id':state['base_run_id'],'closure':state.get('closure',[])},ensure_ascii=False,sort_keys=True)])
    writer.writerow(['对账',rid,'','','',json.dumps(state.get('reconcile',{}),ensure_ascii=False,sort_keys=True)])
    for row in state.get('calculations',[]):writer.writerow(['计算',rid,row.get('line',''),row.get('id',''),row.get('code',''),json.dumps(row,ensure_ascii=False,sort_keys=True)])
    for row in state['quarantine']:writer.writerow(['隔离',rid,row['line'],row.get('id',''),row['code'],json.dumps(row.get('raw',{}),ensure_ascii=False,sort_keys=True)])
    for row in state.get('corrections',[]):
        evidence={k:v for k,v in row.items() if k!='replacement_b64'}
        evidence['replacement_sha256']=hashlib.sha256(base64.b64decode(row['replacement_b64'])).hexdigest()
        writer.writerow(['补正',rid,row.get('line',''),row.get('target_id',''),'|'.join(row.get('reason_codes',[])),json.dumps(evidence,ensure_ascii=False,sort_keys=True)])
    return '﻿'+out.getvalue()

def checksum(state):
    return hashlib.sha256(bundle(state).encode('utf-8')).hexdigest()
