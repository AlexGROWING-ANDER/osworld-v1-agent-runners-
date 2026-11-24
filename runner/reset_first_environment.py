"""Skip the disposable pre-reset VMware boot; retain the official reset path."""


def reset_first_environment(base: type) -> type:
    class ResetFirstEnvironment(base):
        def __init__(self, *args, **kwargs):
            self._defer_constructor_boot = kwargs.get('provider_name') == 'vmware'
            super().__init__(*args, **kwargs)

        def _start_emulator(self):
            if self._defer_constructor_boot:
                self._defer_constructor_boot = False
                return
            return super()._start_emulator()

    return ResetFirstEnvironment
