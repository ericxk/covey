"""Structured delivery failures and durable, bounded idempotent CLI writes.

Receipts contain hashes/status/message IDs, never message bodies or CLI stderr.
Uncertain writes are retried only inside Feishu's one-hour UUID window. A later
attempt requires reconciliation, not a new UUID or blind transport fallback.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, asdict, replace
import hashlib
import json
from pathlib import Path
import re
import subprocess
import threading
import time

from covey.core import timing
from covey.core.locks import resource_lock
from covey.core.state import write_durable_json, read_json


def lark_argv(profile: str | None, *args: str) -> list[str]:
    if profile:
        return ["lark-cli", "--profile", profile, *args]
    return ["lark-cli", *args]


@dataclass(frozen=True)
class Failure:
    category: str
    retryable: bool = False
    ambiguous: bool = False
    retry_after: float = 0
    code: str = ''  # Feishu's own error code, e.g. CardKit 300301 for an invalid element ID.
    detail: str = ''  # Feishu's short error message, bounded; never request content.


def operation(argv):
    """The API a lark-cli call made, for logs: `PUT /open-apis/...` or `im +messages-reply`."""
    argv = list(argv)
    if 'api' in argv[:6]:
        at = argv.index('api')
        return ' '.join(argv[at + 1:at + 3])[:160]
    shortcut = next((i for i, a in enumerate(argv) if isinstance(a, str) and a.startswith('+')), None)
    return ' '.join(argv[shortcut - 1:shortcut + 1]) if shortcut else 'lark-cli'


_local = threading.local()
_root = None

# A write to a card that exists (its JSON, settings, elements, batch_update) carries its sequence and uuid, so
# another try of it changes nothing once one landed (repo review E5). On such a write only, never a message's:
# CardKit's 300308 Server Internal Error may or may not have applied it, and is tried again like a 5xx; and
# 200770 (this uuid was consumed) after a try whose outcome was unknown is that try having landed. A first
# try's 200770 is someone else's write with this uuid, and stays a failure. Adding elements (POST .../elements)
# answers such a later try with 300315 Duplicate ID instead: the elements the earlier try added are there.
CARD_WRITE = re.compile(r"(PUT|PATCH|POST|DELETE) /open-apis/cardkit/v1/cards/[^/?\s]+")
CARD_SERVER_ERROR, CARD_UUID_CONSUMED, CARD_DUPLICATE_ID = "300308", "200770", "300315"
ADD_ELEMENTS = re.compile(r"POST /open-apis/cardkit/v1/cards/[^/?\s]+/elements$")
# Feishu's DLP audit refused the message: it carries a plaintext email address or phone number, named in
# the error's message ("contain sensitive data: EMAIL_ADDRESS"). The same text is refused every time.
AUDIT_REJECTED = "230028"


# What a refusal is, in the owner's words, for the line that goes when a card could not be shown (feishu/messenger.py
# publish_card): by Feishu's code first, else by the failure's category. Feishu's own message (``detail``) follows only an
# unknown code, cut short; it never carries the request's content.
CARD_NODES_OVER, CARD_SIZE_OVER = "300305", "200860"
CODE_REASONS = {CARD_NODES_OVER: "卡片超过飞书一张卡 200 个组件的上限", CARD_SIZE_OVER: "卡片超过飞书一张卡的大小上限",
                AUDIT_REJECTED: "内容里有邮箱或手机号，被飞书的安全审核拦下"}
CATEGORY_REASONS = {"rate_limited": "飞书限流，暂时不收", "network": "网络出错，飞书没有确认",
                    "network_timeout": "飞书超时没有回应", "authentication": "机器人的飞书鉴权没过",
                    "invalid_card": "飞书认为这张卡无效", "content": "飞书不收这段内容", "validation": "飞书说请求参数不对",
                    "local_process": "本机调用飞书的命令没跑起来", "idempotency_conflict": "同一个发送键已用于别的内容",
                    "reconciliation_required": "上一次发送的结果还没核对清楚", "unconfirmed": "飞书没有确认"}
REASON_DETAIL = 60  # Characters of Feishu's own message after a code it has no words for here.


def reason(failure) -> str:
    """Why a write failed, in a few words: CODE_REASONS, else CATEGORY_REASONS, with Feishu's code in brackets
    ("卡片超过飞书一张卡 200 个组件的上限（300305）"); "飞书拒绝（code：message）" for a code not known here; "飞书没有确认"
    without a failure. Never raises."""
    if failure is None:
        return "飞书没有确认"
    code = str(getattr(failure, "code", "") or "")
    words = CODE_REASONS.get(code) or CATEGORY_REASONS.get(str(getattr(failure, "category", "") or ""))
    if words:
        return words + (f"（{code}）" if code else "")
    detail = " ".join(str(getattr(failure, "detail", "") or "").split())[:REASON_DETAIL].strip()
    return "飞书拒绝" + (f"（{code}" + (f"：{detail}" if detail else "") + "）" if code else "")


# What became of one send, for its caller's record (WAVE12 C10): SENT confirmed; NOT_SENT surely did not go (Feishu refused it,
# the CLI did not run, no card was made), so the same key may be sent again; UNCERTAIN may have gone (a timeout, an unconfirmed
# answer, an earlier try still unknown): never sent again by itself (AGENTS.md rule 1).
SENT, NOT_SENT, UNCERTAIN = "sent", "not_sent", "uncertain"


def outcome(ok: bool, failure) -> str:
    """SENT when ``ok``; UNCERTAIN without a failure, for an ambiguous one, or for ``reconciliation_required`` (an earlier try of
    the key is still unknown); else NOT_SENT."""
    if ok:
        return SENT
    if failure is None or getattr(failure, "ambiguous", False) or getattr(failure, "category", "") == "reconciliation_required":
        return UNCERTAIN
    return NOT_SENT


def card_write(argv):
    return CARD_WRITE.match(operation(argv)) is not None


def landed_before(argv, failure):
    """Whether ``failure``, on a try after one whose outcome was unknown, says that earlier try landed."""
    if failure.code == CARD_UUID_CONSUMED:
        return card_write(argv)
    return (failure.code == CARD_DUPLICATE_ID and 'Duplicate ID' in failure.detail
            and ADD_ELEMENTS.match(operation(argv)) is not None)


def configure_receipts(state_dir):
    global _root
    previous = _root.parent if _root else None
    _root = Path(state_dir) / 'delivery-receipts' if state_dir else None
    return previous


def last_failure():
    return getattr(_local, 'failure', None)


def set_failure(value):
    _local.failure = value


def classify(payload=None, *, returncode=1, exception=None):
    failure = _classify(payload, returncode=returncode, exception=exception)
    payload = payload if isinstance(payload, dict) else {}
    error = payload.get('error') or payload
    error = error if isinstance(error, dict) else {}
    code = str(error.get('code') or payload.get('code') or '')
    detail = ' '.join(str(error.get('message') or error.get('msg') or payload.get('msg') or '').split())[:120]
    return replace(failure, code='' if code == '0' else code[:20], detail=detail)


def _classify(payload=None, *, returncode=1, exception=None):
    if isinstance(exception, subprocess.TimeoutExpired):
        return Failure('network_timeout', True, True)
    if isinstance(exception, OSError):
        return Failure('local_process')
    payload = payload if isinstance(payload, dict) else {}
    error = payload.get('error') or payload
    error = error if isinstance(error, dict) else {}
    kind, subtype = error.get('type'), error.get('subtype')
    code = str(error.get('code', ''))
    status = str(error.get('status_code') or error.get('status') or '')
    try:
        delay = min(3600, max(0, float(error.get('retry_after') or payload.get('retry_after') or 0)))
    except (ValueError, TypeError):
        delay = 0
    if kind == 'rate_limit' or subtype in {'rate_limit', 'rate_limited'} or status == '429' or code == '99991400':
        return Failure('rate_limited', True, False, delay)
    if kind in {'auth', 'authentication', 'authorization', 'permission'} or status in {'401', '403'} or returncode == 3:
        return Failure('authentication')
    if kind == 'network' or status in {'500', '502', '503', '504'}:
        return Failure('network', True, True, delay)
    if subtype == 'invalid_card' or (code == '230001' and 'cardid is invalid' in str(error.get('message', '')).lower()):
        return Failure('invalid_card')
    # A generic invalid parameter is not proof the content can be reformatted.
    if subtype in {'message_too_large', 'invalid_content', 'invalid_markdown'} or (
            kind == 'validation' and error.get('param') in {'content', '--markdown', '--text'}):
        return Failure('content')
    if code == AUDIT_REJECTED:
        return Failure('audit_rejected')
    if kind == 'validation' or status == '400':
        return Failure('validation')
    if kind in {'api', 'business'} or code not in {'', '0'}:
        return Failure('api_rejected')
    return Failure('unconfirmed', True, True)


def decode(proc):
    for raw in (proc.stdout, proc.stderr):
        try:
            payload = json.loads(raw or '')
            if isinstance(payload, dict):
                if (proc.returncode == 0 and payload.get('ok') is not False
                        and payload.get('code', 0) in {0, '0'}):
                    return payload, None
                return None, classify(payload, returncode=proc.returncode)
        except (ValueError, TypeError):
            pass
    return None, classify(returncode=proc.returncode)


def _save(path, value):
    if path is not None:
        write_durable_json(path, value)  # fsync receipt and directory before a remote write


def _receipt_path(uuid):
    return _root / (hashlib.sha256(uuid.encode()).hexdigest() + '.json') if _root and uuid else None


def tried(uuid):
    """Whether a write under ``uuid`` was ever tried here (its receipt exists), whatever came of it."""
    path = _receipt_path(uuid)
    return path is not None and path.exists()


@contextmanager
def receipt(argv, uuid):
    fingerprint = hashlib.sha256(json.dumps(argv, ensure_ascii=False).encode()).hexdigest()
    # Profile/target are part of argv and its fingerprint. UUID collisions must
    # fail closed even across destinations; never reuse a key with new content.
    path = _receipt_path(uuid)
    with resource_lock('delivery-receipt', path or fingerprint):
        saved = read_json(path) if path and path.exists() else {}
        if saved and saved.get('fingerprint') != fingerprint:
            set_failure(Failure('idempotency_conflict'))
            yield None, None
            return
        saved.setdefault('fingerprint', fingerprint)
        yield path, saved


def call(argv, *, timeout=30, log=lambda _: None, uuid='', retry_safe=False, runner=None):
    """Return a decoded success or None; expose safe failure on this thread. Each call is timed
    (core/timing.py): its wall time, the process's (or SDK request's) own, attempts and result."""
    via = getattr(runner, 'via', None)  # 'sdk' from feishu/sdk.py; lark-cli otherwise.
    clock = timing.call(argv, via if isinstance(via, str) else 'cli')
    result = 'exception'
    try:
        value = _call(argv, timeout=timeout, log=log, uuid=uuid, retry_safe=retry_safe, runner=runner, clock=clock)
        failure = last_failure()
        result = (('ok' if getattr(clock, 'attempts', 1) else 'receipt') if value is not None
                  else failure.category + (':' + failure.code if failure.code else '') if failure else 'failed')
        return value
    finally:
        clock.done(result)


def last_json(text):
    """The JSON object ``text`` is (lark-cli prints it over many lines), else its last JSON line."""
    try:
        value = json.loads(text or "")
    except ValueError:
        value = None
    if isinstance(value, dict):
        return value
    for line in reversed((text or "").strip().splitlines()):
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return {}


class CliFailed(RuntimeError):
    """A read through cli_json that exited non-zero, or printed what ``expect`` refused: ``proc`` is what it
    printed, ``message`` its JSON error's message ('' without one)."""

    def __init__(self, proc):
        self.proc = proc
        error = last_json(proc.stdout).get("error")
        self.message = error.get("message") if isinstance(error, dict) else ""
        super().__init__(self.reason() or f"lark-cli rc={proc.returncode}")

    def reason(self, limit=300):
        """In one line (lark-cli prints its error as indented JSON), its last ``limit`` characters: the
        error's message, else what it printed; '' when it printed nothing."""
        return " ".join((self.message or self.proc.stderr or self.proc.stdout or "").split())[-limit:]


def cli_json(argv, *, timeout, runner=None, cwd=None, empty=None, expect=None):
    """The JSON a read-only lark-cli call printed (``argv`` from lark_argv, with its --as and --format),
    timed like every call (run_cli). CliFailed when it exits non-zero or ``expect`` (a type) refuses what it
    printed; the JSON error (ValueError) when it printed none, ``empty`` instead when it printed nothing.
    Each caller words its own failure (repo review R7: these were five hand-written copies). Reads, and the
    image upload of outgoing_images.upload_image, which carries no key: a keyed write goes through ``call`` and its
    receipt (rule 1). Holds no lock: callable from any thread."""
    proc = run_cli(argv, runner=runner, capture_output=True, text=True, timeout=timeout, check=False,
                   **({"cwd": cwd} if cwd is not None else {}))
    if proc.returncode != 0:
        raise CliFailed(proc)
    payload = empty if empty is not None and not proc.stdout else json.loads(proc.stdout)
    if expect is not None and not isinstance(payload, expect):
        raise CliFailed(proc)
    return payload


def cli_result(argv, *, timeout, cwd=None, runner=None):
    """An unkeyed lark-cli call's result as ``decode`` reads it, timed like every call (run_cli): (its JSON,
    None), or (the JSON object it printed on stdout, on stderr when stdout is empty, else None; its Failure).
    OSError and TimeoutExpired are the caller's."""
    proc = run_cli(argv, runner=runner, capture_output=True, text=True, timeout=timeout, check=False, cwd=cwd)
    payload, failure = decode(proc)
    if failure is None:
        return payload, None
    try:
        error = json.loads(proc.stdout or proc.stderr)
    except (TypeError, ValueError):
        error = None
    return (error if isinstance(error, dict) else None), failure


def run_cli(argv, *, runner=None, **kwargs):
    """``runner(argv, **kwargs)`` (``subprocess.run``, or feishu/sdk.py's request) for a lark-cli call made
    without ``call``, timed like one. Callers pass their own module's ``subprocess.run``: tests replace it there."""
    via = getattr(runner, 'via', None)
    clock = timing.call(argv, via if isinstance(via, str) else 'cli')
    result = 'exception'
    try:
        proc = clock.run(runner or subprocess.run, argv, **kwargs)
        result = 'ok' if proc.returncode == 0 else f'rc:{proc.returncode}'
        return proc
    except BaseException as exc:
        result = type(exc).__name__
        raise
    finally:
        clock.done(result)


def _call(argv, *, timeout, log, uuid, retry_safe, runner, clock):
    set_failure(None)
    with receipt(list(argv), uuid) as (path, saved):
        if saved is None:
            # Not sent: this key's receipt is of other content. Logged like a failed try, which this is not.
            log(f'delivery failed category=idempotency_conflict code=- op={operation(argv)} retryable=False '
                'attempt=0: this key was used with other content')
            return None
        if saved.get('delivered'):
            return saved.get('response') or {'ok': True}
        if saved.get('uncertain_since') and time.time() - saved['uncertain_since'] > 3500:
            set_failure(Failure('reconciliation_required'))
            log(f'delivery failed category=reconciliation_required code=- op={operation(argv)} retryable=False '
                'attempt=0: an earlier try is still unknown after 3500 s')
            return None
        for attempt in range(3 if retry_safe or uuid else 1):
            now = time.time()
            prior_uncertain = saved.get('uncertain_since')
            saved.update(uncertain_since=prior_uncertain or now, attempted_at=now)
            _save(path, saved)  # A process exit during the call is an uncertain write.
            try:
                proc = clock.run(runner or subprocess.run, argv, text=True, capture_output=True, timeout=timeout,
                                 check=False)
                result, failure = decode(proc)
            except (OSError, subprocess.TimeoutExpired) as exc:
                result, failure = None, classify(exception=exc)
            if result is None and failure.code == CARD_SERVER_ERROR and card_write(argv):
                failure = replace(failure, category='network', retryable=True, ambiguous=True)
            elif result is None and prior_uncertain is not None and landed_before(argv, failure):
                log(f'delivery confirmed code={failure.code} op={operation(argv)} attempt={attempt + 1}: '
                    'an earlier try landed')
                result = {'code': 0, 'data': {}}
            if result is not None:
                data = result.get('data', result)
                saved.update(delivered=True, response={'ok': True, 'data': {
                    'message_id': data.get('message_id', '') if isinstance(data, dict) else ''}})
                saved.pop('uncertain_since', None)
                _save(path, saved)
                set_failure(None)
                return result
            set_failure(failure)
            if not failure.ambiguous and prior_uncertain is None:
                saved.pop('uncertain_since', None)
            saved['failure'] = asdict(failure)
            _save(path, saved)
            # Code and Feishu's message say why (e.g. 300301 elementID format error); the API says which write.
            log(f'delivery failed category={failure.category} code={failure.code or "-"} op={operation(argv)} '
                f'retryable={failure.retryable} attempt={attempt + 1}' + (f' detail={failure.detail}' if failure.detail else ''))
            # Long Retry-After waits belong to the durable outbox, never a card handler.
            if not failure.retryable or not (retry_safe or uuid) or attempt == 2 or failure.retry_after > 2:
                break
            time.sleep(max(failure.retry_after, .25 * 2**attempt))
    return None


def data_of(payload):
    """Feishu's answer's ``data`` when it is an object, else the answer as it is."""
    return payload.get("data") if isinstance(payload, dict) and isinstance(payload.get("data"), dict) else payload


def message_id_of(payload) -> str:
    """The message ID Feishu's answer to a send names (``message_id`` or ``messageId``, in ``data`` or at the top),
    '' when none."""
    data = data_of(payload)
    return str(data.get("message_id") or data.get("messageId") or "") if isinstance(data, dict) else ""
