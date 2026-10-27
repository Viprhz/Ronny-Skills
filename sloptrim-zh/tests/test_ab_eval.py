"""Deterministic harness tests. Synthetic votes test the math, NOT skill effectiveness."""
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("ab_eval", ROOT / "scripts" / "ab_eval.py")
ab = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ab)


def sample(i, split="development"):
    return {"id": f"case-{i:03d}", "text": "我们将持续推进优化工作。",
            "request": "请改得自然清楚，不能添加事实。",
            "genre": ("周报", "技术说明", "个人观点")[i % 3],
            "split": split, "source": "synthetic-smoke-test"}


def outputs(manifest, positive=True):
    result = []
    for case in manifest["cases"]:
        for arm in ("baseline", "skill"):
            result.append({"id": case["id"], "arm": arm,
                           "text": f"{case['id']}:{arm}",
                           "model": "mock-model-0", "settings": {"temperature": 0},
                           "runner": "synthetic-fixture"})
    return result


def ratings(manifest, raters=("rater1", "rater2"), winner="skill"):
    rows = []
    for case in manifest["cases"]:
        for rater in raters:
            preferred = next(k for k in ("A", "B") if case[k] == winner)
            row = {"id": case["id"], "rater": rater,
                   "preferred": preferred}
            for dim in ab.DIMENSIONS:
                row[f"{dim}_{preferred}"] = 5
                row[f"{dim}_{'B' if preferred == 'A' else 'A'}"] = 2
            row["hard_error_A"] = False
            row["hard_error_B"] = False
            rows.append(row)
    return rows


class HarnessTests(unittest.TestCase):
    def test_prepare_stable_and_balanced_allocation(self):
        cases = [sample(i) for i in range(30)]
        a = ab.prepare(cases, 12)
        self.assertEqual(a, ab.prepare(list(reversed(cases)), 12))
        self.assertNotEqual(a, ab.prepare(cases, 13))
        self.assertEqual(len(a["cases"]), 30)
        self.assertTrue(all(set((c["A"], c["B"])) == {"skill", "baseline"}
                            for c in a["cases"]))

    def test_blind_packets_never_reveal_arm_or_metadata(self):
        m = ab.prepare([sample(i) for i in range(4)], 99)
        packets = ab.blind_packets(m, outputs(m))
        self.assertEqual(len(packets), 4)
        self.assertNotIn("arm", packets[0])
        self.assertNotIn("model", packets[0])
        self.assertNotIn("runner", packets[0])
        self.assertEqual(set(packets[0]), {"id", "genre", "text", "request", "A", "B"})

    def test_incomplete_pairs_and_config_mismatch_rejected(self):
        m = ab.prepare([sample(1)], 19)
        rows = outputs(m)
        with self.assertRaisesRegex(ValueError, "both arms"):
            ab.response_index(rows[:1], ab.validate_manifest(m))
        rows[1]["settings"] = {"temperature": 1}
        with self.assertRaisesRegex(ValueError, "mismatched settings"):
            ab.response_index(rows, ab.validate_manifest(m))

    def test_development_smoke_can_never_claim_improvement(self):
        m = ab.prepare([sample(i) for i in range(12)], 99)
        r = ab.report(m, outputs(m), ratings(m))
        self.assertEqual(r["counts"]["win"], 12)
        self.assertGreater(r["mean_delta"]["clarity"], 0)
        self.assertFalse(r["evidence_requirements_met"])
        self.assertEqual(r["status"], "insufficient_evidence")

    def test_synthetic_holdout_proves_only_the_calculator(self):
        m = ab.prepare([sample(i, "holdout") for i in range(30)], 99)
        r = ab.report(m, outputs(m), ratings(m))
        self.assertTrue(r["evidence_requirements_met"])
        self.assertEqual(r["status"], "improved_on_this_holdout")
        self.assertEqual(r["mean_preference"], 1)
        self.assertGreater(r["preference_bootstrap_lower_95"], 0.5)

    def test_rating_validation_rejects_bad_values_and_duplicates(self):
        m = ab.prepare([sample(1)], 1)
        rows = ratings(m, raters=("r1",))
        rows[0]["clarity_A"] = 5.5
        with self.assertRaisesRegex(ValueError, "integer"):
            ab.validate_ratings(rows, ab.validate_manifest(m))
        rows = ratings(m, raters=("r1",))
        with self.assertRaisesRegex(ValueError, "duplicate"):
            ab.validate_ratings(rows + rows, ab.validate_manifest(m))

    def test_runner_executes_each_arm_without_shell(self):
        with tempfile.TemporaryDirectory() as td:
            adapter = Path(td) / "adapter.py"
            adapter.write_text("import sys,json\np=json.load(sys.stdin)\n"
                               "print(p['arm'] + ':' + p['id'])\n", encoding="utf-8")
            cmd = f"{sys.executable} {adapter}"
            m = ab.prepare([sample(i) for i in range(3)], 21)
            rows = ab.run_agents(m, cmd, cmd, "mock-model", {"temperature": 0},
                                 "mock-runner", 15)
            self.assertEqual(len(rows), 6)
            self.assertEqual(rows[0]["arm"], m["cases"][0]["execution_order"][0])
            ab.response_index(rows, ab.validate_manifest(m))

    def test_agent_adapter_differentiates_arms_and_writes_trace(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            mock = root / "mock.py"
            mock.write_text("import sys\nprompt=sys.stdin.read()\n"
                            "print('SKILL' if '此次实验启用 sloptrim-zh' in prompt else 'BASELINE')\n",
                            encoding="utf-8")
            params = {"id": "sample1", "request": "改得自然一些。",
                      "text": "请进一步赋能业务协同。"}
            for arm, expected in (("baseline", "BASELINE"), ("skill", "SKILL")):
                p = dict(params, arm=arm)
                adapter = ROOT / "scripts" / "ab_agent_adapter.py"
                cmd = [sys.executable, str(adapter), "--arm", arm,
                       "--agent-command", f"{sys.executable} {mock}",
                       "--cwd", td, "--trace-dir", str(root / "traces")]
                r = subprocess.run(cmd, input=json.dumps(p, ensure_ascii=False),
                                   text=True, capture_output=True)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(r.stdout.strip(), expected)
                trace = json.loads((root / "traces" / f"sample1-{arm}.json").read_text())
                self.assertEqual(trace["arm"], arm)
                self.assertEqual(bool(trace["skill_sha256"]), arm == "skill")

    def test_cli_end_to_end_without_model_calls(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            corpus = root / "corpus.jsonl"
            ab.write_jsonl(corpus, [sample(i) for i in range(3)])
            manifest = root / "manifest.json"
            cmd = [sys.executable, str(ROOT / "scripts" / "ab_eval.py"),
                   "prepare", "--cases", str(corpus), "--out", str(manifest)]
            p = subprocess.run(cmd, text=True, capture_output=True)
            self.assertEqual(p.returncode, 0, p.stderr)
            m = json.loads(manifest.read_text(encoding="utf-8"))
            response_file = root / "responses.jsonl"
            ab.write_jsonl(response_file, outputs(m))
            blind_file = root / "blind.jsonl"
            p = subprocess.run([sys.executable, str(ROOT / "scripts" / "ab_eval.py"),
                                "blind", "--manifest", str(manifest),
                                "--responses", str(response_file), "--out", str(blind_file)],
                               text=True, capture_output=True)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertEqual(len(ab.read_jsonl(blind_file)), 3)
            rating_file = root / "ratings.jsonl"
            ab.write_jsonl(rating_file, ratings(m))
            report_file = root / "report.json"
            p = subprocess.run([sys.executable, str(ROOT / "scripts" / "ab_eval.py"),
                                "report", "--manifest", str(manifest),
                                "--responses", str(response_file),
                                "--ratings", str(rating_file), "--out", str(report_file)],
                               text=True, capture_output=True)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertEqual(json.loads(report_file.read_text(encoding="utf-8"))["status"],
                             "insufficient_evidence")


if __name__ == "__main__":
    unittest.main()
