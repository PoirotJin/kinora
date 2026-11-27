"""Small, versioned contracts shared by the UI, model driver and motion tools."""
import hashlib
import json
import math
import re
import uuid
from dataclasses import dataclass


DEFAULT_BUDGET = dict(max_attempts=8, max_steps=24, max_candidates=8, max_cost_usd=0.0)
TERMINAL_TASKS = {'accepted', 'cancelled', 'failed'}
OPERATION_STATES = {'submitting', 'waiting', 'succeeded', 'failed', 'unknown'}


class HarnessError(ValueError):
    pass


class Conflict(HarnessError):
    pass


class BudgetExceeded(HarnessError):
    pass


class LeaseLost(Conflict):
    pass


class RunBusy(Conflict):
    pass


def identifier(value, label='identifier'):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,159}', value):
        raise HarnessError('Invalid ' + label)
    return value


def new_id(prefix):
    return prefix + '_' + uuid.uuid4().hex


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def text(value, label, maximum=8000):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise HarnessError('Invalid ' + label)
    return value.strip()


def selection(value):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {'start_frame', 'end_frame'}:
        raise HarnessError('Selection requires start_frame and end_frame')
    start, end = value['start_frame'], value['end_frame']
    if type(start) is not int or type(end) is not int or not 0 <= start < end:
        raise HarnessError('Invalid half-open frame selection')
    return dict(value)


def budget(value):
    value = {} if value is None else value
    if not isinstance(value, dict) or set(value) - set(DEFAULT_BUDGET):
        raise HarnessError('Invalid budget keys')
    result = dict(DEFAULT_BUDGET, **value)
    for key in ('max_attempts', 'max_steps', 'max_candidates'):
        if type(result[key]) is not int or not 1 <= result[key] <= 1000:
            raise HarnessError('Invalid ' + key)
    cost = result['max_cost_usd']
    if type(cost) not in (int, float) or not math.isfinite(cost) or not 0 <= cost <= 100000:
        raise HarnessError('Invalid maximum cost')
    return result


def requirements(values):
    if values is None:
        return []
    if not isinstance(values, list) or len(values) > 100:
        raise HarnessError('Invalid requirements')
    result = []
    for item in values:
        if isinstance(item, str):
            item = {'text': item}
        elif isinstance(item, dict):
            item = dict(item)
        else:
            raise HarnessError('Requirement must be text or an object')
        if set(item) - {'text', 'scope', 'kind', 'persistent', 'check'}:
            raise HarnessError('Invalid requirement fields')
        r = dict(id=new_id('req'), text=text(item.get('text'), 'requirement', 2000),
                 scope=item.get('scope', 'all'), kind=item.get('kind', 'semantic'),
                 persistent=item.get('persistent', True), status='unverified')
        if r['scope'] not in {'all', 'selection'} or r['kind'] not in {'semantic', 'hard'} or type(r['persistent']) is not bool:
            raise HarnessError('Invalid requirement scope or kind')
        if 'check' in item:
            r['check'] = item['check']
        encode(r)
        result.append(r)
    return result


def validate_schema(value, schema, label='arguments'):
    """Validate the strict JSON-schema subset used by our registered tools."""
    kind = schema.get('type')
    matches = {'object': isinstance(value, dict), 'array': isinstance(value, list),
               'string': isinstance(value, str), 'integer': type(value) is int,
               'number': type(value) in (int, float) and math.isfinite(value),
               'boolean': type(value) is bool, 'null': value is None}
    if kind and not matches.get(kind, False):
        raise HarnessError('Invalid type for ' + label)
    if 'enum' in schema and value not in schema['enum']:
        raise HarnessError('Invalid choice for ' + label)
    if kind == 'object':
        missing = set(schema.get('required', [])) - set(value)
        if missing:
            raise HarnessError('Missing fields: ' + ', '.join(sorted(missing)))
        properties = schema.get('properties', {})
        if schema.get('additionalProperties', False) is False and set(value) - set(properties):
            raise HarnessError('Unknown fields for ' + label)
        for key, item in value.items():
            if key in properties:
                validate_schema(item, properties[key], label + '.' + key)
    elif kind == 'array':
        if len(value) > schema.get('maxItems', 1000):
            raise HarnessError('Too many items in ' + label)
        for item in value:
            validate_schema(item, schema.get('items', {}), label)
    elif kind in ('integer', 'number'):
        if value < schema.get('minimum', -math.inf) or value > schema.get('maximum', math.inf):
            raise HarnessError('Out of range: ' + label)
    elif kind == 'string':
        if not schema.get('minLength', 0) <= len(value) <= schema.get('maxLength', 8000):
            raise HarnessError('Invalid length for ' + label)


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict
    execute: object
    reconcile: object = None
    side_effect: bool = True
    max_cost_usd: float = 0.0


class ToolRegistry:
    def __init__(self):
        self._tools = {}

    def register(self, tool):
        identifier(tool.name, 'tool name')
        if tool.name in self._tools:
            raise Conflict('Tool already registered')
        if not callable(tool.execute) or (tool.reconcile is not None and not callable(tool.reconcile)):
            raise HarnessError('Tool requires callable execution/reconciliation')
        if type(tool.max_cost_usd) not in (int, float) or not math.isfinite(tool.max_cost_usd) or tool.max_cost_usd < 0:
            raise HarnessError('Invalid tool cost bound')
        self._tools[tool.name] = tool
        return tool

    def get(self, name):
        if name not in self._tools:
            raise HarnessError('Unknown tool: ' + str(name))
        return self._tools[name]

    def schemas(self):
        return [dict(name=t.name, description=t.description, parameters=t.input_schema,
                     side_effect=t.side_effect, max_cost_usd=t.max_cost_usd)
                for t in self._tools.values()]
