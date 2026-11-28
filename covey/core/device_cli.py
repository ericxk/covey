"""``covey device``: the command every Harness runs from its shell to use a device (core/device_access.py is the channel).

Run as bin/covey_device.py through ``<数据根>/bin/covey`` (core/home.py SHIM_TEXT). What it prints is for the model: the
command's own output as it is, and one line in Chinese for whatever the channel refused or could not reach. ``--json``
prints one JSON object instead. Exit codes: a ``run``'s is the device command's (124 when it timed out); 3 refused by a
guard; 4 the device did not answer; 2 a wrong command line.
"""
from __future__ import annotations

import argparse
import json
import sys

from covey.core import device_access as access
from covey.core import device_transport as dt
from covey.core import devices
from covey.core.state import read_object
from covey.core.topics import topic_file

REFUSED, UNREACHABLE, TIMED_OUT = 3, 4, 124
# A turn held to reading whose sandbox keeps its shell off the devices (Codex's) has their reading tools instead
# (core/device_tools.py): said where its command could not reach a device.
HELD_HINT = ("[covey device] 这一轮只读，命令行可能连不上设备：改用 bridge_workspace 的 device_list、device_ls、device_stat、"
             "device_get 工具")
EPILOG = """\
例子：
  covey device list
  covey device run gpu1 -- nvidia-smi
  covey device run gpu1 --dir ~/proj --timeout 600 -- make test
  covey device run gpu1 -- sh -c 'cd ~/proj && git pull && make'   # 只有可写设备能跑 shell
  covey device get gpu1 ~/proj/train.py           # 取到本话题产物目录 devices/gpu1/ 下
  covey device put gpu1 ./train.py ~/proj/train.py  # 取回过的文件会核对没被改过；覆盖没取回过的文件加 --write
  covey device job start gpu1 --dir ~/proj -- python train.py   # 长任务：断线、一轮结束都不停
  covey device job log gpu1 job-0123456789 [偏移]   # 按偏移补读输出，末尾给下一次的偏移
  covey device job stop gpu1 job-0123456789

只读设备（devices.json 的 access 不是 write）和只读的轮次只能 list、ls、stat、get 和只读命令（不经 shell）：
  {commands}
  git 只能 {git}；find 要带 -maxdepth（≤{depth}）、不能 -exec/-delete；tail 不能 -f；put、job 一律不行。
危险命令（rm -r、kill/pkill、systemctl/launchctl stop|restart|disable、reboot、docker rm|stop、git push --force、
git reset --hard、卸载包等，sudo、env、sh -c 里的也算）run 和 job start 都先拦下：先用问题卡问主人（这道题给一个 id），
主人同意后带 --approved <那道题的 id> 重跑；只认本话题里主人本人的回答。
退出码：run 的是设备上命令的（超时 124）；3 被护栏拦下；4 设备没有答复；2 命令行写错。
"""
APPROVED_HELP = "危险命令要主人先答应：本话题问题卡里主人答过的那道题的 id（答案号）"


def parser() -> argparse.ArgumentParser:
    epilog = EPILOG.format(commands=" ".join(access.READ_COMMANDS), git="/".join(access.GIT_READ), depth=access.FIND_DEPTH)
    top = argparse.ArgumentParser(prog="covey_device.py", description="covey device：从主目录用别的设备（devices.json 里的设备）",
                                  epilog=epilog, formatter_class=argparse.RawDescriptionHelpFormatter)
    subs = top.add_subparsers(dest="op", required=True)

    def sub(name, text):
        found = subs.add_parser(name, help=text, description=text)
        found.add_argument("--json", action="store_true", help="打印一个 JSON 对象")
        return found

    sub("list", "列出设备：能不能用、可写还是只读")
    run = sub("run", "在设备上跑一条命令（-- 后面是命令和参数）")
    run.add_argument("alias", help="设备别名")
    run.add_argument("--dir", default=None, help="设备上的工作目录（默认设备的家目录）")
    run.add_argument("--timeout", type=float, default=access.RUN_SECONDS, help="最多等几秒（默认 120，最多 3600）")
    run.add_argument("--approved", default="", help=APPROVED_HELP)
    run.add_argument("argv", nargs=argparse.REMAINDER, help="-- 命令 参数…")
    ls = sub("ls", "列设备上的目录")
    ls.add_argument("alias")
    ls.add_argument("path", nargs="?", default="~")
    stat = sub("stat", "看设备上的文件在不在、多大")
    stat.add_argument("alias")
    stat.add_argument("paths", nargs="+")
    get = sub("get", "把设备上的文件取到本机（默认本话题产物目录 devices/<设备>/ 下）")
    get.add_argument("alias")
    get.add_argument("remote")
    get.add_argument("local", nargs="?", default=None)
    put = sub("put", "把本机文件放到设备上（只有可写设备）")
    put.add_argument("alias")
    put.add_argument("local")
    put.add_argument("remote")
    put.add_argument("--write", action="store_true", help="设备上已有、这个话题又没取回过时，确定整个覆盖")
    job = sub("job", "长任务：start、log、status、stop")
    job.add_argument("action", choices=("start", "log", "status", "stop"))
    job.add_argument("alias")
    job.add_argument("rest", nargs=argparse.REMAINDER, help="start：-- 命令…；log：任务号 [偏移]；status、stop：任务号")
    job.add_argument("--dir", default=None, help="start 时设备上的工作目录")
    job.add_argument("--approved", default="", help=APPROVED_HELP)
    return top


VALUED = {"--dir": "dir", "--timeout": "timeout", "--approved": "approved"}


def _command(args, words) -> list:
    """The command of ``run`` or ``job start``: argparse leaves the options given after the alias in ``words`` (its
    REMAINDER takes all), so those before ``--`` (or before the first other word) are taken here into ``args``."""
    words = list(words or [])
    while words and words[0] != "--":
        name, sep, value = words[0].partition("=")
        if name == "--json":
            args.json = True
            words = words[1:]
        elif name in VALUED and (sep or len(words) > 1):
            given = value if sep else words[1]
            setattr(args, VALUED[name], float(given) if name == "--timeout" else given)
            words = words[1:] if sep else words[2:]
        else:
            break
    return words[1:] if words[:1] == ["--"] else words


def _topic_devices(turn) -> list:
    if not turn.topic:
        return []
    data = read_object(topic_file(turn.state_dir, turn.topic))
    return data.get("devices") if isinstance(data.get("devices"), list) else []


def _say(args, payload, text="") -> None:
    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
    elif text:
        print(text)


def _list_text(rows) -> str:
    if not rows:
        return "devices.json 里还没有设备（在网页设置的「设备」页加）。"
    lines = []
    for row in rows:
        state = ("可写" if row["writable"] else "只读") if row["usable"] else f"不能用：{row['why']}"
        lines.append(f"{row['alias']}\t{row['title']}\t{row['note'] or '—'}\t{state}")
    return "\n".join(lines)


def run(args, channel) -> int:
    op = args.op
    if op == "list":
        rows = channel.list()
        _say(args, {"devices": rows, "limit": channel.turn.limit}, _list_text(rows))
        return 0
    if op == "run":
        argv = _command(args, args.argv)
        found = channel.run(args.alias, argv, cwd=args.dir, timeout=args.timeout, approved=args.approved)
        if args.json:
            _say(args, found)
        else:
            sys.stdout.write(found["out"])
            sys.stderr.write(found["err"])
            if found["timed_out"] or found["code"] or found["truncated"]:
                tail = "超时被停止" if found["timed_out"] else f"退出 {found['code']}"
                cut = "，输出超过 1 MiB 已截断" if found["truncated"] else ""
                sys.stderr.write(f"\n[covey device] {args.alias}：{tail}{cut}（{found['seconds']:g} 秒）\n")
        if found["timed_out"]:
            return TIMED_OUT
        return found["code"] if isinstance(found["code"], int) and 0 <= found["code"] < 256 else 1
    if op == "ls":
        found = channel.ls(args.alias, args.path)
        lines = [f"{item['kind'][0]}\t{item.get('size', '')}\t{item['name']}" for item in found.get("entries") or []]
        more = "\n（只列了前 %d 个）" % len(lines) if found.get("truncated") else ""
        _say(args, found, f"{found.get('path')}:\n" + "\n".join(lines) + more)
        return 0
    if op == "stat":
        results = channel.stat(args.alias, args.paths)
        lines = [f"{item['path']}\t" + (("目录" if item.get("is_dir") else f"{item.get('size')} 字节")
                                        if item.get("exists") else "不存在") for item in results]
        _say(args, {"results": results}, "\n".join(lines))
        return 0
    if op == "get":
        found = channel.get(args.alias, args.remote, args.local)
        _say(args, found, f"取回 {found['path']}（{found['bytes']} 字节，sha256 {found['sha256'][:12]}…）")
        return 0
    if op == "put":
        turn = channel.turn
        if_sha = access.fetched_sha(turn.task_dir, _topic_devices(turn), args.alias, args.remote)
        found = channel.put(args.alias, args.local, args.remote, if_sha=if_sha or None, overwrite=args.write)
        word = "内容没变，没重写" if found["same"] else f"写入 {found['bytes']} 字节"
        _say(args, found, f"{args.alias}:{found['path']}：{word}")
        return 0
    if op == "job":
        return job(args, channel)
    return 2


def job(args, channel) -> int:
    rest = list(args.rest or [])
    if args.action == "start":
        found = channel.job_start(args.alias, _command(args, rest), cwd=args.dir, approved=getattr(args, "approved", ""))
        _say(args, found, f"已在 {args.alias} 上起任务 {found['job']}（进程组 {found.get('pgid')}）。"
                          f"看输出：covey device job log {args.alias} {found['job']}；停：covey device job stop {args.alias} {found['job']}")
        return 0
    if not rest:
        raise access.Refused(f"要给任务号：covey device job {args.action} {args.alias} job-…")
    name = rest[0]
    if args.action == "log":
        offset = int(rest[1]) if len(rest) > 1 and rest[1].isdigit() else 0
        found = channel.job_log(args.alias, name, offset)
        if args.json:
            _say(args, found)
        else:
            sys.stdout.write(found["text"])
            end = (f"已结束（{found['exit'].get('reason')}，退出 {found['exit'].get('code')}）" if found["ended"]
                   else f"还在跑（{found.get('state')}）")
            sys.stderr.write(f"\n[covey device] {name} {end}；下次从偏移 {found['next']} 读："
                             f"covey device job log {args.alias} {name} {found['next']}\n")
        return 0
    if args.action == "status":
        found = channel.job_status(args.alias, name)
        exit_info = found.get("exit") or {}
        _say(args, found, f"{name}：{found['state']}" + (f"，退出 {exit_info.get('code')}（{exit_info.get('reason')}）"
                                                          if exit_info else ""))
        return 0
    found = channel.job_stop(args.alias, name)
    _say(args, found, f"{name}：" + ("已停" if found["exited"] else "停止请求已发，还没确认结束"))
    return 0


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        if not devices.enabled():
            raise access.Refused("设备通道没开（服务的 COVEY_DEVICES 不是 1）")
        channel = access.Channel(access.turn_from_env())
    except access.Refused as exc:
        print(f"[covey device] {exc}", file=sys.stderr)
        return REFUSED
    except devices.DeviceConfigError as exc:
        print(f"[covey device] devices.json 有错，先改好：{exc}", file=sys.stderr)
        return REFUSED
    try:
        return run(args, channel)
    except access.Refused as exc:
        print(f"[covey device] {exc}", file=sys.stderr)
        return REFUSED
    except (dt.DeviceError, OSError) as exc:
        reason = devices.OFFLINE_REASONS.get(type(exc).__name__, type(exc).__name__)
        print(f"[covey device] {getattr(args, 'alias', '')} 没有答复或出错（{reason}）：{str(exc)[:300]}", file=sys.stderr)
        if channel.turn.held:
            print(HELD_HINT, file=sys.stderr)
        return UNREACHABLE
    finally:
        channel.close()


if __name__ == "__main__":
    sys.exit(main())
