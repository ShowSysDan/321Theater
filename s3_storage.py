# 3·2·1→Theater
# © 2026 Dr. Phillips Center for the Performing Arts; portions © 2026 Thauma Systems, LLC.
# MIT Licensed — see LICENSE for details.
"""
SeaweedFS / S3-compatible object storage for 321Theater.

Configuration comes from one of two sources, chosen by the admin
(Settings → System → Database → File Storage; `s3_config_source` app_setting):

  - 'ini' (the default, and the historical behavior): the [seaweedfs] section
    of db_config.ini next to this file — a single endpoint.
  - 'gui': app_settings rows (s3_endpoints JSON list, s3_access_key,
    s3_secret_key, s3_bucket) managed in the Settings UI and stored in the
    live database, supporting MULTIPLE endpoints.

app.py injects the GUI reader via set_settings_provider() — this module never
imports app.py. If the provider is unset, errors, or the source is 'ini',
db_config.ini is used exactly as before, so existing deployments keep working
untouched.

Multiple endpoints (e.g. two SeaweedFS S3 gateways fronting one cluster) are
tried in order with automatic failover: the last endpoint that worked is
preferred for subsequent calls, and every failover / endpoint error is logged
through the 'showadvance' logger (S3_ENDPOINT_ERROR / S3_FAILOVER), which
app.py forwards to syslog.

Results are cached for 30 s to avoid re-reading config on every request.

Key naming scheme (single bucket):
  attachments/{show_id}/{aid}/{filename}
  exports/{show_id}/{export_type}/v{version}.pdf
  asset-photos/{type_id}
  external-rentals/{er_id}/{filename}
"""
import configparser
import logging
import os
import time

_logger = logging.getLogger('showadvance')  # same logger app.py wires to syslog

# ─── Settings Cache ────────────────────────────────────────────────────────────
_settings_cache: dict = {}
_settings_ts: float = 0.0
_CACHE_TTL = 30  # seconds

_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'db_config.ini')

# Injected by app.py: a zero-arg callable returning a settings dict (see
# read_s3_settings for the shape) when the GUI source is active, or None to
# fall back to db_config.ini. Kept as injection to avoid a circular import.
_settings_provider = None

# Index (into s3_endpoints) of the endpoint that most recently succeeded —
# preferred first on subsequent operations so a dead primary isn't retried
# on every single call. Benign under Gunicorn threads (worst case: one extra
# attempt against a dead endpoint).
_preferred_idx: int = 0


def set_settings_provider(fn) -> None:
    """Register the GUI-settings reader (called once by app.py at import)."""
    global _settings_provider
    _settings_provider = fn
    clear_settings_cache()


def _read_ini_settings() -> dict:
    """The historical config source: db_config.ini [seaweedfs]."""
    result = {}
    if os.path.exists(_CONFIG_PATH):
        try:
            cp = configparser.ConfigParser()
            cp.read(_CONFIG_PATH, encoding='utf-8')
            if 'seaweedfs' in cp:
                sec = cp['seaweedfs']
                endpoint = sec.get('endpoint', '').rstrip('/')
                result = {
                    's3_endpoints':  [endpoint] if endpoint else [],
                    's3_access_key': sec.get('access_key', ''),
                    's3_secret_key': sec.get('secret_key', ''),
                    's3_bucket':     sec.get('bucket', '321theater'),
                    's3_source':     'ini',
                }
        except Exception:
            pass
    return result


def read_s3_settings() -> dict:
    """Return the active S3 settings (GUI source if selected, else ini).

    Shape: {'s3_endpoints': [url, ...], 's3_endpoint': first-url-or-'',
            's3_access_key', 's3_secret_key', 's3_bucket', 's3_source'}.
    Cached 30 s.
    """
    global _settings_cache, _settings_ts
    if _settings_cache and (time.time() - _settings_ts) < _CACHE_TTL:
        return _settings_cache
    cfg = None
    if _settings_provider is not None:
        try:
            cfg = _settings_provider()   # dict when GUI source active, else None
        except Exception as e:
            _logger.error(f'S3_SETTINGS_PROVIDER_ERROR error={e} — falling back to db_config.ini')
            cfg = None
    if cfg is None:
        cfg = _read_ini_settings()
        cfg.setdefault('s3_source', 'ini')
    else:
        cfg = dict(cfg)
        cfg.setdefault('s3_source', 'gui')
    endpoints = [str(e).strip().rstrip('/') for e in (cfg.get('s3_endpoints') or []) if str(e).strip()]
    cfg['s3_endpoints'] = endpoints
    cfg['s3_endpoint'] = endpoints[0] if endpoints else ''   # legacy single-endpoint key
    cfg.setdefault('s3_access_key', '')
    cfg.setdefault('s3_secret_key', '')
    if not cfg.get('s3_bucket'):
        cfg['s3_bucket'] = '321theater'
    _settings_cache = cfg
    _settings_ts = time.time()
    return cfg


def clear_settings_cache():
    """Invalidate the settings cache (call after saving S3 settings)."""
    global _settings_cache, _settings_ts, _preferred_idx
    _settings_cache = {}
    _settings_ts = 0.0
    _preferred_idx = 0


def is_configured() -> bool:
    """Return True if at least one endpoint plus credentials are configured."""
    cfg = read_s3_settings()
    return bool(cfg.get('s3_endpoints') and cfg.get('s3_access_key') and cfg.get('s3_secret_key'))


def get_client(endpoint: str = None):
    """Return a boto3 S3 client pointed at *endpoint* (default: primary)."""
    import boto3
    from botocore.config import Config
    cfg = read_s3_settings()
    return boto3.client(
        's3',
        endpoint_url=endpoint or cfg['s3_endpoint'],
        aws_access_key_id=cfg['s3_access_key'],
        aws_secret_access_key=cfg['s3_secret_key'],
        config=Config(signature_version='s3v4',
                      connect_timeout=10, read_timeout=60,
                      retries={'max_attempts': 1}),
    )


def _bucket() -> str:
    return read_s3_settings().get('s3_bucket', '321theater')


def _with_failover(op_name: str, fn):
    """Run *fn(client)* against each configured endpoint until one succeeds.

    The endpoint that last succeeded is tried first. All gateways front the
    same SeaweedFS cluster, so any endpoint is equivalent for reads, writes
    and deletes. Raises the last error when every endpoint fails.
    """
    global _preferred_idx
    endpoints = read_s3_settings().get('s3_endpoints') or []
    if not endpoints:
        raise RuntimeError('No S3 endpoints configured')
    start = _preferred_idx if _preferred_idx < len(endpoints) else 0
    order = [(i % len(endpoints)) for i in range(start, start + len(endpoints))]
    last_err = None
    for pos, idx in enumerate(order):
        ep = endpoints[idx]
        try:
            result = fn(get_client(ep))
            if pos > 0:
                _logger.warning(f'S3_FAILOVER op={op_name} endpoint={ep} '
                                f'(primary {endpoints[order[0]]} unavailable)')
            _preferred_idx = idx
            return result
        except Exception as e:
            _logger.error(f'S3_ENDPOINT_ERROR op={op_name} endpoint={ep} error={e}')
            last_err = e
    raise last_err


def upload_file(key: str, data: bytes, content_type: str = 'application/octet-stream') -> None:
    """Upload *data* to S3 at *key*.  Raises on failure of every endpoint."""
    import io

    def _op(client):
        client.put_object(
            Bucket=_bucket(),
            Key=key,
            Body=io.BytesIO(data),
            ContentType=content_type,
            ContentLength=len(data),
        )
    _with_failover('upload', _op)


def download_file(key: str) -> bytes:
    """Download and return bytes for *key*.  Raises on failure of every endpoint."""
    def _op(client):
        resp = client.get_object(Bucket=_bucket(), Key=key)
        return resp['Body'].read()
    return _with_failover('download', _op)


def delete_file(key: str) -> None:
    """Delete *key* from S3.  Raises on failure of every endpoint."""
    def _op(client):
        client.delete_object(Bucket=_bucket(), Key=key)
    _with_failover('delete', _op)


def test_connection() -> dict:
    """
    Verify connectivity by uploading, reading back, and deleting a test object
    on EVERY configured endpoint individually (no failover — a broken gateway
    must show as broken here even when its sibling covers for it in normal use).

    Returns {"success": bool, "message": str, "endpoint": str, "bucket": str,
             "source": "ini"|"gui", "endpoints": [{endpoint, success, message}]}.
    Top-level success is True when at least one endpoint works.
    """
    cfg = read_s3_settings()
    bucket = cfg.get('s3_bucket', '')
    source = cfg.get('s3_source', 'ini')
    if not is_configured():
        where = ('db_config.ini' if source == 'ini'
                 else 'Settings → System → Database → File Storage')
        return {'success': False, 'message': f'S3 storage not configured in {where}.',
                'endpoint': cfg.get('s3_endpoint', ''), 'bucket': bucket,
                'source': source, 'endpoints': []}
    test_key = '_321theater_s3_test.txt'
    test_data = b'321Theater S3 connectivity test'
    results = []
    for ep in cfg['s3_endpoints']:
        try:
            client = get_client(ep)
            import io
            client.put_object(Bucket=bucket, Key=test_key, Body=io.BytesIO(test_data),
                              ContentType='text/plain', ContentLength=len(test_data))
            fetched = client.get_object(Bucket=bucket, Key=test_key)['Body'].read()
            client.delete_object(Bucket=bucket, Key=test_key)
            if fetched != test_data:
                results.append({'endpoint': ep, 'success': False,
                                'message': 'Data mismatch on read-back.'})
            else:
                results.append({'endpoint': ep, 'success': True, 'message': 'OK'})
        except Exception as e:
            results.append({'endpoint': ep, 'success': False, 'message': str(e)})
    ok = [r for r in results if r['success']]
    n = len(results)
    message = (f'{len(ok)}/{n} endpoint(s) OK.' if ok
               else (results[0]['message'] if n == 1 else f'All {n} endpoints failed.'))
    return {'success': bool(ok), 'message': message,
            'endpoint': cfg.get('s3_endpoint', ''), 'bucket': bucket,
            'source': source, 'endpoints': results}
