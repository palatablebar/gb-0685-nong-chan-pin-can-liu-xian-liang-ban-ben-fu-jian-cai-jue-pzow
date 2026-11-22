'use strict';
const register={loading:false};const statusMessage=document.getElementById('status-message');
async function adjudicate(input){if(register.loading)return;register.loading=true;statusMessage.textContent='报告裁决中…';try{const response=await fetch('/api/runs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(input)});const data=await response.json();if(!response.ok)throw Error(data.message);location.reload();}catch(error){statusMessage.textContent=error.message+'，请修正报告';}finally{register.loading=false;}}
document.getElementById('rerun').addEventListener('click',()=>adjudicate({sample:'dirty.csv'}));document.getElementById('print').addEventListener('click',()=>print());
const source=document.getElementById('lab-source');if(source)source.addEventListener('submit',event=>{event.preventDefault();const fields=new FormData(source);const csv=fields.get('csv');adjudicate(csv?{csv}:{sample:fields.get('sample')});});
const sign=document.getElementById('sign');if(sign)sign.addEventListener('click',async()=>{try{const response=await fetch('/api/runs/'+sign.dataset.id+'/review',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({version:Number(sign.dataset.version),decision:'通过'})});const data=await response.json();if(!response.ok)throw Error(data.message);sign.dataset.version=data.review_version;statusMessage.textContent='报告已复核 / '+data.review_version;}catch(error){statusMessage.textContent=error.message;}});

const correctionForm=document.getElementById('correction-form');
if(correctionForm){
  const box=document.getElementById('correction-result');
  async function callCorrection(endpoint,reloadAfter){
    const fields=new FormData(correctionForm);
    const body={target_id:fields.get('target_id'),csv:fields.get('csv')};
    try{
      const response=await fetch(endpoint,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
      const data=await response.json();
      if(!response.ok)throw Error(data.message);
      if(reloadAfter){statusMessage.textContent='补正已落盘，批次结果已更新 / '+data.new_run_id;setTimeout(()=>location.reload(),700);return;}
      const rows=(data.affected||[]).map(r=>'<tr><td>'+r.id+'</td><td>'+r.before+' → '+r.code+'</td><td>'+(r.replaces||'—')+'</td></tr>').join('');
      box.innerHTML='<div class="panel" style="border-color:var(--ok)"><header><h2>预演：闭包 '+data.closure.join('、')+'</h2><span>'+(data.balanced?'对账平衡':'对账不平')+'</span></header>'
        +'<div class="scroll"><table><thead><tr><th>报告</th><th>结论变化</th><th>替代</th></tr></thead><tbody>'+rows+'</tbody></table></div>'
        +'<dl><dt>隔离</dt><dd>'+data.quarantine_before+' → '+data.quarantine_after+'</dd><dt>合格项目</dt><dd>'+data.business_before.pass+' → '+data.business_after.pass+'</dd></dl></div>';
    }catch(error){box.innerHTML='';statusMessage.textContent=error.message+'，已保留旧版结果';}
  }
  document.getElementById('correction-preview').addEventListener('click',()=>callCorrection('/api/m8_correction/preview',false));
  document.getElementById('correction-apply').addEventListener('click',()=>callCorrection('/api/m8_correction/apply',true));
}
