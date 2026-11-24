"""Narrow SPA-ready wait compatibility; evaluator targets/scoring unchanged."""
import inspect
import re

GETTER = 'get_gotoRecreationPage_and_get_html_content'


def rewrite_source(source: str) -> str:
    """Fail closed if the audited upstream navigation statements changed."""
    changes = [
        ('page.wait_for_load_state(\'networkidle\', timeout=30000)',
         'page.wait_for_load_state(\'domcontentloaded\', timeout=30000)'),
        ('page.wait_for_url(url=re.compile(r"recreation\\.gov/search"), timeout=20000)',
         'page.wait_for_url(url=re.compile(r"recreation\\.gov/search"), wait_until="domcontentloaded", timeout=20000)'),
        ('newpage.wait_for_load_state("load", timeout=20000)',
         'newpage.wait_for_load_state("domcontentloaded", timeout=20000)'),
    ]
    for before, after in changes:
        if source.count(before) != 1:
            raise RuntimeError('Recreation evaluator source differs from audited navigation contract')
        source = source.replace(before, after, 1)
    return source


def install() -> None:
    """Replace only the affected expected-state getter, including its export."""
    import desktop_env.evaluators.getters as getters
    from desktop_env.evaluators.getters import chrome

    original = getattr(chrome, GETTER)
    if getattr(original, '_osworld_spa_ready', False):
        return
    source = rewrite_source(inspect.getsource(original))
    namespace = dict(original.__globals__)
    exec(compile(source, '<osworld-recreation-navigation-compat>', 'exec'), namespace)
    replacement = namespace[GETTER]
    replacement._osworld_spa_ready = True
    replacement._osworld_original = original
    setattr(chrome, GETTER, replacement)
    if getattr(getters, GETTER, None) is original:
        setattr(getters, GETTER, replacement)
