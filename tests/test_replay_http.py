import json, os, sys, tempfile, unittest, urllib.request, urllib.error
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).parents[1]/'gateway'))
from controller_dashboard_server import ControllerDashboardHttpServer
from controller_runtime import ControllerRuntime
from controller_core import HardwareAdapter

class ReplayHttpTests(unittest.TestCase):
    def test_replay_requires_session_csrf_and_configuration_permission(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            with patch.dict(os.environ,{'DANTHERM_WEBUI_AUTH_FILE':str(root/'auth.json')}):
                runtime=ControllerRuntime(gateway_state={},hardware=HardwareAdapter(),state_path=root/'controller.json')
                server=ControllerDashboardHttpServer('127.0.0.1',0,{},'Test',None,history_path=root/'history.sqlite3',controller_runtime=runtime)
            runtime.config.save()
            server.auth.save('owner','long-test-password',True)
            server.auth.create_user('reader','long-test-password',role='user')
            server.start()
            try:
                url=f'http://127.0.0.1:{server.server.server_address[1]}/api/controller/replay'
                def request(sid=None, csrf=None):
                    headers={'Content-Type':'application/json'}
                    if sid: headers['Cookie']='dantherm_session='+sid
                    if csrf: headers['X-CSRF-Token']=csrf
                    return urllib.request.urlopen(urllib.request.Request(url,data=b'{"settings":{"co2_setpoint":1200}}',headers=headers),timeout=5)
                with self.assertRaises(urllib.error.HTTPError) as e: request()
                self.assertEqual(e.exception.code,401)
                sid,csrf=server.auth.session('owner')
                with self.assertRaises(urllib.error.HTTPError) as e: request(sid)
                self.assertEqual(e.exception.code,403)
                reader,reader_csrf=server.auth.session('reader')
                with self.assertRaises(urllib.error.HTTPError) as e: request(reader,reader_csrf)
                self.assertEqual(e.exception.code,403)
                before=runtime.config.path.read_bytes()
                with request(sid,csrf) as response: result=json.load(response)
                self.assertEqual(result['scope'],'local_auto')
                self.assertEqual(runtime.config.path.read_bytes(),before)
            finally: server.stop()
