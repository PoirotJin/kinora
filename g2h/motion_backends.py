"""Explicit motion model capabilities. Missing models never become silent fallbacks."""
from dataclasses import dataclass


class BackendUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class Capability:
    name: str
    kind: str
    available: bool
    reason: str = ''
    representation: str = 'human_smplh_y_up_v1'
    max_duration_s: float | None = None
    version: str = ''

    def as_dict(self):
        return dict(self.__dict__)


class MotionBackends:
    """Callbacks are supplied by the existing, authorized worker bridge.

    Registration does not load weights or call a provider. Generation and editing
    have separate contracts; the application supplies authorized callbacks.
    """
    def __init__(self, generator=None, editor=None, kimodo_generator=None):
        self._callbacks = {}
        self._capabilities = {
            'hy_motion': Capability('hy_motion', 'generate', generator is not None,
                                    '' if generator else 'Existing worker bridge is not configured',
                                    max_duration_s=12, version='HY-Motion-1.0'),
            'kimodo': Capability('kimodo', 'generate', kimodo_generator is not None,
                                 '' if kimodo_generator else 'Kimodo worker bridge is not configured',
                                 max_duration_s=120, version='Kimodo-SOMA-RP-v1.2'),
            'local_editor': Capability('local_editor', 'edit', editor is not None,
                                       '' if editor else 'No semantic editing model is configured',
                                       version='callback-v1'),
        }
        if generator is not None:
            self._callbacks['hy_motion'] = generator
        if kimodo_generator is not None:
            self._callbacks['kimodo'] = kimodo_generator
        if editor is not None:
            self._callbacks['local_editor'] = editor

    def capabilities(self):
        return [v.as_dict() for v in self._capabilities.values()]

    def invoke(self, name, **request):
        capability = self._capabilities.get(name)
        if capability is None:
            raise ValueError('Unknown motion backend')
        if not capability.available or name not in self._callbacks:
            raise BackendUnavailable(capability.reason)
        return self._callbacks[name](**request)
