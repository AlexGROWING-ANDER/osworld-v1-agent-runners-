"""A killable API subprocess enforces total per-call time, including response reads."""
import multiprocessing as mp
from types import SimpleNamespace


class TransportFailure(RuntimeError):
    def __init__(self, category, chain=None, status_code=None):
        super().__init__(category)
        self.category = category
        self.remote_chain = chain or []
        self.status_code = status_code


def _serve(pipe, settings):
    from openai import OpenAI
    import httpx
    from run_reliability import classify_error, error_chain
    client = OpenAI(base_url=settings['base_url'], api_key=settings['api_key'],
                    max_retries=0, timeout=settings['timeout'],
                    http_client=httpx.Client(trust_env=settings['trust_env'],
                                             timeout=settings['timeout']))
    try:
        while True:
            request = pipe.recv()
            if request is None:
                return
            try:
                response = client.chat.completions.create(**request)
                pipe.send({'ok': True, 'response': response.model_dump()})
            except Exception as exc:
                pipe.send({'ok': False, 'category': classify_error(exc),
                           'chain': error_chain(exc), 'status': getattr(exc, 'status_code', None)})
                # A failed connection is not reused by subsequent retries.
                return
    except (EOFError, BrokenPipeError):
        pass
    finally:
        client.close()
        pipe.close()


class BoundedApiTransport:
    def __init__(self, base_url, api_key, timeout, trust_env=True, worker=_serve):
        self.settings = dict(base_url=base_url, api_key=api_key, timeout=timeout, trust_env=trust_env)
        self.timeout, self.worker = timeout, worker
        self.process = self.pipe = None

    def close(self):
        if self.process is not None:
            if self.process.is_alive():
                self.process.terminate()
            self.process.join(timeout=5)
            if self.process.is_alive():
                self.process.kill()
                self.process.join(timeout=5)
            self.process.close()
            self.process = None
        if self.pipe is not None:
            self.pipe.close()
            self.pipe = None

    def create(self, **request):
        if self.process is None or not self.process.is_alive():
            self.close()
            context = mp.get_context('spawn')
            self.pipe, child_pipe = context.Pipe()
            self.process = context.Process(target=self.worker, args=(child_pipe, self.settings), daemon=True)
            self.process.start()
            child_pipe.close()
        self.pipe.send(request)
        if not self.pipe.poll(self.timeout):
            self.close()
            raise TransportFailure('api_timeout', ['TotalRequestDeadline'])
        try:
            result = self.pipe.recv()
        except (EOFError, OSError) as exc:
            self.close()
            raise TransportFailure('api_connection', [type(exc).__name__]) from exc
        if not result['ok']:
            self.close()
            raise TransportFailure(result['category'], result['chain'], result['status'])
        from openai.types.chat import ChatCompletion
        return ChatCompletion.model_validate(result['response'])
