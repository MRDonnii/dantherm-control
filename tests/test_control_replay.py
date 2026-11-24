import copy
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parents[1]/'gateway'))
from control_replay import replay_local_auto
from controller_core import ControllerState, ControllerError
from unittest.mock import patch

class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.config=ControllerState(Path(self.tmp.name)/'controller.json')
        self.config.configure({'co2_setpoint':1000,'local_normal_level':2})
        self.samples=[{'ts':1000+i*60,'co2':1500,'humidity':40,'outdoor_temp':5,'extract_temp':22} for i in range(30)]
    def test_replay_compares_real_local_auto_logic_without_touching_live_state_or_hardware(self):
        before=copy.deepcopy(self.config.data)
        disk=self.config.path.read_bytes()
        with patch('controller_core.ControllerEngine.apply',side_effect=AssertionError('hardware path')):
            result=replay_local_auto(self.config.data,self.samples,{'co2_setpoint':1800})
        self.assertEqual(result['scope'],'local_auto')
        self.assertEqual(result['covered_minutes'],29)
        self.assertGreater(result['different_minutes'],0)
        self.assertEqual(self.config.data,before)
        self.assertEqual(self.config.path.read_bytes(),disk)
    def test_gaps_are_not_counted_as_covered_or_as_differences(self):
        samples=[{'ts':1000,'co2':1500},{'ts':1060,'co2':1500},{'ts':10000,'co2':500},{'ts':10060,'co2':500}]
        result=replay_local_auto(self.config.data,samples,{'co2_setpoint':1800})
        self.assertEqual(result['covered_minutes'],2)
        self.assertEqual(result['different_minutes'],1)
        self.assertFalse(result['humidity_available'])
    def test_limits_and_invalid_settings(self):
        for patch_ in ({'mode':'manual'},{'co2_setpoint':-50},[]):
            with self.assertRaises(ControllerError): replay_local_auto(self.config.data,self.samples,patch_)
        with self.assertRaises(ControllerError): replay_local_auto(self.config.data,self.samples*25,{})
