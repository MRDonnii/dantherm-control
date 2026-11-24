import sys,sqlite3
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).parents[1]/'gateway'))
from control_history import ControlHistory
from operating_context import afterheat_context,NORMAL,FROST,STARTUP,BOOST,OFF,FAN_CHANGE,EXTERNAL
from dashboard_server import HistoryStore

def test_recording_is_rate_limited_bounded_and_preserves_raw_observations(tmp_path):
    store=ControlHistory(tmp_path/'inputs.sqlite3')
    obs={'rooms':{'Bad':{'humidity':80}},'ha_online':True,'local':{'co2':500}}
    with patch('control_history.MAX_RECORDS',3):
        for ts in (10000,10010,10060,10120,10180):store.record(obs,now=ts)
    with patch('control_history.time.time',return_value=10200):rows=store.query(3600)
    assert [row['ts'] for row in rows]==[10060,10120,10180]
    assert rows[0]['rooms']['Bad']['humidity']==80
    store.record({'ha_online':False},now=1000000)
    with patch('control_history.time.time',return_value=1000000):assert len(store.query(86400))==1

def test_failure_of_optional_recording_is_not_fatal(tmp_path):
    path=tmp_path/'file';path.write_text('not a directory')
    store=ControlHistory(path/'history.sqlite3')
    assert not store.available
    store.record({'rooms':{}})
    assert store.query(86400)==[]

def test_afterheat_context_is_annotation_and_does_not_modify_commands():
    snap={'afterheat_command':22,'afterheat_frost_lock':False}
    for expected,patch_ in [(NORMAL,{}),(FROST,{'afterheat_frost_lock':True}),(BOOST,{'quick_boost_active':True}),
                             (OFF,{'afterheat_command':None})]:
        candidate={**snap,**patch_};before=dict(candidate)
        assert afterheat_context(candidate,pi_master=True,uptime=1000,fan_changed=False)==expected
        assert candidate==before
    assert afterheat_context(snap,pi_master=True,uptime=10,fan_changed=False)==STARTUP
    assert afterheat_context(snap,pi_master=True,uptime=1000,fan_changed=True)==FAN_CHANGE
    assert afterheat_context(snap,pi_master=False,uptime=1000,fan_changed=False)==EXTERNAL

def test_history_never_averages_context_codes_or_hides_unknowns(tmp_path):
    store=HistoryStore(tmp_path/'history.sqlite3')
    # 24h history groups 120 second buckets: mixed and partial legacy are unknown.
    with store._connect() as db:
        db.executemany('INSERT INTO samples (ts,afterheat_context) VALUES (?,?)',
                       [(120000,0),(120030,0),(120120,0),(120150,2),(120240,0),(120270,None)])
    with patch('dashboard_server.time.time',return_value=120300):
        rows=store.query('24h')
    assert [row['afterheat_context'] for row in rows]==[0,None,None]
