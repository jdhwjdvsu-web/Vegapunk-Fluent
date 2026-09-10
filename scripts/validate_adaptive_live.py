"""Opt-in live five-parameter regression against an isolated running Web instance."""
import argparse
import json
import time
import urllib.request
from pathlib import Path


def request(base, path, payload=None):
    req = urllib.request.Request(base + path, data=None if payload is None else json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=650) as response:
        return json.load(response)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8780")
    parser.add_argument("--run", action="store_true", help="Actually launch one Fluent optimization trial")
    args = parser.parse_args()
    state = request(args.url, "/api/state")
    profile = state["model"]["profile"]
    if not profile or not state["model"]["execution_ready"]:
        raise SystemExit("Analyze the audited Mixing Elbow baseline in this isolated Web instance first")
    parameters = []
    wanted = {( "cold-inlet", "velocity"), ("hot-inlet", "velocity"),
              ("hot-inlet", "temperature"), ("cold-inlet", "turbulence_intensity"),
              ("cold-inlet", "hydraulic_diameter")}
    for p in state["parameters"]:
        if (p["zone"], p["rule_id"]) in wanted:
            parameters.append(dict(parameter_key=p["key"], range_min=p["default_value"] * .95,
                                   range_max=p["default_value"] * 1.05))
    if len(parameters) != 5:
        raise SystemExit("Did not discover all five required regression parameters")
    print(json.dumps(parameters, ensure_ascii=False), flush=True)
    if not args.run:
        return
    payload = dict(case_file=profile["case_file"], endpoint=profile["endpoint"],
                   parameters=parameters, iterations=100, target_trials=1)
    print(request(args.url, "/api/runs", payload), flush=True)
    deadline = time.monotonic() + 1200
    while time.monotonic() < deadline:
        state = request(args.url, "/api/state")
        if state["job"]["status"] != "running":
            print(json.dumps({"job": state["job"], "summary": state["summary"]}, ensure_ascii=False), flush=True)
            if state["job"]["status"] != "completed" or not state["trials"]:
                raise SystemExit("Live regression failed")
            valid = [t for t in state["trials"] if t.get("gates", {}).get("passed")]
            if not valid:
                raise SystemExit("No trial passed the quality gates")
            return
        time.sleep(2)
    raise SystemExit("Live regression timed out; inspect the owned task, do not blindly retry")


if __name__ == "__main__":
    main()
