#!/usr/bin/env python3
"""Blind paired A/B evaluation of Chinese editing quality. Standard library only.

This tool NEVER calls a model. Provide real outputs from the same configured
agent/model, with and without sloptrim-zh, then collect independent blind ratings.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import shlex
import subprocess
import statistics
import time
import sys
from collections import defaultdict
from pathlib import Path


DIMENSIONS = ("naturalness", "clarity", "faithfulness", "voice")


def read_jsonl(path: Path) -> list[dict]:
    rows = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        obj = json.loads(line)
        if not isinstance(obj, dict):
            raise ValueError(f"{path}:{lineno}: expected object")
        rows.append(obj)
    return rows


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows),
                    encoding="utf-8")


def unique_ids(rows: list[dict], label: str) -> dict[str, dict]:
    result = {}
    for row in rows:
        key = row.get("id")
        if not isinstance(key, str) or not key.strip() or key in result:
            raise ValueError(f"{label}: missing or duplicate id: {key!r}")
        result[key] = row
    if not result:
        raise ValueError(f"{label}: empty input")
    return result


def prepare(cases: list[dict], seed: int) -> dict:
    ids = unique_ids(cases, "cases")
    for row in cases:
        if not all(isinstance(row.get(k), str) and row[k].strip()
                   for k in ("text", "request", "genre", "source", "split")):
            raise ValueError(f"{row['id']}: needs text/request/genre/source/split")
        if row["split"] not in ("development", "holdout"):
            raise ValueError(f"{row['id']}: split must be development or holdout")
    canonical = json.dumps([ids[k] for k in sorted(ids)], ensure_ascii=False,
                           sort_keys=True, separators=(",", ":"))
    corpus_sha256 = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    rng = random.Random(seed)
    pairs = []
    for row in sorted(ids.values(), key=lambda r: r["id"]):
        skill_is_a = bool(rng.getrandbits(1))
        pairs.append({"id": row["id"], "genre": row["genre"], "split": row["split"],
                      "source": row["source"], "text": row["text"],
                      "request": row["request"],
                      "A": "skill" if skill_is_a else "baseline",
                      "B": "baseline" if skill_is_a else "skill",
                      "execution_order": (["skill", "baseline"] if rng.getrandbits(1)
                                          else ["baseline", "skill"])})
    return {"protocol": "sloptrim-zh-paired-v1", "seed": seed,
            "corpus_sha256": corpus_sha256, "cases": pairs}


def validate_manifest(manifest: dict) -> dict:
    if manifest.get("protocol") != "sloptrim-zh-paired-v1":
        raise ValueError("unsupported manifest protocol")
    cases = unique_ids(manifest.get("cases", []), "manifest")
    for row in cases.values():
        if set((row.get("A"), row.get("B"))) != {"baseline", "skill"}:
            raise ValueError(f"{row['id']}: invalid allocation")
    return cases


def response_index(rows: list[dict], cases: dict) -> dict:
    data = {}
    for row in rows:
        key = (row.get("id"), row.get("arm"))
        if key in data or row.get("id") not in cases or key[1] not in ("baseline", "skill"):
            raise ValueError(f"unexpected or duplicate response {key}")
        if not isinstance(row.get("text"), str) or not row["text"].strip():
            raise ValueError(f"{key}: empty text")
        for field in ("model", "settings", "runner"):
            if field not in row or not row[field]:
                raise ValueError(f"{key}: missing reproducibility metadata: {field}")
        data[key] = row
    globally_consistent = {"model": set(), "settings": set(), "runner": set()}
    for row in data.values():
        if not isinstance(row["model"], str) or not isinstance(row["settings"], dict) or not isinstance(row["runner"], str):
            raise ValueError("model/runner must be strings and settings must be a JSON object")
        globally_consistent["model"].add(row["model"])
        globally_consistent["runner"].add(row["runner"])
        globally_consistent["settings"].add(json.dumps(row["settings"], sort_keys=True, ensure_ascii=False))
    if any(len(values) != 1 for values in globally_consistent.values()):
        raise ValueError("model/settings/runner must be identical across the entire A/B batch")
    for case_id in cases:
        left, right = data.get((case_id, "baseline")), data.get((case_id, "skill"))
        if left is None or right is None:
            raise ValueError(f"{case_id}: both arms required")
        for field in ("model", "settings", "runner"):
            if left[field] != right[field]:
                raise ValueError(f"{case_id}: mismatched {field} across arms")
        if left["text"] == right["text"]:
            # Valid: a well-written source can need zero edits; report transparently.
            pass
    return data


def blind_packets(manifest: dict, outputs: list[dict]) -> list[dict]:
    cases = validate_manifest(manifest)
    data = response_index(outputs, cases)
    return [{"id": key, "genre": row["genre"], "text": row["text"],
             "request": row["request"],
             "A": data[(key, row["A"])]["text"], "B": data[(key, row["B"])]["text"]}
            for key, row in cases.items()]


def validate_ratings(rows: list[dict], cases: dict) -> dict:
    seen = {}
    for r in rows:
        key = (r.get("id"), r.get("rater"))
        if r.get("id") not in cases or not isinstance(r.get("rater"), str) or not r["rater"] or key in seen:
            raise ValueError(f"unknown/duplicate rating: {key}")
        if r.get("preferred") not in ("A", "B", "tie"):
            raise ValueError(f"{key}: preferred must be A, B or tie")
        for d in DIMENSIONS:
            for label in "AB":
                value = r.get(f"{d}_{label}")
                if type(value) is not int or not 1 <= value <= 5:
                    raise ValueError(f"{key}: {d}_{label} must be integer 1..5")
        for label in "AB":
            if type(r.get(f"hard_error_{label}")) is not bool:
                raise ValueError(f"{key}: hard_error_{label} must be bool")
        seen[key] = r
    return seen


def bootstrap_lower(samples: list[float], seed: int, repetitions: int = 3000) -> float:
    rng = random.Random(seed)
    n = len(samples)
    means = sorted(sum(rng.choice(samples) for _ in range(n)) / n
                   for _ in range(repetitions))
    return round(means[int(0.025 * (repetitions - 1))], 4)


def report(manifest: dict, responses: list[dict], ratings: list[dict]) -> dict:
    cases = validate_manifest(manifest)
    index = response_index(responses, cases)
    reviews = validate_ratings(ratings, cases)
    grouped = defaultdict(list)
    for (case_id, _), rating in reviews.items():
        grouped[case_id].append(rating)
    if set(grouped) != set(cases):
        raise ValueError("incomplete blind reviews: every case needs a review")
    details, margins, delta_by_dim = [], [], {d: [] for d in DIMENSIONS}
    counts = {"win": 0, "tie": 0, "loss": 0}
    hard_errors = {"baseline": 0, "skill": 0}
    for case_id, case in cases.items():
        votes, per_dim = [], {d: [] for d in DIMENSIONS}
        err = {"baseline": False, "skill": False}
        for rating in grouped[case_id]:
            for label in "AB":
                arm = case[label]
                err[arm] = err[arm] or rating[f"hard_error_{label}"]
            pref = rating["preferred"]
            votes.append("tie" if pref == "tie" else case[pref])
            for dim in DIMENSIONS:
                a = rating[f"{dim}_A"]
                b = rating[f"{dim}_B"]
                per_dim[dim].append(a - b if case["A"] == "skill" else b - a)
        points = votes.count("skill") + 0.5 * votes.count("tie")
        preference = points / len(votes)
        outcome = "win" if preference > 0.5 else "loss" if preference < 0.5 else "tie"
        counts[outcome] += 1
        margins.append(preference)
        for dim in DIMENSIONS:
            delta_by_dim[dim].append(statistics.mean(per_dim[dim]))
        for arm in ("baseline", "skill"):
            hard_errors[arm] += int(err[arm])
        details.append({"id": case_id, "genre": case["genre"], "split": case["split"],
                        "raters": len(grouped[case_id]), "outcome": outcome,
                        "preference_score": round(preference, 4),
                        "delta_scores": {d: round(statistics.mean(values), 3)
                                         for d, values in per_dim.items()},
                        "hard_errors": err,
                        "identical_outputs": index[(case_id, "baseline")]["text"] ==
                                             index[(case_id, "skill")]["text"]})
    n = len(cases)
    lcb = bootstrap_lower(margins, manifest["seed"])
    is_independent = all(row["split"] == "holdout" for row in cases.values())
    min_raters = min(len(grouped[c]) for c in cases)
    sufficient = n >= 30 and min_raters >= 2 and is_independent and len({c["genre"] for c in cases.values()}) >= 3
    improved = (sufficient and lcb > 0.5
                and statistics.mean(delta_by_dim["naturalness"]) > 0
                and statistics.mean(delta_by_dim["clarity"]) > 0
                and statistics.mean(delta_by_dim["faithfulness"]) >= 0
                and statistics.mean(delta_by_dim["voice"]) >= 0
                and hard_errors["skill"] == 0)
    elapsed = {arm: [r.get("wall_seconds") for r in index.values()
                     if r["arm"] == arm and isinstance(r.get("wall_seconds"), (float, int))]
               for arm in ("baseline", "skill")}
    return {
        "protocol": manifest["protocol"], "n": n, "seed": manifest["seed"],
        "corpus_sha256": manifest.get("corpus_sha256"),
        "split": "holdout" if is_independent else "development_or_mixed",
        "counts": counts, "model": sorted({str(v["model"]) for v in index.values()}),
        "runner": sorted({str(v["runner"]) for v in index.values()}),
        "mean_preference": round(statistics.mean(margins), 4),
        "preference_bootstrap_lower_95": lcb,
        "mean_delta": {d: round(statistics.mean(values), 3) for d, values in delta_by_dim.items()},
        "hard_error_cases": hard_errors, "min_raters_per_case": min_raters,
        "per_genre": {genre: {outcome: sum(1 for d in details if d["genre"] == genre and d["outcome"] == outcome)
                              for outcome in ("win", "tie", "loss")}
                      for genre in sorted({d["genre"] for d in details})},
        "mean_wall_seconds": {arm: (round(statistics.mean(v), 3) if v else None)
                              for arm, v in elapsed.items()},
        "status": ("improved_on_this_holdout" if improved else
                   "insufficient_evidence" if not sufficient else "no_verified_improvement"),
        "evidence_requirements_met": sufficient,
        "details": details,
        "limitations": ("Ratings must be independent and blinded; this script cannot verify model settings, "
                        "true skill execution, human rater identity, corpus provenance or absence of leakage. "
                        "Bootstrap CI uses paired case units, not rating events; no AI-authorship claim.")
    }



def run_agents(manifest: dict, baseline_cmd: str, skill_cmd: str, model: str,
               settings: dict, runner: str, timeout: int) -> list[dict]:
    """Execute configured adapters in randomized order. No shell; JSON request on stdin."""
    cases = validate_manifest(manifest)
    if not all((baseline_cmd, skill_cmd, model, runner)) or not isinstance(settings, dict):
        raise ValueError("commands, model, runner and settings are required")
    result = []
    for case_id, case in cases.items():
        for arm in case["execution_order"]:
            command = shlex.split(skill_cmd if arm == "skill" else baseline_cmd)
            if not command:
                raise ValueError("adapter command is empty")
            payload = {"id": case_id, "request": case["request"], "text": case["text"],
                       "genre": case["genre"], "arm": arm}
            started = time.perf_counter()
            proc = subprocess.run(command, input=json.dumps(payload, ensure_ascii=False),
                                  text=True, capture_output=True, timeout=timeout, check=False)
            if proc.returncode:
                raise ValueError(f"{case_id}/{arm}: adapter failed exit={proc.returncode}; "
                                 f"stderr={proc.stderr[:600]}")
            elapsed = time.perf_counter() - started
            generated = proc.stdout.strip()
            if not generated:
                raise ValueError(f"{case_id}/{arm}: empty model output")
            result.append({"id": case_id, "arm": arm, "text": generated,
                           "model": model, "settings": settings, "runner": runner,
                           "wall_seconds": round(elapsed, 3)})
    return result


def cli() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prepare", help="make deterministic blind assignment manifest")
    p.add_argument("--cases", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--seed", type=int, default=20261027)
    p = sub.add_parser("run", help="run CLI adapters on paired cases (paid model calls)")
    p.add_argument("--manifest", type=Path, required=True)
    p.add_argument("--baseline-cmd", required=True,
                   help="adapter command reading request JSON on stdin; must NOT load skill")
    p.add_argument("--skill-cmd", required=True,
                   help="same model/config adapter loading the actual skill")
    p.add_argument("--model", required=True, help="exact model identifier, including version")
    p.add_argument("--settings-json", required=True, help="model config JSON (temperature etc.)")
    p.add_argument("--runner", required=True, help="agent CLI version and execution environment")
    p.add_argument("--timeout", type=int, default=180)
    p.add_argument("--out", type=Path, required=True)
    p = sub.add_parser("blind", help="create blinded rating packets from paired outputs")
    p.add_argument("--manifest", type=Path, required=True)
    p.add_argument("--responses", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p = sub.add_parser("report", help="compare blind human ratings, require paired metadata")
    p.add_argument("--manifest", type=Path, required=True)
    p.add_argument("--responses", type=Path, required=True)
    p.add_argument("--ratings", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    try:
        if args.command == "prepare":
            result = prepare(read_jsonl(args.cases), args.seed)
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"Prepared {len(result['cases'])} cases (not quality evidence).")
        else:
            manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
            responses = read_jsonl(args.responses) if args.command != "run" else []
            if args.command == "run":
                settings = json.loads(args.settings_json)
                if args.timeout <= 0:
                    raise ValueError("timeout must be positive")
                outputs = run_agents(manifest, args.baseline_cmd, args.skill_cmd,
                                     args.model, settings, args.runner, args.timeout)
                write_jsonl(args.out, outputs)
                print(f"Collected {len(outputs)} unblinded responses; do not show raters.")
            elif args.command == "blind":
                packets = blind_packets(manifest, responses)
                write_jsonl(args.out, packets)
                print(f"Prepared {len(packets)} blinded packets.")
            else:
                result = report(manifest, responses, read_jsonl(args.ratings))
                args.out.parent.mkdir(parents=True, exist_ok=True)
                args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
                print(json.dumps({k: result[k] for k in (
                    "n", "counts", "mean_preference", "preference_bootstrap_lower_95",
                    "mean_delta", "hard_error_cases", "status")},
                    ensure_ascii=False, indent=2))
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        ap.exit(2, f"Evaluation input error: {exc}\n")
    return 0


if __name__ == "__main__":
    sys.exit(cli())
