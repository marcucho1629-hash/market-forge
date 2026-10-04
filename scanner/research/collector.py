"""Offline shadow collector. Receives normalized observations; never sends messages.
Existing-engine outcomes must be imported from actual logs, not reconstructed here.
"""
import math
from .lifecycle import Candidate,candidate_id,observe,expire,timestamps

class Collector:
 def __init__(self,experiment_id,config_hash):
  if not experiment_id or not config_hash:raise ValueError('experiment/config required')
  self.experiment_id=experiment_id;self.config_hash=config_hash
  self.states={};self.events=[];self.metadata={}
 def ingest(self,row):
  engine=row['engine']
  if engine not in ('timing_only','shared_core'):raise ValueError('unsupported shadow engine')
  for flag in ('core_ok',):
   if type(row[flag]) is not bool:raise ValueError('explicit boolean required')
  observed=row['observed_at'];emitted=row['webhook_emitted_at'];received=row['server_received_at']
  if not observed<=emitted<=received:raise ValueError('clock ordering')
  cid=candidate_id(row['ticker'],row['feed'],row['timeframe'],row['source_bar_start'],row['direction'],row['core_version'])
  key=(engine,cid)
  s=self.states.get(key)
  if s is None:
   # Only first-minute/two-minute intrabar candidates enter this frozen experiment.
   age=(observed-row['source_bar_start']).total_seconds()
   if row.get('kind','pulse')!='pulse' or not row['core_ok'] or (received-observed).total_seconds()>15:return None
   if age<0 or (engine=='shared_core' and not 60<=age<=120):return None
   if not all(math.isfinite(row[k]) and row[k]>0 for k in ('price','previous_confirmed_atr')):raise ValueError('invalid price/ATR')
   if row['bar_end']<=observed:raise ValueError('invalid source bar')
   s=Candidate(cid,self.experiment_id,self.config_hash,row['direction'],observed,row['bar_end'])
   self.states[key]=s
   self.metadata[key]={k:row[k] for k in ('ticker','feed','timeframe','source_bar_start','core_version')}
   self.events.append(dict(engine=engine,candidate_id=cid,from_phase=None,to_phase='PREP',**timestamps(observed,emitted,received)))
  next_s=observe(s,identity=cid,source_observed_at=observed,webhook_emitted_at=emitted,server_received_at=received,core_ok=row['core_ok'],price=row['price'],atr=row['previous_confirmed_atr'],kind=row.get('kind','pulse'),confirmed=row.get('confirmed'),chart_x=row.get('chart_x',False))
  if next_s.phase!=s.phase or next_s.chart_x_at!=s.chart_x_at:
   self.events.append(dict(engine=engine,candidate_id=cid,from_phase=s.phase,to_phase=next_s.phase,event='CHART_X' if next_s.chart_x_at!=s.chart_x_at else next_s.reason,**timestamps(observed,emitted,received)))
  self.states[key]=next_s
  return next_s
 def sweep(self,now):
  # Independent deadlines per candidate; no whole-batch completion flag.
  for key,s in list(self.states.items()):
   n=expire(s,now)
   if n!=s:self.events.append(dict(engine=key[0],candidate_id=key[1],from_phase=s.phase,to_phase=n.phase,event=n.reason,server_received_at=now.isoformat()))
   self.states[key]=n
 def rows(self):
  rows=[]
  for (engine,cid),s in self.states.items():
   rows.append(dict(candidate_id=cid,engine=engine,experiment_id=s.experiment_id,config_hash=s.config_hash,**self.metadata[(engine,cid)],prep=s.prep_at.isoformat(),would_send=s.send_at.isoformat() if s.send_at else None,gap_sec=(s.send_at-s.prep_at).total_seconds() if s.send_at else None,confirmed=s.confirmed_at.isoformat() if s.confirmed_at else None,pre_send_expire=s.phase=='EXPIRED',pre_send_cancel=s.phase=='CANCELLED',post_send_invalidate=s.phase=='INVALIDATED',close_missing=s.phase=='EXPIRED_CLOSE_MISSING',phase=s.phase,reason=s.reason,signal_price=s.signal_price,chart_x=s.chart_x_at.isoformat() if s.chart_x_at else None,**s.metrics()))
  return rows
