"""The wire contract between Service.qml and the daemon.

Newline-delimited JSON over a unix socket. Three shapes:

    request   {"id": 42, "cmd": "seek", "pos": 30, "mode": "relative"}
    response  {"id": 42, "ok": true, "result": {...}}
              {"id": 42, "ok": false, "error": {"code": "not-found", "message": "..."}}
    event     {"event": "player", "seq": 9911, "data": {...}}

Commands are plain functions registered with the `@command` decorator. Each
declares its arguments once; `parse_args` validates and coerces them so the
handlers can trust their inputs. Handlers may be coroutines.
"""

import inspect

MAX_LINE = 1024 * 1024

# Error codes. The QML side maps these to user-facing notices, so they are a
# small closed set rather than free text.
BAD_REQUEST = "bad-request"
NOT_FOUND = "not-found"
UNAVAILABLE = "unavailable"
RATE_LIMITED = "rate-limited"
NETWORK = "network"
CONFLICT = "conflict"
INTERNAL = "internal"


class ProtocolError(Exception):
    def __init__(self, code, message, **extra):
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra

    def to_json(self):
        payload = {"code": self.code, "message": self.message}
        payload.update(self.extra)
        return payload


class Arg:
    """One command argument: its type, whether it is required, and bounds."""

    def __init__(self, kind, required=True, default=None, choices=None, minimum=None, maximum=None):
        self.kind = kind
        self.required = required
        self.default = default
        self.choices = choices
        self.minimum = minimum
        self.maximum = maximum

    def coerce(self, name, value):
        kind = self.kind
        try:
            if kind is bool:
                if isinstance(value, str):
                    value = value.strip().lower() in ("1", "true", "yes", "on")
                else:
                    value = bool(value)
            elif kind is int:
                if isinstance(value, bool):
                    raise ValueError
                value = int(value)
            elif kind is float:
                if isinstance(value, bool):
                    raise ValueError
                value = float(value)
            elif kind is str:
                if value is None:
                    value = ""
                value = str(value)
            elif kind is list:
                if not isinstance(value, list):
                    raise ValueError
            elif kind is dict:
                if not isinstance(value, dict):
                    raise ValueError
            elif kind == "int-list":
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    value = [int(value)]
                elif isinstance(value, list):
                    value = [int(item) for item in value]
                else:
                    raise ValueError
        except (TypeError, ValueError):
            raise ProtocolError(BAD_REQUEST, "argument %r has the wrong type" % name)
        if self.choices is not None and value not in self.choices:
            raise ProtocolError(BAD_REQUEST, "argument %r must be one of %s" % (name, ", ".join(map(str, self.choices))))
        if self.minimum is not None and value < self.minimum:
            raise ProtocolError(BAD_REQUEST, "argument %r must be at least %s" % (name, self.minimum))
        if self.maximum is not None and value > self.maximum:
            raise ProtocolError(BAD_REQUEST, "argument %r must be at most %s" % (name, self.maximum))
        return value


class Command:
    def __init__(self, name, handler, args, description):
        self.name = name
        self.handler = handler
        self.args = args
        self.description = description
        self.is_coroutine = inspect.iscoroutinefunction(handler)


COMMANDS = {}


def command(name, description="", **args):
    """Register `handler(engine, client, **kwargs)` under `name`."""
    def decorate(handler):
        COMMANDS[name] = Command(name, handler, args, description)
        return handler
    return decorate


def parse_args(cmd, message):
    """Validate the flat request object against the command's declared args."""
    parsed = {}
    for name, spec in cmd.args.items():
        if name in message and message[name] is not None:
            parsed[name] = spec.coerce(name, message[name])
        elif spec.required:
            raise ProtocolError(BAD_REQUEST, "missing argument %r" % name)
        else:
            parsed[name] = spec.default
    return parsed


def lookup(name):
    cmd = COMMANDS.get(str(name))
    if cmd is None:
        raise ProtocolError(BAD_REQUEST, "unknown command %r" % str(name))
    return cmd


def describe():
    """Machine-readable command list, for `podcastd.py call --help` and tests."""
    result = []
    for name in sorted(COMMANDS):
        cmd = COMMANDS[name]
        result.append({
            "cmd": name,
            "description": cmd.description,
            "args": {
                arg: {"required": spec.required, "type": getattr(spec.kind, "__name__", str(spec.kind))}
                for arg, spec in cmd.args.items()
            },
        })
    return result
