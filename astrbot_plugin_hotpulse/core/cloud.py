"""云服务器询价：按同一规格问阿里云 / 腾讯云的官方价格接口。

为什么要走官方 API 而不是抓活动页：实测三家活动页的 HTML 里，价格和配置是分开
的两个 DOM 节点，抽成纯文本就断开了（三个站点用「同句含价格+规格」的正则只命中
1 条）；活动卡片又都是 JS 渲染，HTML 里没有任何商品接口。想拿到能对比的数字，
只有询价 API 这一条路。

拿到的是**目录价 + 官方折扣**（阿里云 OriginalPrice/TradePrice、腾讯云
OriginalPrice/DiscountPrice），**不含限时活动价和代金券**——这一点必须写清楚，
否则用户会拿它和活动页上的 99 元/年 对比，然后觉得插件在骗人。

签名手写，不引 SDK：多一个依赖就多一次在 2C2G 容器里 pip 失败的机会。
阿里云的签名有官方固定向量可做端到端断言；腾讯云的 SecretKey 在文档里是脱敏的，
只能断言与密钥无关的中间结果（CanonicalRequest / StringToSign / 哈希）。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import quote

import httpx

from .settings import get_logger, get_settings

# 只挑两家都有标准型号的档位，避免某些 SKU 在某地域不售卖导致询价失败。
# 阿里云 c7 是 1:2 计算型（c7.large=2核4G、c7.xlarge=4核8G）；
# 腾讯云 S5 同为 1:2（MEDIUM4=2核4G、LARGE8=4核8G）。
SPECS: dict[str, dict[str, str]] = {
    "2核4G": {"aliyun": "ecs.c7.large", "tencent": "S5.MEDIUM4"},
    "4核8G": {"aliyun": "ecs.c7.xlarge", "tencent": "S5.LARGE8"},
}

DEFAULT_SPEC = "2核4G"


@dataclass
class Quote:
    vendor: str
    spec: str
    price: float  # 实际要付的钱（含官方折扣）
    list_price: float | None  # 原价，没有就是 None
    period: str  # 例如「1年」
    region: str

    @property
    def discount(self) -> float | None:
        """官方折扣省下的比例，0.31 表示省 31%。没有原价就返回 None。"""
        if not self.list_price or self.list_price <= 0 or self.list_price <= self.price:
            return None
        return 1 - self.price / self.list_price


@dataclass
class QuoteResult:
    vendor: str
    ok: bool
    quote: Quote | None = None
    error: str | None = None


# ---------- 阿里云（RPC 风格签名 V1）----------


def _pe(value: str) -> str:
    """阿里云签名要求的 PercentEncode：保留 -_.~ ，空格转 %20，* 转 %2A。"""
    return quote(str(value), safe="~").replace("+", "%20").replace("*", "%2A")


def aliyun_signature(params: dict[str, str], secret: str, method: str = "GET") -> str:
    """按阿里云 RPC 规范算签名。参数里不要带 Signature 本身。

    格式：Base64(HMAC-SHA1(secret + "&", "GET&%2F&" + PercentEncode(规范化查询串)))
    注意 Timestamp 里的冒号会被编码两次：先是 %3A，进入待签名串后变成 %253A。
    """
    canonical = "&".join(f"{_pe(k)}={_pe(v)}" for k, v in sorted(params.items()))
    string_to_sign = f"{method}&%2F&{_pe(canonical)}"
    digest = hmac.new(f"{secret}&".encode(), string_to_sign.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


async def fetch_aliyun_price(
    client: httpx.AsyncClient,
    access_key: str,
    secret: str,
    spec: str,
    region: str = "cn-hangzhou",
    period: int = 12,
) -> QuoteResult:
    instance_type = SPECS.get(spec, SPECS[DEFAULT_SPEC])["aliyun"]
    params = {
        "AccessKeyId": access_key,
        "Action": "DescribePrice",
        "Format": "JSON",
        "SignatureMethod": "HMAC-SHA1",
        "SignatureNonce": uuid.uuid4().hex,
        "SignatureVersion": "1.0",
        "Timestamp": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "Version": "2014-05-26",
        "RegionId": region,
        "ResourceType": "instance",
        "InstanceType": instance_type,
        "PriceUnit": "Year",
        "Period": str(max(1, period // 12)),
        "Amount": "1",
        "SystemDisk.Category": "cloud_essd",
        "SystemDisk.Size": "40",
        "InternetChargeType": "PayByTraffic",
        "InternetMaxBandwidthOut": "5",
    }
    params["Signature"] = aliyun_signature(params, secret)

    try:
        resp = await client.get(f"https://ecs.{region}.aliyuncs.com/", params=params)
        payload = resp.json()
    except Exception as exc:
        return QuoteResult("阿里云", False, error=f"{type(exc).__name__}: {exc}")

    # 阿里云失败时返回 {"Code": "...", "Message": "..."}，HTTP 也可能是 200
    if isinstance(payload, dict) and payload.get("Code"):
        return QuoteResult(
            "阿里云", False, error=f"{payload.get('Code')}: {payload.get('Message')}"
        )

    try:
        price_info = (payload.get("PriceInfo") or {}).get("Price") or {}
        trade = float(price_info.get("TradePrice"))
        original = price_info.get("OriginalPrice")
        original = float(original) if original is not None else None
    except (TypeError, ValueError, AttributeError):
        return QuoteResult("阿里云", False, error=f"返回结构不符预期：{str(payload)[:200]}")

    return QuoteResult(
        "阿里云",
        True,
        Quote(
            vendor="阿里云",
            spec=spec,
            price=trade,
            list_price=original,
            period=f"{max(1, period // 12)}年",
            region=region,
        ),
    )


# ---------- 腾讯云（TC3-HMAC-SHA256）----------


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def tencent_authorization(
    secret_id: str,
    secret_key: str,
    service: str,
    host: str,
    action: str,
    payload: str,
    timestamp: int,
    region: str = "",
) -> str:
    """按腾讯云签名 v3 生成 Authorization 头。

    关键易错点：日期取 UTC+0（东八区跨零点时会差一天）；content-type 必须和实际
    发送的完全一致（带 ; charset=utf-8）；signed headers 按字典序，且必须都出现
    在 canonical headers 里。
    """
    content_type = "application/json; charset=utf-8"
    date = datetime.fromtimestamp(timestamp, UTC).strftime("%Y-%m-%d")
    canonical_headers = f"content-type:{content_type}\nhost:{host}\nx-tc-action:{action.lower()}\n"
    signed_headers = "content-type;host;x-tc-action"
    canonical_request = "\n".join(
        ["POST", "/", "", canonical_headers, signed_headers, _sha256_hex(payload.encode())]
    )
    credential_scope = f"{date}/{service}/tc3_request"
    string_to_sign = "\n".join(
        [
            "TC3-HMAC-SHA256",
            str(timestamp),
            credential_scope,
            _sha256_hex(canonical_request.encode()),
        ]
    )

    secret_date = hmac.new(f"TC3{secret_key}".encode(), date.encode(), hashlib.sha256).digest()
    secret_service = hmac.new(secret_date, service.encode(), hashlib.sha256).digest()
    secret_signing = hmac.new(secret_service, b"tc3_request", hashlib.sha256).digest()
    signature = hmac.new(secret_signing, string_to_sign.encode(), hashlib.sha256).hexdigest()

    return (
        f"TC3-HMAC-SHA256 Credential={secret_id}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )


async def _tencent_call(
    client: httpx.AsyncClient,
    secret_id: str,
    secret_key: str,
    action: str,
    body: dict,
    region: str,
    version: str = "2017-03-12",
) -> dict:
    host, service = "cvm.tencentcloudapi.com", "cvm"
    payload = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    timestamp = int(datetime.now(UTC).timestamp())
    headers = {
        "Authorization": tencent_authorization(
            secret_id, secret_key, service, host, action, payload, timestamp, region
        ),
        "Content-Type": "application/json; charset=utf-8",
        "Host": host,
        "X-TC-Action": action,
        "X-TC-Timestamp": str(timestamp),
        "X-TC-Version": version,
        "X-TC-Region": region,
    }
    resp = await client.post(f"https://{host}", content=payload, headers=headers)
    return resp.json().get("Response") or {}


async def _tencent_image_id(
    client: httpx.AsyncClient, secret_id: str, secret_key: str, region: str
) -> str:
    """询价必须带镜像 ID，随便取一个公共镜像即可（价格几乎不受镜像影响）。"""
    data = await _tencent_call(
        client,
        secret_id,
        secret_key,
        "DescribeImages",
        {
            "Limit": 1,
            "Filters": [{"Name": "image-type", "Values": ["PUBLIC_IMAGE"]}],
        },
        region,
    )
    images = data.get("ImageSet") or []
    if not images:
        err = data.get("Error") or {}
        # 带上错误码，否则用户只看到一句英文，不知道是密钥问题还是权限问题
        raise RuntimeError(
            f"{err.get('Code', 'NoImage')}: {err.get('Message') or '拿不到公共镜像'}"
        )
    return images[0]["ImageId"]


async def fetch_tencent_price(
    client: httpx.AsyncClient,
    secret_id: str,
    secret_key: str,
    spec: str,
    region: str = "ap-guangzhou",
    zone: str = "",
    period: int = 12,
) -> QuoteResult:
    instance_type = SPECS.get(spec, SPECS[DEFAULT_SPEC])["tencent"]
    try:
        image_id = await _tencent_image_id(client, secret_id, secret_key, region)
        body = {
            "Placement": {"Zone": zone or f"{region}-3"},
            "InstanceType": instance_type,
            "ImageId": image_id,
            "InstanceChargeType": "PREPAID",
            "InstanceChargePrepaid": {"Period": max(1, period // 12)},
            "InstanceCount": 1,
            "SystemDisk": {"DiskType": "CLOUD_SSD", "DiskSize": 50},
            "InternetAccessible": {
                "InternetChargeType": "TRAFFIC_POSTPAID_BY_HOUR",
                "InternetMaxBandwidthOut": 5,
            },
        }
        data = await _tencent_call(
            client, secret_id, secret_key, "InquiryPriceRunInstances", body, region
        )
    except Exception as exc:
        return QuoteResult("腾讯云", False, error=f"{type(exc).__name__}: {exc}")

    if data.get("Error"):
        err = data["Error"]
        return QuoteResult("腾讯云", False, error=f"{err.get('Code')}: {err.get('Message')}")

    price = data.get("Price") or {}
    try:
        trade = float(price.get("DiscountPrice", price.get("OriginalPrice")))
        original = price.get("OriginalPrice")
        original = float(original) if original is not None else None
    except (TypeError, ValueError):
        return QuoteResult("腾讯云", False, error=f"返回结构不符预期：{str(data)[:200]}")

    return QuoteResult(
        "腾讯云",
        True,
        Quote(
            vendor="腾讯云",
            spec=spec,
            price=trade,
            list_price=original,
            period=f"{max(1, period // 12)}年",
            region=region,
        ),
    )


# ---------- 统一入口 ----------


async def compare(spec: str = DEFAULT_SPEC) -> tuple[list[QuoteResult], str]:
    """问两家价格，返回 (结果列表, 说明为什么某个源没跑)。

    密钥只从插件配置读；缺谁就跳过谁，并在返回里说明，不要静默变成「只有一家」。
    """
    settings = get_settings()
    client = httpx.AsyncClient(timeout=settings.timeout + 4, follow_redirects=True)
    skipped: list[str] = []
    tasks = []
    try:
        if settings.aliyun_ak and settings.aliyun_sk:
            tasks.append(fetch_aliyun_price(client, settings.aliyun_ak, settings.aliyun_sk, spec))
        else:
            skipped.append("阿里云（未填 AccessKey）")
        if settings.tencent_secret_id and settings.tencent_secret_key:
            tasks.append(
                fetch_tencent_price(
                    client, settings.tencent_secret_id, settings.tencent_secret_key, spec
                )
            )
        else:
            skipped.append("腾讯云（未填 SecretId）")

        results = list(await asyncio.gather(*tasks)) if tasks else []
    finally:
        await client.aclose()

    for result in results:
        if not result.ok:
            get_logger().warning("云比价 %s 失败：%s", result.vendor, result.error)
    return results, "、".join(skipped)
