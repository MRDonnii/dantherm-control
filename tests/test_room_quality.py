import sys,time,tempfile
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).parents[1]/'gateway'))
from room_quality import quality_rooms,age_observations
from controller_runtime import ControllerRuntime
from controller_core import HardwareAdapter


def test_every_measurement_expires_on_report_age_not_transport_heartbeat():
    room={'measurement_age_version':1,'temperature':22,'humidity':65,'co2':1800,'pm25':60,
          'temperature_age_s':20,'humidity_age_s':10801,'co2_age_s':10799,'pm25_age_s':10801}
    effective,q=quality_rooms({'Stue':room},elapsed=2)
    assert effective['Stue']['temperature']==22
    assert 'humidity' not in effective['Stue'] and 'co2' not in effective['Stue'] and 'pm25' not in effective['Stue']
    assert q['Stue']['co2']['state']=='stale'
    assert room['co2']==1800


def test_missing_and_invalid_ages_are_visible_and_never_refreshed_by_forwarding():
    for age in [None,float('nan'),True,-1]:
        clean,q=quality_rooms({'Stue':{'measurement_age_version':1,'co2':1800,'co2_age_s':age}})
        assert 'co2' not in clean['Stue'] and q['Stue']['co2']['state']=='unknown_age'
    clean,q=quality_rooms({'Old client':{'co2':1800}})
    assert clean['Old client']['co2']==1800 and q['Old client']['co2']['state']=='unknown_age'
    clean,q=quality_rooms({'Stue':{'co2':1800,'co2_age_s':0}},online=False)
    assert 'co2' not in clean['Stue'] and q['Stue']['co2']['state']=='connection_stale'


def test_age_keeps_advancing_without_new_ha_message_and_temperature_fallback_is_safe():
    with tempfile.TemporaryDirectory() as tmp:
        state={'extract_temp':21,'outdoor_temp':5,'co2':600}
        r=ControllerRuntime(gateway_state=state,hardware=HardwareAdapter(),state_path=Path(tmp)/'state.json')
        r.config.configure({'mode':'local_auto','afterheat_room_source':'auto'})
        r.room_inputs({'rooms':{'Stue':{'measurement_age_version':1,'temperature':24,'temperature_age_s':10799,
                                      'humidity':50,'humidity_age_s':0,'co2':1800,'co2_age_s':10799}},'valid_for_s':180})
        r.smart_inputs_received_monotonic=time.monotonic()-2
        assert r._fresh_ha_rooms()['Stue'].get('temperature') is None
        assert r._fresh_ha_rooms()['Stue'].get('co2') is None
        r.refresh_measurements()
        assert r.engine.measurements['room']==21
        assert r.afterheat_room_source_used=='t3'


def test_stale_humidity_cannot_extend_a_bathroom_episode_or_trigger_old_rise():
    with tempfile.TemporaryDirectory() as tmp:
        r=ControllerRuntime(gateway_state={},hardware=HardwareAdapter(),state_path=Path(tmp)/'state.json')
        r._room_rh_history['Bad'].extend([(time.time()-300,40),(time.time(),80)])
        r.room_inputs({'rooms':{'Bad':{'measurement_age_version':1,'humidity':80,'humidity_age_s':20000,'room_type':'bathroom'}}})
        assert r.smart_controlling_metric is None
        assert not r._bathroom_episodes


def test_recording_ages_is_a_copy_and_preserves_unknown_ages():
    rooms={'Stue':{'co2':700,'co2_age_s':20,'humidity':50}}
    assert age_observations(rooms,10)['Stue']['co2_age_s']==30
    assert rooms['Stue']['co2_age_s']==20
    assert 'humidity_age_s' not in age_observations(rooms,10)['Stue']
