"use strict";

const { spawnSync } = require("node:child_process");

// 先跑 HTTP 运行契约，再跑完整领域测试套件
const runs = [
  ["python3", ["-m", "unittest", "-v", "service_contract"]],
  ["python3", ["-m", "unittest", "discover", "-v"]],
];

for (const [cmd, args] of runs) {
  const result = spawnSync(cmd, args, { stdio: "inherit" });
  if (result.error) {
    console.error(result.error.message);
    process.exit(1);
  }
  if (result.status !== 0) {
    process.exit(result.status);
  }
}
