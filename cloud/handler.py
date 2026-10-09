"""SCF 云函数入口：对象存储上传事件 → 启动处理流水线（方案B · 云端）。

部署：腾讯云 SCF，触发器 = COS 事件通知（PutObject）。
以 meta.json 到达作为"一节完整课程就绪"的信号，启动整条流水线。
"""
import logging
from urllib.parse import unquote

from pipeline import Pipeline

log = logging.getLogger("handler")


def main_handler(event, context):  # noqa: ARG001
    records = event.get("Records", [])
    for r in records:
        ev = r.get("event", {}).get("eventName", "")
        if not ev.startswith("cos:ObjectCreated"):
            continue
        cos = r.get("cos", {})
        bucket = cos.get("cosBucket", {}).get("Name", "")
        key = unquote(cos.get("object", {}).get("key", ""))
        log.info("对象上传事件 bucket=%s key=%s", bucket, key)

        if key.endswith("meta.json"):
            # 音频分段一般先于 meta.json 上传完毕；若乱序，可在流水线内做等待/对账
            Pipeline().process(bucket=bucket, meta_key=key)
    return {"statusCode": 200}
