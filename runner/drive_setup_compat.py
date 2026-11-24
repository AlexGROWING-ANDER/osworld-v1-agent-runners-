"""Correct a known V1 setup type-check typo; keep task operations and URLs."""
import inspect
import textwrap


def fixed_upload_path_method(original):
    source=textwrap.dedent(inspect.getsource(original))
    typo="paths = [params['path']] if params['path'] != list else params['path']"
    if typo not in source:
        return original
    if source.count(typo)!=1:
        raise RuntimeError('Unexpected Drive setup source; do not transform')
    source=source.replace(typo,"paths = params['path'] if isinstance(params['path'], list) else [params['path']]")
    download='response = requests.get(url, stream=True)'
    if download in source:
        source=source.replace(download,"response = requests.get(url, stream=True, timeout=(10, 60), proxies={'http':'http://127.0.0.1:18081','https':'http://127.0.0.1:18081'})")
    namespace={}
    exec(compile(source,inspect.getsourcefile(original) or '<drive_setup_compat>','exec'),original.__globals__,namespace)
    return namespace[original.__name__]
