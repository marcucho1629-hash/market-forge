"""Replay normalized shadow JSONL into an isolated SQLite journal and export CSV.
Not a live HTTP receiver. No Telegram credentials, production imports or network.
Each line: {receipt_id: str, observations: [...]}. Dates must include timezone.
"""
import argparse,csv,json
from datetime import datetime
from pathlib import Path
from .store import ShadowStore
DATE_FIELDS={'source_bar_start','bar_end','observed_at','webhook_emitted_at','server_received_at'}
ALLOWED=DATE_FIELDS|{'engine','ticker','feed','timeframe','direction','core_version','core_ok','price','previous_confirmed_atr','kind','confirmed','chart_x'}
REQUIRED=ALLOWED-{'kind','confirmed','chart_x'}
def normalize(row):
 if not isinstance(row,dict) or not REQUIRED<=row.keys():raise ValueError('missing normalized fields')
 if row.keys()-ALLOWED:raise ValueError('unknown normalized fields')
 r=dict(row)
 for k in DATE_FIELDS:
  if not isinstance(r[k],str):raise ValueError('timestamp string required: '+k)
  r[k]=datetime.fromisoformat(r[k].replace('Z','+00:00'))
  if r[k].tzinfo is None:raise ValueError('timezone required: '+k)
 if type(r['direction']) is not int or r['direction'] not in (-1,1):raise ValueError('direction must be -1 or 1')
 for k in ('core_ok','chart_x'):
  if k in r and type(r[k]) is not bool:raise ValueError('boolean required: '+k)
 for k in ('ticker','feed','core_version'):
  if not isinstance(r[k],str) or not r[k].strip():raise ValueError('identity missing: '+k)
 if str(r['timeframe'])!='5' or (r['bar_end']-r['source_bar_start']).total_seconds()!=300:raise ValueError('5m source bar required')
 if r.get('kind','pulse') not in ('pulse','closed'):raise ValueError('invalid kind')
 if r.get('kind')=='closed' and type(r.get('confirmed')) is not bool:raise ValueError('explicit close confirmation required')
 return r

def run(input_path,db_path,outdir,experiment,config):
 store=ShadowStore(db_path,experiment,config);accepted=duplicates=0
 try:
  with open(input_path) as f:
   for line_no,line in enumerate(f,1):
    if not line.strip():continue
    try:
     envelope=json.loads(line)
     if set(envelope)!= {'receipt_id','observations'}:raise ValueError('invalid envelope fields')
     if not isinstance(envelope['receipt_id'],str) or not isinstance(envelope['observations'],list):raise ValueError('invalid envelope types')
     rows=[normalize(r) for r in envelope['observations']]
     if store.ingest_batch(envelope['receipt_id'],rows):accepted+=1
     else:duplicates+=1
    except (ValueError,KeyError,TypeError) as e:raise ValueError(f'line {line_no}: {e}') from e
  out=Path(outdir);out.mkdir(parents=True,exist_ok=True)
  rows=store.rows()
  with (out/'candidates.csv').open('w',newline='') as f:
   writer=csv.DictWriter(f,fieldnames=list(rows[0]) if rows else ['candidate_id','engine','phase'])
   writer.writeheader();writer.writerows(rows)
  (out/'events.json').write_text(json.dumps(store.events(),indent=2))
  result=dict(accepted=accepted,duplicates=duplicates,candidates=len(rows),scope='normalized input only; not live performance')
  (out/'replay-summary.json').write_text(json.dumps(result,indent=2))
  return result
 finally:store.close()
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('input');p.add_argument('--db',required=True);p.add_argument('--output',required=True);p.add_argument('--experiment',required=True);p.add_argument('--config',required=True)
 a=p.parse_args();print(json.dumps(run(a.input,a.db,a.output,a.experiment,a.config)))
