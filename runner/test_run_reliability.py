import json
import tempfile
import unittest
from pathlib import Path
from run_reliability import (ApiAudit, InfrastructureFailure, ResourceBlocked,
    check_memory, classify_error, error_chain, normalized_to_source, RuntimeGuard, journal_action)


class APITimeoutError(Exception):
    pass


class HttpError(Exception):
    def __init__(self, status):
        self.status_code = status


class ReliabilityTests(unittest.TestCase):
    def test_action_evidence_survives_reflector_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)/'step_4'
            actions=[]
            def execute():
                actions.append('click')
                return ({'screenshot':b'after'},0,False,{})
            journal_action(folder,Path(directory)/'actions',4,'click',lambda:b'before',execute)
            audit = ApiAudit(Path(directory)/'api',sleep=lambda delay:None)
            def reflect():
                raise APITimeoutError()
            with self.assertRaises(InfrastructureFailure):
                audit.call(reflect,{})
            self.assertEqual(actions,['click'])
            self.assertEqual((folder/'after_action.png').read_bytes(),b'after')
            events=[json.loads(line) for line in (Path(directory)/'actions').read_text().splitlines()]
            self.assertEqual(events[-1]['phase'],'execution_completed')

    def test_uncertain_execution_is_not_replayed(self):
        with tempfile.TemporaryDirectory() as directory:
            actions=[]
            def execute():
                actions.append(1)
                raise ConnectionError()
            with self.assertRaises(ConnectionError):
                journal_action(Path(directory)/'step_1',Path(directory)/'actions',1,'click',lambda:b'before',execute)
            self.assertEqual(actions,[1])
            last=json.loads((Path(directory)/'actions').read_text().splitlines()[-1])
            self.assertEqual(last['phase'],'execution_unknown')

    def test_watchdog_marks_interrupted_not_success(self):
        from unittest.mock import patch, MagicMock
        import run_guarded_mobile
        with tempfile.TemporaryDirectory() as directory:
            output=Path(directory)/'newrun'
            child=MagicMock()
            child.pid=123
            child.poll.return_value=None
            with patch('sys.argv',['runner','--deadline-seconds','1','--output',str(output)]), patch.object(run_guarded_mobile.subprocess,'Popen',return_value=child), patch.object(run_guarded_mobile.subprocess,'run') as kill, patch.object(run_guarded_mobile.time,'monotonic',side_effect=[0,2]):
                self.assertEqual(run_guarded_mobile.main(),2)
                kill.assert_called_once()
            result=json.loads((output/'task_result.json').read_text())
            self.assertEqual(result['status'],'watchdog_timeout')
            self.assertIsNone(result['success'])

    def test_coordinates(self):
        x,y = normalized_to_source(85,760)
        self.assertAlmostEqual(x*1920/1932,163.2)
        self.assertAlmostEqual(y*1080/1092,820.8)

    def test_coordinate_bounds(self):
        for point in [(-1,0),(0,1001),(float('nan'),1)]:
            with self.assertRaises(ValueError):
                normalized_to_source(*point)

    def test_resource_low_physical(self):
        with self.assertRaises(ResourceBlocked):
            check_memory({'free_physical_mb':2300,'free_commit_mb':9000},2048)

    def test_resource_low_commit(self):
        with self.assertRaises(ResourceBlocked):
            check_memory({'free_physical_mb':9000,'free_commit_mb':3000},2048)

    def test_resource_enough(self):
        check_memory({'free_physical_mb':9000,'free_commit_mb':9000},2048)

    def test_error_classification(self):
        for status,category in [(401,'authentication_or_permission'),(403,'authentication_or_permission'),(402,'billing_or_payment'),(429,'rate_or_quota_limit'),(503,'api_server_error')]:
            self.assertEqual(classify_error(HttpError(status)),category)

    def test_error_chain_hides_messages(self):
        exc = APITimeoutError('SECRET')
        exc.__cause__ = TimeoutError('SECRET')
        self.assertEqual(error_chain(exc),['APITimeoutError','TimeoutError'])

    def test_reflector_retry_does_not_replay_action(self):
        with tempfile.TemporaryDirectory() as directory:
            audit = ApiAudit(Path(directory)/'events', sleep=lambda delay:None)
            audit.module = 'reflector'
            actions, requests = [], []
            actions.append('one click already executed')
            def request():
                requests.append(1)
                if len(requests)<3:
                    raise APITimeoutError('SECRET')
                return 'response'
            self.assertEqual(audit.call(request,{'image_count':2}), 'response')
            self.assertEqual(len(actions),1)
            self.assertEqual(len(requests),3)
            self.assertIsNone(audit.last_failure)
            text = audit.path.read_text()
            self.assertNotIn('SECRET',text)
            self.assertEqual(sum(json.loads(line)['phase']=='request_failed' for line in text.splitlines()),2)

    def test_exhausted_retries(self):
        with tempfile.TemporaryDirectory() as directory:
            audit = ApiAudit(Path(directory)/'events', sleep=lambda delay:None)
            def request():
                raise APITimeoutError()
            with self.assertRaises(InfrastructureFailure):
                audit.call(request,{})
            self.assertEqual(audit.last_failure.category,'api_timeout')

    def test_auth_no_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            audit = ApiAudit(Path(directory)/'events',sleep=lambda delay:None)
            attempts = []
            def request():
                attempts.append(1)
                raise HttpError(401)
            with self.assertRaises(InfrastructureFailure):
                audit.call(request,{})
            self.assertEqual(len(attempts),1)

    def test_lock_exclusivity_and_release(self):
        with tempfile.TemporaryDirectory() as directory:
            one, two = RuntimeGuard(Path(directory)/'lock'),RuntimeGuard(Path(directory)/'lock')
            one.acquire()
            try:
                with self.assertRaises(ResourceBlocked):
                    two.acquire()
            finally:
                one.close()
            two.acquire()
            two.close()


if __name__ == '__main__':
    unittest.main()
