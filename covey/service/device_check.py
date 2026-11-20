#!/usr/bin/env python3
"""Check the devices of devices.json from the command line: ``covey_devices.py check <alias>`` runs core/device_probe.py's self-check.

``check`` is the same flow the workspace card's device line reports (``device_probe.check_device``): a 5-second ssh with
BatchMode, the device-side program and the Codex reader pushed, ``hello``, the environment profile imported, then one
minimal real request for every Harness the device declares (60 seconds each, with the profile's environment); the result
is written to ``<state>/devices/<alias>/profile.json``. ``list`` prints each device's last check without touching it.
Exit status: 0 passed, 2 a check failed, 3 devices are off or the alias is not listed."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from covey.core import device_probe, devices


def devices_report(state_dir, config=None):
    """``{alias: {"title", "harnesses", "check"}}`` of every device in devices.json: the last self-check, read from its
    ``profile.json`` (``check``: when, ``ok``, the words). Empty when devices are off. For ``list`` and
    ``service/doctor.py``; asks no device."""
    try:
        config = config if config is not None else devices.current()
    except devices.DeviceConfigError as exc:
        return {"error": str(exc)}
    rows = {}
    for alias, device in config.devices.items():
        profile = devices.read_profile(state_dir, alias)
        check = profile.get("check") if isinstance(profile.get("check"), dict) else None
        rows[alias] = {"title": device.title, "harnesses": list(device.harnesses), "check": check,
                       "status": device_probe.status_text(state_dir, alias)}
    return rows


def run_check(args, out=None):
    out = out or sys.stdout
    if not devices.enabled():
        print("COVEY_DEVICES 没有设成 1：多设备是关的，没有可检查的设备。", file=out)
        return 3
    try:
        config = devices.load()
        config.device(args.alias)
    except devices.DeviceConfigError as exc:
        print("devices.json 有问题：%s" % exc, file=out)
        return 3
    except devices.UnknownDevice:
        print("设备 %s 不在 devices.json 里（有：%s）" % (args.alias, ", ".join(config.devices) or "没有"), file=out)
        return 3
    result = device_probe.self_check(config, args.alias, state_dir=args.state_dir.expanduser().resolve(),
                                probe=None if args.no_probe else device_probe.probe_harness)
    if args.json:
        print(json.dumps({"alias": args.alias, "ok": result.ok, "text": result.explain(),
                          "steps": [{"name": s.name, "ok": s.ok, "skipped": s.skipped, "detail": s.detail,
                                     "seconds": round(s.seconds, 1)} for s in result.steps]}, ensure_ascii=False), file=out)
    else:
        print("\n".join(result.lines()), file=out)
        print(result.explain(), file=out)
    return 0 if result.ok else 2


def run_list(args, out=None):
    out = out or sys.stdout
    report = devices_report(args.state_dir.expanduser().resolve())
    if "error" in report:
        print("devices.json 有问题：%s" % report["error"], file=out)
        return 3
    if not report:
        print("没有设备（COVEY_DEVICES 没开，或 devices.json 里没有设备）。", file=out)
        return 3
    for alias, row in report.items():
        print("%s（%s）· %s：%s" % (alias, row["title"], "、".join(row["harnesses"]), row["status"]), file=out)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="covey_devices.py", description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True, help="the Bridge's state directory (profile.json goes under devices/)")
    sub = parser.add_subparsers(dest="action", required=True)
    check = sub.add_parser("check", help="self-check one device and record what was learned")
    check.add_argument("alias")
    check.add_argument("--no-probe", action="store_true", help="skip the minimal real request of each Harness")
    check.add_argument("--json", action="store_true", help="print the result as one JSON line")
    sub.add_parser("list", help="each device's last self-check, without touching it")
    args = parser.parse_args(argv)
    return run_check(args) if args.action == "check" else run_list(args)


if __name__ == "__main__":
    sys.exit(main())
