import copy,sys,tempfile
from pathlib import Path
from unittest.mock import patch
import pytest
sys.path.insert(0,str(Path(__file__).parents[1]/'gateway'))
from control_replay import replay_smart_auto
from controller_core import ControllerState,ControllerError

@pytest.fixture
def settings(tmp_path):
    state=ControllerState(tmp_path/'controller.json')
    state.configure({'fan_step_count':6,'local_normal_level':3,'air_quality_max_level':6,
                     'local_max_level':6,'humidity_smart_enabled':False})
    return state

def observation(ts,room):
    return {'ts':ts,'ha_online':True,'rooms':{'Stue':room},'local':{'co2':500,'humidity':45,'extract_temp':22,'outdoor_temp':5},
            'external':{'extract_temp':22},'observed_level':3,'observed_source':'ha_smart'}

def test_priorities_change_only_the_isolated_proposed_demand(settings):
    samples=[observation(10000+i*60,{'co2':1200,'co2_age_s':0,'measurement_age_version':1,'priority':'normal'}) for i in range(5)]
    before=copy.deepcopy(settings.data);disk=settings.path.read_bytes()
    with (patch('controller_core.ControllerEngine.apply',side_effect=AssertionError('hardware')),
          patch('controller_core.ControllerEngine.resolve',side_effect=AssertionError('afterheat path'))):
        result=replay_smart_auto(settings.data,samples,{'room_priorities':{'Stue':'critical'}})
    assert result['scope']=='smart_auto_demand' and result['covered_minutes']==4
    assert result['different_minutes']>0
    assert result['points'][0]['proposed_level']>result['points'][0]['current_level']
    assert settings.data==before and settings.path.read_bytes()==disk
    assert samples[0]['rooms']['Stue']['priority']=='normal'

def test_bathroom_maximum_and_drying_are_replayed(settings):
    samples=[observation(10000+i*60,{'humidity':80-i,'humidity_age_s':0,'measurement_age_version':1,
                                    'temperature':22,'temperature_age_s':0,'room_type':'bathroom'}) for i in range(8)]
    result=replay_smart_auto(settings.data,samples,{'bathroom_max_level':4})
    assert result['points'][0]['current_level']>result['points'][0]['proposed_level']
    assert any('Badeværelse' in point['current_reason'] for point in result['points'])

def test_missing_connection_falls_back_and_gaps_do_not_count(settings):
    samples=[observation(10000,{'co2':1600}),observation(10060,{'co2':1600}),observation(20000,{'co2':500}),observation(20060,{'co2':500})]
    samples[-1]['ha_online']=False
    result=replay_smart_auto(settings.data,samples,{})
    assert result['covered_minutes']==2
    assert result['different_minutes']==0
    assert result['points'][-1]['current_reason'].startswith('HA offline;')

def test_stale_room_value_is_not_reanimated_in_replay(settings):
    samples=[observation(10000,{'co2':3000,'co2_age_s':10801,'measurement_age_version':1})]
    result=replay_smart_auto(settings.data,samples,{})
    assert result['points'][0]['current_room']!='Stue'

def test_validation(settings):
    for invalid in ({'afterheat_setpoint':30},{'room_priorities':{'Stue':'bad'}},{'room_priorities':[]}):
        with pytest.raises(ControllerError):replay_smart_auto(settings.data,[],invalid)
