"use strict";

const { spawnSync } = require("node:child_process");

// 先跑顶层服务契约 service_contract.py，再发现 tests/ 下的全部领域测试
const steps = [
  ["python3", ["-m", "unittest", "-v", "service_contract"]],
  ["python3", ["-m", "unittest", "discover", "-v", "-s", "tests"]],
];

let status = 0;
for (const [cmd, args] of steps) {
  const result = spawnSync(cmd, args, { stdio: "inherit" });
  if (result.error) {
    console.error(result.error.message);
    process.exit(1);
  }
  status = result.status ?? 1;
  if (status !== 0) break;
}
process.exit(status);

