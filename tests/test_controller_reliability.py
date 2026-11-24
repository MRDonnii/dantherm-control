import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).parents[1] / 'gateway'))
from controller_core import ControllerState, ControllerEngine, HardwareAdapter
from controller_runtime import ControllerRuntime
from controller_observability import ControllerObservability
from sensor_freshness import CONTROL_SENSOR_TIMESTAMPS
from dantherm_gateway import Gateway

class ReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
    def runtime(self, state=None):
        return ControllerRuntime(gateway_state=state or {}, hardware=HardwareAdapter(), state_path=self.root/'controller.json', tick_seconds=1)
    def test_explicit_missing_clears_measurement_but_partial_update_preserves_others(self):
        e=self.runtime().engine
        e.update_measurements(co2=1800, outdoor=-5)
        e.update_measurements(outdoor=2)
        self.assertEqual(e.measurements['co2'],1800)
        e.update_measurements(co2=None, outdoor=float('nan'), rh=True)
        self.assertIsNone(e.measurements['co2'])
        self.assertIsNone(e.measurements['outdoor'])
        self.assertIsNone(e.measurements['rh'])
    def test_individual_stale_measurements_expire_while_other_bus_traffic_continues(self):
        now=time.monotonic()
        state={'sensor_freshness_tracking':True,'co2':1800,'humidity':75,'outdoor_temp':-5,'extract_temp':22,
               'bus_traffic':True,'bus_last_frame_age':0.1}
        for keys in CONTROL_SENSOR_TIMESTAMPS.values(): state[keys[0]]=now
        runtime=self.runtime(state)
        runtime.refresh_measurements()
        self.assertEqual(runtime.engine.measurements['co2'],1800)
        state['co2_sample_monotonic']=now-46
        runtime.refresh_measurements()
        self.assertIsNone(runtime.engine.measurements['co2'])
        self.assertEqual(runtime.engine.measurements['outdoor'],-5)
        health=ControllerObservability(runtime,path=self.root/'log')._data_health(runtime.snapshot())
        self.assertFalse(health['sensors']['co2']['fresh'])
    def test_unchanged_received_samples_refresh_their_own_timestamp(self):
        gateway=Gateway.__new__(Gateway)
        gateway.state={'co2':600,'outdoor_temp':10,'co2_sample_monotonic':1,'outdoor_temperature_sample_monotonic':2}
        with patch('dantherm_gateway.time.monotonic',return_value=100): gateway.publish('co2',600)
        self.assertEqual(gateway.state['co2_sample_monotonic'],100)
        self.assertEqual(gateway.state['outdoor_temperature_sample_monotonic'],2)
    def test_monotonic_bus_clock_overrules_frozen_boolean(self):
        r=self.runtime({'bus_traffic':True,'bus_last_frame_age':0})
        r.master.last_bus_frame=time.monotonic()-11
        self.assertFalse(r._bus_healthy())
    def test_temporary_failures_recover_beyond_fast_retry_limit(self):
        engine=self.runtime().engine
        def broken(*_): raise OSError('missed ack')
        for attempt in range(8):
            engine._retry_at.clear()
            with self.assertRaises(OSError): engine._call('fan_pair',(30,30),broken,30,30)
            self.assertLessEqual(engine._retry_at['fan_pair']-time.time(),300)
        recovered=[]
        # A different successful write cannot hide the still failing fan command.
        engine._call('bypass','auto',lambda *_:None,'auto')
        self.assertIsNotNone(engine.last_error)
        engine._retry_at.clear()
        engine._call('fan_pair',(30,30),lambda *args:recovered.append(args),30,30)
        self.assertEqual(recovered,[(30,30)])
        self.assertFalse(engine.command_errors)
        self.assertIsNone(engine.last_error)
    def test_cycle_recovers_from_disk_failure_without_exiting_thread(self):
        r=self.runtime()
        r.config.data['quick_boost_until']=time.time()-1
        r.config.data['quick_boost_minutes']=10
        saves=[]
        def failed_save():
            saves.append(True)
            raise OSError('full disk')
        original_tick=r.tick
        calls=[]
        def tick():
            calls.append(True)
            if len(calls)==2: r.stop_event.set()
            original_tick()
        with patch.object(r.config,'save',side_effect=failed_save),patch.object(r,'tick',side_effect=tick):
            with self.assertLogs('passivelink-controller',level='ERROR'): r._run()
        self.assertEqual(len(calls),2)
        self.assertEqual(r.loop_failures,1)
        self.assertIsNotNone(r.last_successful_tick_monotonic)
        self.assertIsNone(r.loop_error)
    def test_polling_snapshot_cannot_refresh_loop_heartbeat(self):
        r=self.runtime()
        r.last_successful_tick_monotonic=time.monotonic()-100
        r.snapshot();r.snapshot()
        self.assertGreater(r.loop_health()['last_success_age_seconds'],99)
        self.assertEqual(r.loop_health()['state'],'stalled')
