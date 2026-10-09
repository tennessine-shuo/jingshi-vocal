"""本地运行云端流水线（调试用，方案B）。

用法：
  python run_local.py --config cloud_config.json \
      --meta-key jiaopei/room-01/20260904/TEST-001_..._meta.json

在 SCF 部署前，用本机（已装 ffmpeg + Python 环境）对 COS 中的真实课程
完整跑一遍云端流水线，验证各环节与产物。
"""
from __future__ import annotations

import argparse
import json
import logging
import sys

from pipeline import Pipeline

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")


def main() -> int:
    ap = argparse.ArgumentParser(description="本地运行云端教评流水线")
    ap.add_argument("--config", default="cloud_config.json", help="云端配置文件")
    ap.add_argument("--meta-key", required=True, help="COS 中 meta.json 的对象 key")
    ap.add_argument("--bucket", default="", help="覆盖配置中的桶名（可选）")
    args = ap.parse_args()

    with open(args.config, encoding="utf-8") as f:
        config = json.load(f)
    bucket = args.bucket or config["cos"]["bucket"]

    result = Pipeline(config).process(bucket=bucket, meta_key=args.meta_key)

    print("\n================ 流水线结果 ================")
    print("状态:", result["status"])
    if result.get("stats"):
        print("统计:", json.dumps(result["stats"], ensure_ascii=False))
    if result.get("report"):
        print("报告:", json.dumps(result["report"], ensure_ascii=False, indent=2))
    return 0 if result["status"] == "done" else 1


if __name__ == "__main__":
    sys.exit(main())
