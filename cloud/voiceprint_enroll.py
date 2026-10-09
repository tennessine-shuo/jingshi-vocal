"""声纹注册工具（方案B · 云端运维）。

给老师/学生注册声纹到腾讯云 1:N 声纹分组，并写入本地注册库
voiceprint_registry.json（voiceprint_id → 人员身份）。

用法：
  python voiceprint_enroll.py --config cloud_config.json \
      --wav teacher_10s.wav --person-id T001 --name 张老师 --role teacher

要求：wav 为 16kHz 单声道、有效语音 3~30s（推荐 10s 左右）。
注意：VoicePrintId 由腾讯云自动生成并写入注册库，无需自定义。
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path

from tencentcloud.asr.v20190614 import asr_client, models as asr_models
from tencentcloud.common import credential
from tencentcloud.common.profile.client_profile import ClientProfile
from tencentcloud.common.profile.http_profile import HttpProfile


def main() -> int:
    ap = argparse.ArgumentParser(description="声纹注册")
    ap.add_argument("--config", default="cloud_config.json")
    ap.add_argument("--wav", required=True, help="16kHz 单声道 wav，3~30s")
    ap.add_argument("--voiceprint-id", default=None,
                    help="声纹 ID（留空则由腾讯云自动生成，推荐留空）")
    ap.add_argument("--person-id", required=True, help="教务系统人员 ID")
    ap.add_argument("--name", required=True, help="姓名")
    ap.add_argument("--role", required=True, choices=["teacher", "student"])
    args = ap.parse_args()

    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    cos_cfg = config["cos"]
    vp_cfg = config.get("voiceprint", {})
    group_id = vp_cfg.get("group_id", "jiaopei_voiceprints")

    cred = credential.Credential(cos_cfg["secret_id"], cos_cfg["secret_key"])
    hp = HttpProfile(); hp.endpoint = "asr.tencentcloudapi.com"
    cp = ClientProfile(); cp.httpProfile = hp
    client = asr_client.AsrClient(cred, cos_cfg.get("region", "ap-shanghai"), cp)

    data = base64.b64encode(Path(args.wav).read_bytes()).decode()
    req = asr_models.VoicePrintEnrollRequest()
    req.VoiceFormat = 1            # wav
    req.SampleRate = 16000
    req.Data = data
    req.GroupId = group_id
    req.SpeakerNick = args.name    # 说话人昵称（≤32字节）
    # 注意：当前版本接口不接受自定义 VoicePrintId，由服务端生成并在响应中返回
    resp = client.VoicePrintEnroll(req)
    voiceprint_id = resp.Data.VoicePrintId
    if args.voiceprint_id and args.voiceprint_id != voiceprint_id:
        print(f"提示：接口不支持自定义声纹 ID，已使用服务端生成的 {voiceprint_id}")
    print(f"声纹已注册: {voiceprint_id}（{args.name}）-> 分组 {group_id}")

    # 写本地注册库
    reg_path = Path(vp_cfg.get("registry_file", "voiceprint_registry.json"))
    registry = json.loads(reg_path.read_text(encoding="utf-8")) if reg_path.exists() else {}
    registry[voiceprint_id] = {
        "person_id": args.person_id, "person_name": args.name, "role": args.role}
    reg_path.write_text(json.dumps(registry, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    print(f"注册库已更新: {reg_path}（共 {len(registry)} 人）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
