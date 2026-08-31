#!/usr/bin/env python3
"""Fetch datasheet PDFs from any S3-compatible storage — without boto3 and
without ever putting credentials in a chat, a repo or a command line.

Runs on YOUR machine (where the storage is reachable), not in a sandbox.

    # 1. check the endpoint is reachable at all (no credentials needed)
    python3 tools/rag/fetch_s3.py --probe --endpoint https://s3.example.com

    # 2. list what is in the bucket
    export AWS_ACCESS_KEY_ID=...
    export AWS_SECRET_ACCESS_KEY=...
    export AWS_ENDPOINT_URL=https://s3.example.com
    export S3_BUCKET=datasheets
    python3 tools/rag/fetch_s3.py --list --prefix smd/

    # 3. download into the corpus folder
    python3 tools/rag/fetch_s3.py --prefix smd/ --out data/datasheets --limit 50

    # 4. or: print time-limited presigned URLs instead of downloading
    python3 tools/rag/fetch_s3.py --presign --expires 3600

Exit codes: 0 ok, 1 usage/runtime error, 2 endpoint unreachable.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

S3_ENV = {
    "key": "AWS_ACCESS_KEY_ID",
    "secret": "AWS_SECRET_ACCESS_KEY",
    "token": "AWS_SESSION_TOKEN",
    "endpoint": "AWS_ENDPOINT_URL",
    "region": "AWS_REGION",
    "bucket": "S3_BUCKET",
}


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


# --------------------------------------------------------------------------- #
# SigV4 — ~40 lines of stdlib instead of a boto3 dependency
# --------------------------------------------------------------------------- #

def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# SHA256 of an empty body — the payload hash S3 expects for body-less GETs.
EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
UNSIGNED_PAYLOAD = "UNSIGNED-PAYLOAD"


def sign(method: str, url: str, headers: dict, key: str, secret: str, region: str,
         service: str = "s3", token: str = "", payload: str = EMPTY_SHA256,
         when: datetime | None = None) -> dict:
    when = when or datetime.now(timezone.utc)
    amz_date = when.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = when.strftime("%Y%m%d")

    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc
    path = parsed.path or "/"
    query = parsed.query

    signed_headers = sorted(h.lower() for h in headers)
    canonical_headers = "".join("%s:%s\n" % (h, str(headers_get(headers, h)).strip())
                                for h in signed_headers)

    canonical_request = "\n".join([
        method.upper(), path, query, canonical_headers,
        ";".join(signed_headers), payload,
    ])
    scope = "%s/%s/%s/aws4_request" % (date_stamp, region, service)
    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256", amz_date, scope, _sha256(canonical_request.encode()),
    ])

    k = _hmac(("AWS4" + secret).encode(), date_stamp)
    for part in (region, service, "aws4_request"):
        k = _hmac(k, part)
    signature = hmac.new(k, string_to_sign.encode(), hashlib.sha256).hexdigest()

    auth = ("AWS4-HMAC-SHA256 Credential=%s/%s, SignedHeaders=%s, Signature=%s"
            % (key, scope, ";".join(signed_headers), signature))
    out = dict(headers)
    out["Authorization"] = auth
    out["X-Amz-Date"] = amz_date
    out["X-Amz-Content-Sha256"] = payload
    if token:
        out["X-Amz-Security-Token"] = token
    return out


def headers_get(headers: dict, name: str) -> str:
    for k, v in headers.items():
        if k.lower() == name.lower():
            return str(v)
    return ""


def presign(method: str, url: str, key: str, secret: str, region: str,
            expires: int = 3600, token: str = "") -> str:
    parsed = urllib.parse.urlparse(url)
    q = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
    q.update({
        "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
        "X-Amz-Credential": "%s/%s/%s/s3/aws4_request" % (
            key, datetime.now(timezone.utc).strftime("%Y%m%d"), region),
        "X-Amz-Date": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        "X-Amz-Expires": str(expires),
        "X-Amz-SignedHeaders": "host",
    })
    if token:
        q["X-Amz-Security-Token"] = token

    canonical_query = urllib.parse.urlencode(sorted(q.items()), quote_via=urllib.parse.quote)
    canonical_request = "\n".join([
        method.upper(), parsed.path or "/", canonical_query,
        "host:%s\n" % parsed.netloc, "host", "UNSIGNED-PAYLOAD",
    ])
    when = datetime.now(timezone.utc)
    date_stamp = when.strftime("%Y%m%d")
    scope = "%s/%s/s3/aws4_request" % (date_stamp, region)
    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256", when.strftime("%Y%m%dT%H%M%SZ"), scope,
        _sha256(canonical_request.encode()),
    ])
    k = _hmac(("AWS4" + secret).encode(), date_stamp)
    for part in (region, "s3", "aws4_request"):
        k = _hmac(k, part)
    signature = hmac.new(k, string_to_sign.encode(), hashlib.sha256).hexdigest()

    return urllib.parse.urlunparse(parsed._replace(
        query="%s&X-Amz-Signature=%s" % (canonical_query, signature)))


# --------------------------------------------------------------------------- #
# client
# --------------------------------------------------------------------------- #

class S3:
    def __init__(self, endpoint: str, bucket: str, region: str = "us-east-1",
                 key: str = "", secret: str = "", token: str = "", path_style: bool = True):
        self.endpoint = endpoint.rstrip("/")
        self.bucket = bucket
        self.region = region
        self.key = key
        self.secret = secret
        self.token = token
        self.path_style = path_style
        self.anonymous = not (key and secret)

    def _url(self, object_key: str = "", query: dict | None = None) -> str:
        host = urllib.parse.urlparse(self.endpoint).netloc
        scheme = urllib.parse.urlparse(self.endpoint).scheme or "https"
        if self.path_style:
            path = "/%s/%s" % (self.bucket, object_key.lstrip("/"))
        else:
            path = "/%s" % object_key.lstrip("/") if object_key else "/"
            host = "%s.%s" % (self.bucket, host)
        q = urllib.parse.urlencode(query or {}, quote_via=urllib.parse.quote)
        return urllib.parse.urlunparse((scheme, host, urllib.parse.quote(path), "", q, ""))

    def _request(self, method: str, url: str, body: bytes | None = None) -> bytes:
        headers = {"Host": urllib.parse.urlparse(url).netloc}
        if body is not None:
            headers["Content-Length"] = str(len(body))
        if not self.anonymous:
            # GET carries no body, so the signed payload hash is the empty-body hash
            headers = sign(method, url, headers, self.key, self.secret, self.region,
                           token=self.token,
                           payload=UNSIGNED_PAYLOAD if body is not None else EMPTY_SHA256)
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.read()

    def probe(self) -> tuple[bool, str]:
        try:
            urllib.request.urlopen(self.endpoint, timeout=15)
            return True, "reachable"
        except urllib.error.HTTPError as exc:
            return True, "reachable (HTTP %d — anonymous request rejected)" % exc.code
        except Exception as exc:
            return False, "%s: %s" % (type(exc).__name__, exc)

    def list_objects(self, prefix: str = "", limit: int = 1000) -> list[dict]:
        query = {"list-type": "2", "prefix": prefix, "max-keys": str(limit)}
        data = self._request("GET", self._url("", query))
        ns = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}
        root = ET.fromstring(data)
        out = []
        for item in root.findall("s3:Contents", ns):
            out.append({
                "key": (item.findtext("s3:Key", "", ns) or ""),
                "size": int(item.findtext("s3:Size", "0", ns) or 0),
            })
        return out

    def download(self, object_key: str, dest: Path) -> int:
        data = self._request("GET", self._url(object_key))
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        tmp.write_bytes(data)
        tmp.replace(dest)
        return len(data)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Fetch datasheet PDFs from S3-compatible storage (no boto3)")
    ap.add_argument("--endpoint", default=_env("AWS_ENDPOINT_URL"))
    ap.add_argument("--bucket", default=_env("S3_BUCKET"))
    ap.add_argument("--region", default=_env("AWS_REGION", "us-east-1"))
    ap.add_argument("--prefix", default="")
    ap.add_argument("--out", type=Path, default=Path("data/datasheets"))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--ext", default=".pdf")
    ap.add_argument("--list", action="store_true", help="only list objects")
    ap.add_argument("--presign", action="store_true", help="print presigned URLs")
    ap.add_argument("--expires", type=int, default=3600)
    ap.add_argument("--anonymous", action="store_true", help="public bucket, no credentials")
    ap.add_argument("--probe", action="store_true", help="connectivity check only")
    ap.add_argument("--virtual-host", action="store_true", help="use virtual-host addressing")
    args = ap.parse_args()

    if args.probe:
        if not args.endpoint:
            log("no endpoint: set AWS_ENDPOINT_URL or pass --endpoint")
            return 1
        client = S3(args.endpoint, args.bucket or "probe")
        ok, msg = client.probe()
        print("%s %s" % ("OK  " if ok else "FAIL", msg))
        return 0 if ok else 2

    if not args.endpoint or not args.bucket:
        log("need --endpoint and --bucket (or AWS_ENDPOINT_URL / S3_BUCKET)")
        return 1

    key, secret, token = "", "", ""
    if not args.anonymous:
        key, secret, token = _env("AWS_ACCESS_KEY_ID"), _env("AWS_SECRET_ACCESS_KEY"), _env("AWS_SESSION_TOKEN")
        if not (key and secret):
            log("no credentials in the environment. Either export them:")
            log("  AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_ENDPOINT_URL / S3_BUCKET")
            log("or pass --anonymous for a public bucket, or --probe to only test reachability.")
            return 1

    client = S3(args.endpoint, args.bucket, args.region, key, secret, token,
                path_style=not args.virtual_host)

    objects = client.list_objects(args.prefix)
    if args.ext:
        objects = [o for o in objects if o["key"].lower().endswith(args.ext.lower())]
    if args.limit:
        objects = objects[: args.limit]

    if not objects:
        log("nothing found: bucket=%s prefix=%r" % (args.bucket, args.prefix))
        return 1

    if args.list:
        for o in objects:
            print("%10.1f KB  %s" % (o["size"] / 1024, o["key"]))
        print("\n%d objects" % len(objects))
        return 0

    if args.presign:
        for o in objects:
            url = client._url(o["key"])
            print(presign("GET", url, key, secret, args.region, args.expires, token))
        return 0

    downloaded = 0
    for i, o in enumerate(objects, start=1):
        name = Path(o["key"]).name or ("object-%03d.pdf" % i)
        dest = args.out / name
        if dest.exists() and dest.stat().st_size == o["size"]:
            print("  [%2d/%d] %-46s cached" % (i, len(objects), name))
            downloaded += 1
            continue
        size = client.download(o["key"], dest)
        print("  [%2d/%d] %-46s %6.1f KB" % (i, len(objects), name, size / 1024))
        downloaded += 1
    print("\n%d files in %s" % (downloaded, args.out))
    print("Next: python3 tools/rag/pipeline.py --corpus %s --rebuild" % args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
