#!/usr/bin/env python3
"""Record what test modules make the Bridge do, and compare two recordings: the check that a
behaviour-preserving change preserved it.

    python3 -B scripts/golden_trace.py record OUT [--jobs N] [--scratch DIR] MODULE ...
    python3 -B scripts/golden_trace.py compare BEFORE AFTER [--noise BEFORE2] [--op] [--show N] [--width N]

``record`` runs each test module (a name under tests/, or the path of a .py file elsewhere) in a
child process of its own, ``--jobs`` at a time, and writes OUT/<module>.json: each test's outcome
and, per test, the events it caused in order, each with the thread that caused it (its name with
digits as N; a second thread of that name in the test, by first event, is NAME@2, and so on):

- feishu: the outermost call of a thread into a Messenger method (every public one but the pure
  helpers in PURE), its arguments and its return or exception; feishu-fake: a call to a mock a test
  put in place of a Messenger method (the mock's own API is untouched);
- log: every Runtime.log line;
- write: every os.replace or os.rename into the scratch dir (the atomic writes), the target's mode
  and content;
- create: every file os.open(O_CREAT) made there (not the dotted *.tmp files of the atomic
  writes); mkdir: every os.mkdir there, the mode asked for and the mode it got; remove: os.unlink,
  os.remove;
- written: a file opened there for writing in another way (os.open's write flags, open(),
  Path.write_text, appends): its mode and content when it is removed, when the directory it is in is
  removed, or else when the test ends;
- popen: every subprocess.Popen argv, cwd and start_new_session;
- tree: when a directory under the scratch dir is removed (shutil.rmtree, which TemporaryDirectory
  uses), every directory and file under its runs/, queued/ and jobs/ (name, mode, content), so
  what another process (a real task runner) wrote is compared too.

Randomness is pinned per test (secrets.token_hex and token_urlsafe, uuid.uuid4, random, tempfile's
names and mkdtemp's numbers, the dates of git commits tests make); paths under the scratch dir, the exec dir and the repo become <S>, <X> and <R> (also as a
Claude project folder name spells them); epoch seconds, dates, HH:MM, elapsed milliseconds, time_ns
values, pids and the other run-specific values in NOISE are folded. Two recordings of the same
tree then agree up to the order of events from different threads and a few tests whose events
depend on timing; record the base twice and pass the second one as ``--noise`` to set those apart.

The tests' temp files go to a numbered slot under /dev/shm/golden-trace-<uid>/ (the first free
one, so consecutive recordings see the same paths; the owner allowed /dev/shm for test temp files),
or under ``--scratch DIR``; files a test executes (support.exec_tempdir) go next to OUT. Both are
removed afterwards. OUT, the scratch and exec dirs must be outside the repo, $HOME and /tmp.

``compare`` matches the two recordings test by test and prints, per differing test, whether the
outcome or the events differ and the first differing events. Kinds: order, only how threads interleave
differs (a thread alone with its name did the same in the same order; threads sharing a name, a pool,
did the same events between them; not counted); sequence, the same events but a thread's in another
order (a record write and a send swapped); threads, the same events but for
which thread caused which; content. With ``--noise`` a test that also differs between BEFORE and
BEFORE2, by more than the interleaving, is reported as noise and not counted. ``--op`` also folds op ids (<time_ns>-<hex8>), random in a real
task runner. Exit status: 0 no real difference, 1 some, 2 bad arguments.

Not a test suite: tests/test_tooling_scripts.py only smoke-tests it. See docs/testing.md.
"""
from __future__ import annotations

import argparse
import builtins
import difflib
import hashlib
import inspect
import io
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FORBIDDEN = (Path.home(), Path("/tmp"), Path("/var/tmp"))  # Never OUT, scratch or exec dirs (nor REPO).
SHM = Path("/dev/shm")
META = "golden-trace.json"  # OUT's own file: the modules and their exit codes, not compared.
MODULE_TIMEOUT = 900  # Seconds per test module.
PURE = frozenset({"key", "safe_key", "message_format", "chunk_limit"})  # Messenger methods that call nothing.

# Run-specific text the recorder folds (and compare folds again in older recordings): time_ns values,
# pids, inodes, event ids made of times, keys of a random secret, the worktree's HEAD, epoch ms, process
# start ticks, timestamps in file names, HH:MM:SS whose hour norm_text kept, the release a runner names.
NOISE = [(re.compile(r"(?<!\d)1[78]\d{17}(?!\d)"), "<NS>"), (re.compile(r"pid=\d+"), "pid=<P>"),
         (re.compile(r'"pid": \d+'), '"pid": "<P>"'), (re.compile(r'bridge\.pid", \d+'), 'bridge.pid", "<P>"'),
         (re.compile(r'bridge\.pid", "0o\d+", \d+'), 'bridge.pid", "<P>"'), (re.compile(r'"inode": \d+'), '"inode": "<I>"'),
         (re.compile(r"event=(?:<T>|\d+)"), "event=<E>"), (re.compile(r"verified-results/[0-9a-f]{64}"), "verified-results/<RK>"),
         (re.compile(r'"((?:verified_)?(?:result_key|authorization))": "[^"]*"'), r'"\1": "<RK>"'),
         (re.compile(r"(?<![0-9a-f])[0-9a-f]{40}(?![0-9a-f])"), "<SHA>"),
         (re.compile(r'"child": \d+'), '"child": "<P>"'), (re.compile(r"(?<!\d)1[78]\d{11}(?!\d)"), "<TMS>"),
         (re.compile(r"release=[\w.-]+"), "release=<REL>"),
         (re.compile(r'"start_ticks": "\d+"'), '"start_ticks": "<TK>"'),
         (re.compile(r"(?<!\d)20\d{6}-\d{6}(?!\d)"), "<STAMP>"),
         (re.compile(r"(?<![\d.])\d\d:<HH:MM>"), "<HMS>")]
OP = (re.compile(r"<NS>-[0-9a-f]{8}(?![0-9a-f])"), "<OP>")


def denoise(text: str, ops: bool = False) -> str:
    for pattern, by in NOISE + ([OP] if ops else []):
        text = pattern.sub(by, text)
    return text


def escaped(path: str) -> list[str]:
    """``path`` as cards and notes show it (test\\_launch, golden&#45;trace): core/text.py's and
    cards/review.py's Markdown escapes and cards/directory.py's ``literal`` entities, copied here because
    the child pins randomness before any Bridge module is imported; each also as JSON strings nested up to
    three deep spell it (a card's JSON test\\\\_launch, that JSON in lark-cli's --data)."""
    shown = [re.sub(r"([`*_\[\]\\])", r"\\\1", path), re.sub(r"([\\`*_{}\[\]()#+.!|~-])", r"\\\1", path),
             "".join(f"&#{ord(c)};" if c in "&<>\\`*_{}[]()#+.!|~-" else c for c in path)]
    nested = list(shown)
    for _ in range(3):
        nested = [json.dumps(text, ensure_ascii=False)[1:-1] for text in nested]
        shown += nested
    return shown


def utf8(text: str) -> str:
    """``text`` with any lone surrogate (text a test decoded with surrogateescape) as its \\udcxx escape,
    which UTF-8 can write and, inside a JSON string, json.loads reads back as the same character."""
    return text.encode("utf-8", "backslashreplace").decode("utf-8")


def allowed(path: Path) -> bool:
    """False inside REPO, $HOME or a system temp dir (as given or resolved)."""
    path = Path(path).absolute()
    return not any(place.is_relative_to(base) for place in (path, path.resolve())
                   for root in (REPO, *FORBIDDEN) for base in (root, root.resolve()))


# ---------------------------------------------------------------- record: the child


def child(module: str, out: Path, scratch: Path, exec_dir: Path) -> None:
    """Run one test module in this process with the recorders in place; write OUT and exit."""
    import random
    import secrets
    import tempfile
    import unittest
    import uuid

    os.chdir(REPO)
    sys.path[:0] = [str(REPO), str(REPO / "tests")]
    if module.endswith(".py"):  # A module outside tests/ (the smoke test's): its folder first. record made it absolute.
        sys.path.insert(0, str(Path(module).parent))
        module = Path(module).stem
    for folder in (scratch, exec_dir):
        if folder.exists():
            shutil.rmtree(folder)
        folder.mkdir(parents=True)
    os.environ["TMPDIR"] = str(scratch)
    os.environ["BRIDGE_TEST_EXEC_TMPDIR"] = str(exec_dir)
    os.environ["GIT_AUTHOR_DATE"] = os.environ["GIT_COMMITTER_DATE"] = "2026-01-01T00:00:00+00:00"  # Commit ids.
    tempfile.tempdir = str(scratch)  # Set, not probed: gettempdir's probe writes a file with an unpinned name.

    state = {"test": "<import>", "hex": 0, "uuid": 0, "dirs": 0, "seats": {}}
    events: dict[str, list] = {}
    outcome: dict[str, str] = {}
    lock = threading.Lock()
    local = threading.local()
    prefix = str(scratch)
    places = sorted([(str(scratch), "<S>"), (str(exec_dir), "<X>"), (str(REPO), "<R>")], key=lambda p: -len(p[0]))
    places += [(re.sub(r"[^A-Za-z0-9]", "-", path), f"<{mark[1]}->") for path, mark in places]  # Claude's folder names.
    places += [(shown, mark) for path, mark in places[:3] for shown in escaped(path) if shown != path]
    places = sorted(dict(places).items(), key=lambda p: -len(p[0]))

    def norm_text(text: str) -> str:
        for path, mark in places:
            text = text.replace(path, mark)
        text = re.sub(r"(elapsed_ms|_ms|ms)=\d+(\.\d+)?", r"\1=N", text)
        text = re.sub(r"\b\d{4}-\d\d-\d\d[ T]\d\d:\d\d(:\d\d(\.\d+)?)?([+-]\d\d:?\d\d|Z)?", "<DATE>", text)
        text = re.sub(r"(?<![\d.])\d\d:\d\d(?![\d:])", "<HH:MM>", text)
        text = re.sub(r"(?<![\d.])1[6-9]\d{8}(\.\d+)?(?![\d])", "<T>", text)
        return text

    def norm(value):
        if isinstance(value, str):
            return norm_text(value)
        if isinstance(value, bool) or value is None:
            return value
        if isinstance(value, (int, float)):
            return "<T>" if 1.5e9 < value < 2.5e9 else value
        if isinstance(value, dict):
            return {norm_text(str(k)): ("<N>" if str(k).endswith("_ms") and isinstance(v, (int, float))
                                        and not isinstance(v, bool) else norm(v)) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [norm(v) for v in value]
        if isinstance(value, Path):
            return norm_text(str(value))
        if callable(value):
            return "<fn>"
        topic = getattr(value, "topic_id", None)
        return f"<{type(value).__name__}{':' + str(topic) if topic else ''}>"

    def emit(kind, *data):
        name = re.sub(r"\d+", "N", threading.current_thread().name)
        with lock:
            if getattr(local, "seat", None) is None or local.seat[0] != state["test"]:  # This thread's first event.
                seats = state["seats"]
                seats[name] = seats.get(name, 0) + 1
                local.seat = (state["test"], seats[name])
            number = local.seat[1]
            events.setdefault(state["test"], []).append([name if number == 1 else f"{name}@{number}", kind,
                                                         *[norm(d) for d in data]])

    # Pinned randomness, before the Bridge's modules import anything from these.
    def token_hex(nbytes=None):
        nbytes = 32 if nbytes is None else nbytes
        with lock:
            state["hex"] += 1
            seed = f"{state['test']}:hex:{state['hex']}"
        out_hex = ""
        while len(out_hex) < 2 * nbytes:
            out_hex += hashlib.sha256((seed + str(len(out_hex))).encode()).hexdigest()
        return out_hex[:2 * nbytes]

    def token_urlsafe(nbytes=None):
        return token_hex(nbytes)[: (32 if nbytes is None else nbytes)]

    def uuid4():
        with lock:
            state["uuid"] += 1
            seed = f"{state['test']}:uuid:{state['uuid']}"
        return uuid.UUID(hashlib.sha256(seed.encode()).hexdigest()[:32], version=4)

    def mkdtemp(suffix=None, prefix=None, dir=None):  # Numbered per test: one more dir leaves later tests' paths.
        with lock:
            state["dirs"] += 1
            number, test = state["dirs"], state["test"]
        base = Path(dir) if dir is not None else scratch
        path = base / f"{prefix or 'tmp'}{hashlib.sha256(test.encode()).hexdigest()[:6]}w{number}{suffix or ''}"
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
        os.mkdir(path, 0o700)
        return str(path.absolute())

    class Names(tempfile._RandomNameSequence):  # mkstemp's and NamedTemporaryFile's names, per test.
        def __init__(self, seed):
            self.seeded = random.Random(seed)

        @property
        def rng(self):
            return self.seeded

    secrets.token_hex = token_hex
    secrets.token_urlsafe = token_urlsafe
    uuid.uuid4 = uuid4
    tempfile.mkdtemp = mkdtemp

    def pin(test):
        with lock:
            state.update(test=test, hex=0, uuid=0, dirs=0, seats={})
        random.seed(test)
        tempfile._name_sequence = Names(test)

    pin("<import>")

    def content(path):
        try:
            text = Path(path).read_text()
        except (OSError, UnicodeDecodeError) as exc:
            return f"<unreadable {type(exc).__name__}>"
        try:
            return json.loads(text)
        except ValueError:
            return text[:4000]

    def mode(path):
        try:
            return oct(os.stat(path).st_mode & 0o7777)
        except OSError:
            return "<gone>"

    def scratch_temp(text):
        name = os.path.basename(text)
        return name.startswith(".") and name.endswith(".tmp")

    real_replace, real_rename, real_unlink, real_remove = os.replace, os.rename, os.unlink, os.remove
    real_mkdir, real_open, real_rmtree, real_io_open = os.mkdir, os.open, shutil.rmtree, io.open
    written: set[str] = set()  # Files opened for writing since their content was last recorded.
    writing = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC

    def wrote(path):
        text = os.fsdecode(path) if isinstance(path, (str, bytes, os.PathLike)) else ""
        if text.startswith(prefix) and not scratch_temp(text):
            with lock:
                written.add(os.path.abspath(text))

    def flush(under=None, skip=()):
        """A written event for each file opened for writing (under ``under``) whose content was not recorded since."""
        with lock:
            due = sorted(path for path in written if under is None or path == under
                         or path.startswith(under.rstrip(os.sep) + os.sep))
            written.difference_update(due)
        for path in due:
            if path not in skip and os.path.isfile(path):
                emit("written", path, mode(path), content(path) if os.path.basename(path) != "runner.log" else "<log>")

    def moved(real):
        def move(src, dst, *args, **kwargs):
            result = real(src, dst, *args, **kwargs)
            with lock:
                written.discard(os.path.abspath(os.fsdecode(src)))
                written.discard(os.path.abspath(os.fsdecode(dst)))
            if str(dst).startswith(prefix):
                emit("write", str(dst), mode(dst), content(dst))
            return result
        return move

    def io_open(file, mode="r", *args, **kwargs):
        result = real_io_open(file, mode, *args, **kwargs)
        if any(flag in mode for flag in "wax+"):
            wrote(file)
        return result

    def mkdir(path, mode_arg=0o777, *args, **kwargs):
        result = real_mkdir(path, mode_arg, *args, **kwargs)
        if str(path).startswith(prefix):
            emit("mkdir", str(path), oct(mode_arg), mode(path))
        return result

    def open_(path, flags, mode_arg=0o777, *args, **kwargs):
        text = os.fsdecode(path) if isinstance(path, (str, bytes, os.PathLike)) else ""
        new = bool(flags & os.O_CREAT) and not kwargs.get("dir_fd") and text.startswith(prefix) \
            and not scratch_temp(text) and not os.path.exists(text)
        fd = real_open(path, flags, mode_arg, *args, **kwargs)
        if new:
            emit("create", text, oct(flags & ~os.O_CLOEXEC), oct(mode_arg), mode(text))
        if flags & writing and not kwargs.get("dir_fd"):
            wrote(text)
        return fd

    def snapshot(path):
        """The tree event; returns the files it holds."""
        root = Path(path)
        found, files_seen = [], set()
        for folder, dirs, files in os.walk(root):
            dirs.sort()
            parts = Path(os.path.relpath(folder, root)).parts
            if not any(p in {"runs", "queued", "jobs"} for p in parts):
                continue
            if "jobs" in parts and any(p.startswith(".") for p in parts):
                continue  # jobs/.locks, .index
            rel = os.path.relpath(folder, root)
            found.append(["dir", rel, mode(folder)])
            for name in sorted(files):
                if scratch_temp(name):
                    continue
                file = Path(folder) / name
                files_seen.add(os.path.abspath(file))
                found.append(["file", os.path.join(rel, name), mode(file),
                              content(file) if name != "runner.log" else "<log>"])
        if found:
            emit("tree", str(root), found)
        return files_seen

    def rmtree(path, *args, **kwargs):
        if str(path).startswith(prefix) and os.path.isdir(path):
            emit("rmtree", str(path))
            seen = set()
            try:
                seen = snapshot(path)
            except Exception as exc:  # noqa: BLE001 - the recording goes on.
                emit("tree-error", str(path), type(exc).__name__)
            flush(os.path.abspath(path), seen)
        return real_rmtree(path, *args, **kwargs)

    def removal(real):
        def remove(path, *args, **kwargs):
            if isinstance(path, (str, bytes, os.PathLike)) and not kwargs.get("dir_fd"):
                flush(os.path.abspath(os.fsdecode(path)))
            result = real(path, *args, **kwargs)
            if str(path).startswith(prefix) and not scratch_temp(str(path)):
                emit("remove", str(path))
            return result
        return remove

    os.replace, os.rename = moved(real_replace), moved(real_rename)
    os.unlink, os.remove = removal(real_unlink), removal(real_remove)
    os.mkdir, os.open, shutil.rmtree = mkdir, open_, rmtree
    io.open = builtins.open = io_open
    real_popen_init = subprocess.Popen.__init__

    def popen_init(self, args, *rest, **kwargs):
        emit("popen", [str(a) for a in args] if isinstance(args, (list, tuple)) else str(args),
             str(kwargs.get("cwd")), bool(kwargs.get("start_new_session")))
        return real_popen_init(self, args, *rest, **kwargs)

    subprocess.Popen.__init__ = popen_init

    import importlib
    importlib.import_module("support")  # Clears the Bridge's environment variables, as every test module does.
    from covey.app.runtime import Runtime
    from covey.feishu.messenger import Messenger

    # Outbound Feishu: the outermost Messenger call of each thread, its arguments and its return.
    outbound = sorted(name for name, value in vars(Messenger).items()
                      if callable(value) and not name.startswith("_") and name not in PURE)
    for name in outbound:
        real = getattr(Messenger, name)
        signature = inspect.signature(real)

        def wrapper(self, *args, __real=real, __name=name, __sig=signature, **kwargs):
            depth = getattr(local, "depth", 0)
            local.depth = depth + 1
            try:
                result = __real(self, *args, **kwargs)
            except BaseException as exc:
                if depth == 0:
                    bound = __sig.bind_partial(self, *args, **kwargs).arguments
                    bound.pop("self", None)
                    emit("feishu", __name, dict(sorted(bound.items())), f"raised {type(exc).__name__}: {exc}")
                raise
            finally:
                local.depth = depth
            if depth == 0:
                bound = __sig.bind_partial(self, *args, **kwargs).arguments
                bound.pop("self", None)
                emit("feishu", __name, dict(sorted(bound.items())), result)
            return result

        setattr(Messenger, name, wrapper)

    # Mocks tests put in place of a Messenger's methods: recorded through a proxy that leaves the mock's own
    # API (call_args_list, assert_*, side_effect) as it was.
    class Recorded:
        def __init__(self, name, target):
            object.__setattr__(self, "_name", name)
            object.__setattr__(self, "_target", target)

        def __call__(self, *args, **kwargs):
            try:
                result = self._target(*args, **kwargs)
            except BaseException as exc:
                emit("feishu-fake", self._name, list(args), dict(sorted(kwargs.items())),
                     f"raised {type(exc).__name__}: {exc}")
                raise
            emit("feishu-fake", self._name, list(args), dict(sorted(kwargs.items())), result)
            return result

        def __getattr__(self, attr):
            return getattr(self._target, attr)

        def __setattr__(self, attr, value):
            setattr(self._target, attr, value)

    def setattr_hook(self, attr, value):
        if attr in outbound and not isinstance(value, Recorded):
            value = Recorded(attr, value)
        object.__setattr__(self, attr, value)

    Messenger.__setattr__ = setattr_hook
    real_log = Runtime.log

    def log(self, message):
        emit("log", str(message))
        return real_log(self, message)

    Runtime.log = log

    class Result(unittest.TextTestResult):
        def startTest(self, test):
            pin(test.id())
            super().startTest(test)

        def stopTest(self, test):
            flush()  # Files the test wrote and left (not in a removed dir).
            super().stopTest(test)

        def addSuccess(self, test):
            outcome[test.id()] = "ok"
            super().addSuccess(test)

        def addFailure(self, test, err):
            outcome[test.id()] = "FAIL"
            super().addFailure(test, err)

        def addError(self, test, err):
            outcome[test.id()] = "ERROR"
            super().addError(test, err)

        def addSkip(self, test, reason):
            outcome[test.id()] = "skip"
            super().addSkip(test, reason)

    suite = unittest.defaultTestLoader.loadTestsFromName(module)
    unittest.TextTestRunner(stream=sys.stderr, resultclass=Result, verbosity=0).run(suite)
    with lock:
        state["test"] = "<after>"
        text = json.dumps({"outcome": outcome, "events": events}, ensure_ascii=False, indent=1,
                          default=lambda v: f"<{type(v).__name__}>")
    out.write_text(utf8(denoise(text)) + "\n")
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)  # Leftover daemon threads must not write after the dump.


# ---------------------------------------------------------------- record: the parent


def tmpfs(path) -> bool:
    try:
        return any(line.split()[1:3] == [str(path), "tmpfs"] for line in Path("/proc/mounts").read_text().splitlines())
    except OSError:
        return False


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def claim(base: Path) -> Path:
    """The first free slot base/s<N>, made by this process (a slot whose owner is gone is taken over)."""
    base.mkdir(parents=True, exist_ok=True)
    for number in range(1000):
        slot = base / f"s{number}"
        try:
            os.mkdir(slot, 0o700)
        except FileExistsError:
            try:
                owner = int((slot / "owner.pid").read_text())
            except (OSError, ValueError):
                owner = 0
            if owner and alive(owner) or not owner and time.time() - slot.stat().st_mtime < 60:
                continue  # In use (or just being made).
            shutil.rmtree(slot, ignore_errors=True)
            try:
                os.mkdir(slot, 0o700)
            except FileExistsError:
                continue
        (slot / "owner.pid").write_text(str(os.getpid()))
        return slot
    raise SystemExit(f"no free slot under {base}")


def module_name(module: str) -> str:
    return Path(module).stem if module.endswith(".py") else module


def record(args) -> int:
    out = Path(args.out).absolute()
    if args.scratch:
        scratch_base = Path(args.scratch).absolute()
    elif tmpfs(SHM) and os.access(SHM, os.W_OK):
        scratch_base = SHM / f"golden-trace-{os.getuid()}"
    else:
        scratch_base = out.parent / f".golden-trace-scratch-{os.getuid()}"
    for path, what in ((out, "OUT"), (scratch_base, "--scratch"), (out.parent, "OUT's parent (the exec dir)")):
        if not allowed(path):
            print(f"golden_trace: {what} {path} is inside the repo, $HOME or /tmp; give another place", file=sys.stderr)
            return 2
    modules = [str(Path(m).resolve()) if m.endswith(".py") else m for m in args.modules]  # The caller's directory.
    for module in modules:
        own = REPO / "tests" / f"{module_name(module)}.py"
        if module.endswith(".py") and own.exists() and own.resolve() != Path(module):
            print(f"golden_trace: {module} has the name of {own}; rename it", file=sys.stderr)
            return 2
    names = [module_name(m) for m in modules]
    if len(set(names)) != len(names):
        print("golden_trace: two modules with one name", file=sys.stderr)
        return 2
    out.mkdir(parents=True, exist_ok=True)
    slot = claim(scratch_base)
    exec_base = out.parent / f".golden-trace-exec-{os.getuid()}-{slot.name}"  # Not noexec like /dev/shm.
    print(f"golden_trace: test temp files in {slot}, executables in {exec_base} (both removed afterwards)", file=sys.stderr)
    pending, running, codes = modules, [], {}
    main_thread = threading.current_thread() is threading.main_thread()
    previous = signal.signal(signal.SIGTERM, interrupted) if main_thread else None
    try:
        while pending or running:
            while pending and len(running) < max(1, args.jobs):
                module = pending.pop(0)
                name = module_name(module)
                log = open(out / f"{name}.log", "w")
                (out / f"{name}.json").unlink(missing_ok=True)
                proc = subprocess.Popen([sys.executable, "-B", str(Path(__file__).resolve()), "child", module,
                                         str(out / f"{name}.json"), str(slot / name), str(exec_base / name)],
                                        cwd=REPO, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                        start_new_session=True)
                running.append((name, proc, log, time.time()))
            name, proc, log, start = running[0]  # Left in `running` until reaped: an interrupt kills it in finally.
            try:
                code = proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                if time.time() - start < MODULE_TIMEOUT:
                    running.append(running.pop(0))
                    continue
                code = "timeout"
            with_group(proc)
            log.close()
            running.pop(0)
            if not (out / f"{name}.json").exists() and code == 0:
                code = "no output"
            codes[name] = code
            print(f"{name}: exit {code}", flush=True)
    finally:
        for _, proc, log, _ in running:
            with_group(proc)
            log.close()
        shutil.rmtree(slot, ignore_errors=True)
        shutil.rmtree(exec_base, ignore_errors=True)
        if main_thread:
            signal.signal(signal.SIGTERM, previous)
    meta = {"modules": dict(sorted(codes.items()))}
    (out / META).write_text(json.dumps(meta, indent=1) + "\n")
    return 1 if any(codes.values()) else 0


def interrupted(signum, frame):
    raise KeyboardInterrupt(f"signal {signum}")


def with_group(proc) -> None:
    """Kill what is left of a child's process group (its tests' leftovers)."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        pass
    proc.wait()


# ---------------------------------------------------------------- compare


def load(folder, ops):
    return {p.stem: json.loads(denoise(p.read_text(), ops)) for p in sorted(Path(folder).glob("*.json"))
            if p.name != META}


def key(event):
    return utf8(json.dumps(event, ensure_ascii=False))


def thread_name(label: str) -> str:
    return label.rsplit("@", 1)[0] if re.search(r"@\d+$", label) else label


def per_thread(events):
    """Thread name -> its threads' event lists, by label."""
    named = {}
    for event in events:
        named.setdefault(thread_name(event[0]), {}).setdefault(event[0], []).append(key(event[1:]))
    return named


def interleaved(a, b) -> bool:
    """True when the events differ only in how threads interleave: a thread alone with its name did the same in
    the same order; for a name several threads share in either test (a pool, the card lanes), which of them took
    which piece of work is timing too, so only the events of that name are compared, not their order."""
    ta, tb = per_thread(a), per_thread(b)
    if ta.keys() != tb.keys():
        return False
    for name, threads in ta.items():
        if len(threads) > 1 or len(tb[name]) > 1:
            if sorted(e for lst in threads.values() for e in lst) != sorted(e for lst in tb[name].values() for e in lst):
                return False
        elif list(threads.values()) != list(tb[name].values()):
            return False
    return True


def differ(a, b):
    """None when the same; 'order' when they differ only in how threads interleave (see interleaved; the one kind
    not counted); 'sequence' when the same events but a thread's in another order (a record write and a Feishu
    send swapped); 'threads' when the same events but for which thread had which (two threads writing one record
    race); else 'content'."""
    if a == b:
        return None
    if interleaved(a, b):
        return "order"
    if sorted(key([thread_name(e[0]), *e[1:]]) for e in a) == sorted(key([thread_name(e[0]), *e[1:]]) for e in b):
        return "sequence"
    if sorted(key(e[1:]) for e in a) == sorted(key(e[1:]) for e in b):
        return "threads"
    return "content"


def differences(before, after):
    result = {}
    for module in sorted(set(before) | set(after)):
        if module not in before or module not in after:
            result[module] = "missing"
            continue
        a, b = before[module], after[module]
        for test in sorted(set(a["outcome"]) | set(b["outcome"]) | set(a["events"]) | set(b["events"])):
            oa, ob = a["outcome"].get(test), b["outcome"].get(test)
            ea, eb = a["events"].get(test, []), b["events"].get(test, [])
            kind = "outcome" if oa != ob else differ(ea, eb)
            if kind:
                result.setdefault(module, {})[test] = (kind, oa, ob, ea, eb)
    return result


def compare(args) -> int:
    for folder in filter(None, (args.before, args.after, args.noise)):
        if not Path(folder).is_dir():
            print(f"golden_trace: {folder} is not a directory", file=sys.stderr)
            return 2
    before, after = load(args.before, args.op), load(args.after, args.op)
    diffs = differences(before, after)
    noise = differences(before, load(args.noise, args.op)) if args.noise else {}
    tests = sum(len(m["outcome"]) for m in before.values())
    events = sum(len(e) for m in before.values() for e in m["events"].values())
    print(f"modules {len(before)} tests {tests} events {events}")
    real = 0
    for module, items in diffs.items():
        if items == "missing":
            print(f"MISSING {module} ({'after' if module in before else 'before'})")
            real += 1
            continue
        for test, (kind, oa, ob, ea, eb) in items.items():
            seen = noise[module].get(test) if isinstance(noise.get(module), dict) else None
            noisy = kind != "order" and seen is not None and seen[0] != "order"  # An interleaving excuses nothing.
            if not noisy and kind != "order":
                real += 1
            print(f"{('noise' if noisy else kind).upper()} {test} outcome {oa}->{ob} events {len(ea)}->{len(eb)}")
            if kind in ("content", "sequence", "threads") and not noisy and args.show:
                sa, sb = [key(e) for e in ea], [key(e) for e in eb]
                shown = 0
                for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, sa, sb, autojunk=False).get_opcodes():
                    if op == "equal" or shown >= args.show:
                        continue
                    shown += 1
                    print(f"   {op} before[{i1}:{i2}] after[{j1}:{j2}]")
                    for line in sa[i1:i2]:
                        print(f"   - {line[:args.width]}")
                    for line in sb[j1:j2]:
                        print(f"   + {line[:args.width]}")
    print(f"real differences: {real}")
    return 1 if real else 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="golden_trace.py", description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    rec = commands.add_parser("record", help="run test modules with recorders and write OUT/<module>.json")
    rec.add_argument("out", help="output directory (outside the repo, $HOME and /tmp)")
    rec.add_argument("--jobs", type=int, default=4, help="modules run at once (default: 4)")
    rec.add_argument("--scratch", help="base for the tests' temp files (default: /dev/shm/golden-trace-<uid>)")
    rec.add_argument("modules", nargs="+", help="test modules under tests/ (test_cards) or paths of .py files")
    cmp_ = commands.add_parser("compare", help="compare two recordings test by test")
    cmp_.add_argument("before")
    cmp_.add_argument("after")
    cmp_.add_argument("--noise", help="a second recording of BEFORE's tree: tests differing there too are noise")
    cmp_.add_argument("--op", action="store_true", help="also fold op ids (<time_ns>-<hex8>)")
    cmp_.add_argument("--show", type=int, default=3, help="differing stretches shown per test (default: 3)")
    cmp_.add_argument("--width", type=int, default=400, help="characters shown per event (default: 400)")
    one = commands.add_parser("child", help="internal: one module with the recorders, for record")
    one.add_argument("module")
    one.add_argument("out")
    one.add_argument("scratch")
    one.add_argument("exec_dir")
    args = parser.parse_args(argv)
    if args.command == "child":
        child(args.module, Path(args.out), Path(args.scratch), Path(args.exec_dir))
        return 0
    return record(args) if args.command == "record" else compare(args)


if __name__ == "__main__":
    sys.exit(main())
