"""One CI-side status snapshot; no model-driven polling or full log ingestion."""
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess


def seconds(step):
    if not step.get("started_at") or not step.get("completed_at"):
        return None
    parse = lambda value: datetime.fromisoformat(value.replace("Z", "+00:00"))
    return max(0, int((parse(step["completed_at"]) - parse(step["started_at"])).total_seconds()))


def summarize(jobs, run_id, candidate_run_id):
    rows = [{"name": job["name"], "status": job.get("conclusion") or job["status"],
             "seconds": seconds(job), "steps": [
                 {"name": step["name"], "status": step.get("conclusion") or step["status"],
                  "seconds": seconds(step)} for step in job.get("steps", [])]}
            for job in jobs if job["name"] != "Summarize release stages"]
    states = {row["name"]: row["status"] for row in rows}
    if states.get("Accept exact Windows candidate") == "failure":
        next_action = "保留隔离候选。使用 accept_candidate 和原构建运行号续验；case_evidence_runs 填本次运行号复用通过场景。构建输入变化必须重建。"
    elif states.get("deliver") == "failure":
        next_action = "检查交付摘要；已验收包使用 stage_existing 续传，生产检查失败须先排查，不重建原包。"
    elif states.get("Build signed formal Windows package") == "failure":
        next_action = "构建未通过，先处理该阶段失败；不得发布候选。"
    else:
        next_action = "按阶段结果继续；直传成功只表示待发布，生产就绪后才可登记。未执行项不视为通过。"
    return {"run_id": str(run_id), "candidate_run_id": str(candidate_run_id),
            "stages": rows, "next_action": next_action}


def main():
    run_id = os.environ["GITHUB_RUN_ID"]
    prefix = f"repos/{os.environ['GITHUB_REPOSITORY']}/actions/runs/{run_id}/jobs?per_page=100"
    jobs = json.loads(subprocess.check_output(["gh", "api", prefix], text=True))["jobs"]
    report = summarize(jobs, run_id, os.environ.get("CANDIDATE_RUN_ID") or run_id)
    Path("formal-stage-timings.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["## 正式流程阶段结果", "", f"运行号：{run_id}；候选构建运行号：{report['candidate_run_id']}",
             "", "| 阶段 | 状态 | 耗时（秒） |", "|---|---|---:|"]
    for stage in report["stages"]:
        lines.append(f"| {stage['name']} | {stage['status']} | {stage['seconds']} |")
        for step in stage["steps"]:
            if step["status"] == "failure":
                lines.append(f"| 失败步骤：{step['name']} | failure | {step['seconds']} |")
    lines.extend(["", report["next_action"], "", "各阶段可能并行，不能将所有阶段耗时相加当作总等待时间。"])
    with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as stream:
        stream.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
