# -*- coding: utf-8 -*-
"""为原音频前缀配置 COS 生命周期规则（30 天转低频，降本）。

用法：python cos_lifecycle.py            # 应用默认规则
      python cos_lifecycle.py --status   # 只读当前规则
      python cos_lifecycle.py --clear    # 清空本桶生命周期（回滚）

默认规则（仅降冷、不删除，非破坏）：
  Rule 1  jiaopei/room-01/   30 天后转低频 MAZ_STANDARD_IA（原音频降本）
  [注] 本测试桶为多 AZ 桶（对象 StorageClass=MAZ_STANDARD），
      不能沉降到单 AZ 低频，必须用多 AZ 枚举 MAZ_STANDARD_IA。
幂等：先删除旧规则再写入，保证完全一致、避免残留。
"""
from __future__ import annotations
import json
import sys

from qcloud_cos import CosConfig, CosS3Client


def _load_cfg() -> dict:
    return json.load(open(r"C:\recorder\cloud\cloud_config.json", encoding="utf-8"))


def _client(cfg: dict) -> CosS3Client:
    return CosS3Client(CosConfig(
        Region=cfg["cos"]["region"],
        SecretId=cfg["cos"]["secret_id"],
        SecretKey=cfg["cos"]["secret_key"]))


# 默认规则：原音频 30 天转低频（不删除，保留可回取）。
# 桶为多 AZ（MAZ_STANDARD），低频沉降必须用多 AZ 枚举 MAZ_STANDARD_IA。
# 若桶为单 AZ（Standard），应改回 "Standard_IA"。
DEFAULT_RULES = {
    "Rule": [
        {
            "ID": "audio-to-IA-30d",
            "Filter": {"Prefix": "jiaopei/room-01/"},
            "Status": "Enabled",
            "Transition": [
                {"Days": 30, "StorageClass": "MAZ_STANDARD_IA"}
            ],
        }
    ]
}


def main() -> None:
    args = sys.argv[1:]
    cfg = _load_cfg()
    client = _client(cfg)
    bucket = cfg["cos"]["bucket"]

    if "--status" in args:
        try:
            r = client.get_bucket_lifecycle(Bucket=bucket)
            print("当前生命周期规则:")
            print(json.dumps(r, ensure_ascii=False, indent=2))
        except Exception as e:  # noqa: BLE001
            print("读取失败（可能无规则）:", str(e)[:200])
        return

    if "--clear" in args:
        client.delete_bucket_lifecycle(Bucket=bucket)
        print("已清空本桶生命周期规则（回滚完成）")
        return

    # 默认：应用规则（幂等：先删旧再写新，保证完全一致）
    client.delete_bucket_lifecycle(Bucket=bucket)
    client.put_bucket_lifecycle(Bucket=bucket,
                                LifecycleConfiguration=DEFAULT_RULES)
    print("已应用生命周期规则（覆盖写入）:")
    print(json.dumps(DEFAULT_RULES, ensure_ascii=False, indent=2))
    # 回读确认
    r = client.get_bucket_lifecycle(Bucket=bucket)
    print("回读确认 OK:", json.dumps(r, ensure_ascii=False)[:400])


if __name__ == "__main__":
    main()
