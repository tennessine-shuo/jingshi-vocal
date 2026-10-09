"""COS 对象存储客户端封装（方案B · 云端公共组件）。

云端各环节统一通过本模块访问对象存储：
- 读 meta.json / 下载音频分段
- 生成预签名下载 URL（供腾讯云 ASR 拉取私有桶音频）
- 回写中间产物与最终报告

密钥来源优先级：显式参数 > 环境变量（COS_SECRET_ID / COS_SECRET_KEY）。
生产（SCF）建议用环境变量；本地调试用 cloud_config.json。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os

from qcloud_cos import CosConfig, CosS3Client

log = logging.getLogger("cos_client")


class CosStore:
    def __init__(self, bucket: str, region: str,
                 secret_id: str | None = None, secret_key: str | None = None):
        self.bucket = bucket
        self.region = region
        sid = secret_id or os.getenv("COS_SECRET_ID", "")
        skey = secret_key or os.getenv("COS_SECRET_KEY", "")
        if not sid or not skey:
            raise RuntimeError("缺少 COS 密钥（secret_id/secret_key 或环境变量）")
        self.client = CosS3Client(CosConfig(Region=region, SecretId=sid, SecretKey=skey))

    # ---------- 读 ----------
    def get_json(self, key: str) -> dict:
        resp = self.client.get_object(Bucket=self.bucket, Key=key)
        raw = resp["Body"].get_raw_stream().read()
        return json.loads(raw.decode("utf-8"))

    def download(self, key: str, local_path: str, expect_sha256: str | None = None) -> str:
        self.client.download_file(Bucket=self.bucket, Key=key, DestFilePath=local_path)
        if expect_sha256:
            actual = self._sha256(local_path)
            if actual != expect_sha256:
                raise RuntimeError(f"SHA256 校验失败 {key}: 期望 {expect_sha256} 实际 {actual}")
        return local_path

    def exists(self, key: str) -> bool:
        return self.client.object_exists(Bucket=self.bucket, Key=key)

    def list_keys(self, prefix: str, max_keys: int = 1000) -> list[str]:
        """列出指定前缀下的全部对象 key（自动翻页）。"""
        return list(self.list_entries(prefix, max_keys))

    def list_entries(self, prefix: str, max_keys: int = 1000) -> dict[str, str]:
        """列出前缀下的 {key: LastModified}（ISO8601 UTC，自动翻页）。

        LastModified 用于「新旧比较」：判断成品是否比源 meta 更新，
        从而决定一门课是否需要（重新）处理。
        """
        out: dict[str, str] = {}
        marker = ""
        while True:
            resp = self.client.list_objects(
                Bucket=self.bucket, Prefix=prefix, Marker=marker,
                MaxKeys=max_keys)
            for item in resp.get("Contents", []):
                out[item["Key"]] = item.get("LastModified", "")
            if resp.get("IsTruncated") == "true":
                marker = resp.get("NextMarker") or next(reversed(out))
            else:
                break
        return out

    # ---------- URL ----------
    def presigned_url(self, key: str, expired: int = 7200) -> str:
        """私有桶预签名 GET URL（默认 2 小时有效，供 ASR 服务拉取音频）。"""
        return self.client.get_presigned_download_url(
            Bucket=self.bucket, Key=key, Expired=expired)

    # ---------- 写 ----------
    def put_json(self, key: str, data: dict) -> None:
        self.client.put_object(
            Bucket=self.bucket, Key=key,
            Body=json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"))

    def put_file(self, key: str, local_path: str) -> None:
        with open(local_path, "rb") as f:
            self.client.put_object(Bucket=self.bucket, Key=key, Body=f)

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=key)

    @staticmethod
    def _sha256(path: str) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
