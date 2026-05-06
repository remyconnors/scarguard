"""ScarGuard speciesnet service — classifies bird detections via SpeciesNet.

Subscribes to ``scarguard:detections``, verifies HMAC, crops the bbox
out of the on-disk snapshot, calls the configured SpeciesNet HTTP API,
persists the result to ``/data/speciesnet.db``, and re-publishes a
condensed payload on ``scarguard:species`` so the web UI can patch the
matching event row live.

Heavy work (HTTP upload + 60-180s polling) runs in a bounded worker
pool so the Redis listen loop stays responsive — a parade of bird
detections can never starve the service of its ability to consume new
events.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import db as species_db
import publisher
import redis as redis_lib
import yaml
from atomic_ref import AtomicRef
from client import (
    Prediction,
    SpeciesNetClient,
    SpeciesNetError,
    SpeciesNetTimeout,
    scrub_secrets,
)
from config_watcher import ConfigWatcher
from cropper import crop_to_jpeg
from healthcheck import start_heartbeat
from settings import SpeciesNetSettings, from_yaml
from url_safety import UnsafeURLError

logger = logging.getLogger(__name__)

CONFIG_PATH = os.environ.get("CONFIG_PATH", "/config/scarguard.yml")
SNAPSHOT_DIR = os.environ.get("SNAPSHOT_DIR", "/data/snapshots")
CHANNEL = "scarguard:detections"

_REDIS_RECONNECT_DELAY = 5
_REDIS_MAX_RECONNECT_DELAY = 60


def _is_under_snapshot_dir(path: str) -> bool:
    """Return True iff *path* resolves inside SNAPSHOT_DIR.

    Defence in depth — HMAC verification protects the detection bus, but
    a compromised detector or a config typo could deliver a snapshot_path
    pointing at /etc/shadow. PIL would just fail to decode it, but we'd
    rather refuse the open than rely on PIL's defensive parsing.
    """
    import pathlib
    try:
        target = pathlib.Path(path).resolve()
        base = pathlib.Path(SNAPSHOT_DIR).resolve()
        return target.is_relative_to(base)
    except (ValueError, OSError):
        return False


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f) or {}
    return cfg if isinstance(cfg, dict) else {}


def setup_logging(log_level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        stream=sys.stdout,
    )


def _build_client(settings: SpeciesNetSettings) -> SpeciesNetClient | None:
    """Construct a :class:`SpeciesNetClient` if the config is usable.

    Returns ``None`` when classification is disabled or the API URL is
    missing/invalid.  Logs the reason at INFO so an operator can see why
    classifications aren't happening.
    """
    if not settings.enabled:
        logger.info("speciesnet disabled via config — events will not be classified")
        return None
    if not settings.api_url:
        logger.warning("speciesnet enabled but api_url is empty — disabling")
        return None
    try:
        return SpeciesNetClient(
            settings.api_url,
            api_token=settings.api_token,
            timeout=settings.timeout_seconds,
            poll_interval=settings.poll_interval_seconds,
            max_polls=settings.max_polls,
        )
    except UnsafeURLError as exc:
        logger.error("speciesnet api_url rejected: %s", exc)
        return None


def _classify_one(
    event: dict,
    settings: SpeciesNetSettings,
    client: SpeciesNetClient,
    redis_client: redis_lib.Redis,
) -> None:
    """Run one detection through cropping, classification, persistence, and publish.

    Designed to be submitted to a worker pool — never raises; failures
    are logged and recorded in the species DB so the web UI can show an
    error state instead of a stuck "classifying…" badge.
    """
    feedback_token = event.get("feedback_token")
    snapshot_path = event.get("snapshot_path")
    bbox = event.get("bbox")
    camera = event.get("camera_name", "")
    detection_class = event.get("class_name", "")

    if not feedback_token:
        logger.warning("Skipping event without feedback_token (camera=%s class=%s)", camera, detection_class)
        return
    if not snapshot_path or not bbox:
        logger.info(
            "Skipping %s — missing snapshot_path or bbox (token=%s)",
            detection_class, feedback_token,
        )
        return

    if not _is_under_snapshot_dir(snapshot_path):
        logger.warning(
            "Refusing to open snapshot outside %s: %s (token=%s)",
            SNAPSHOT_DIR, snapshot_path, feedback_token,
        )
        species_db.insert_pending(feedback_token, camera, detection_class)
        species_db.update_failure(
            feedback_token, status="error",
            error=f"snapshot_path outside {SNAPSHOT_DIR}",
        )
        return

    species_db.insert_pending(feedback_token, camera, detection_class)

    try:
        bbox_tuple = (int(bbox[0]), int(bbox[1]), int(bbox[2]), int(bbox[3]))
    except (TypeError, ValueError, IndexError):
        species_db.update_failure(feedback_token, status="error", error=f"bad bbox: {bbox!r}")
        return

    try:
        image_bytes = crop_to_jpeg(
            snapshot_path,
            bbox_tuple,
            padding_pct=settings.bbox_padding_pct,
        )
    except FileNotFoundError:
        species_db.update_failure(
            feedback_token, status="error",
            error=f"snapshot missing: {snapshot_path}",
        )
        return
    except Exception as exc:  # crop / PIL errors
        species_db.update_failure(feedback_token, status="error", error=f"crop failed: {exc}")
        logger.exception("Crop failed for %s", feedback_token)
        return

    started = time.monotonic()
    try:
        predictions = client.classify(image_bytes, filename=f"{feedback_token}.jpg")
    except SpeciesNetTimeout as exc:
        logger.warning("SpeciesNet timeout for %s: %s", feedback_token, exc)
        species_db.update_failure(
            feedback_token, status="timeout", error=scrub_secrets(str(exc)),
        )
        return
    except SpeciesNetError as exc:
        logger.error("SpeciesNet error for %s: %s", feedback_token, exc)
        species_db.update_failure(
            feedback_token, status="error", error=scrub_secrets(str(exc)),
        )
        return
    except Exception as exc:  # network / unexpected
        logger.exception("Unexpected error classifying %s", feedback_token)
        species_db.update_failure(
            feedback_token, status="error",
            error=scrub_secrets(f"{type(exc).__name__}: {exc}"),
        )
        return

    elapsed = time.monotonic() - started
    if not predictions:
        species_db.update_failure(
            feedback_token, status="error", error="empty predictions list",
        )
        return

    top = predictions[0]
    species_db.update_success(
        feedback_token,
        common_name=top.common_name,
        species=top.species,
        genus=top.genus,
        family=top.family,
        order=top.order,
        class_=top.class_,
        score=top.score,
        geofenced=top.geofenced,
        top_predictions=[_pred_to_dict(p) for p in predictions[:5]],
    )

    publisher.publish(redis_client, {
        "feedback_token": feedback_token,
        "camera_name": camera,
        "common_name": top.common_name,
        "species": top.species,
        "score": top.score,
        "geofenced": top.geofenced,
        "elapsed_seconds": round(elapsed, 2),
        "completed_at": datetime.now(timezone.utc).isoformat(),
    })

    logger.info(
        "Classified %s (camera=%s, %s, conf=%.2f, %.1fs)",
        feedback_token, camera, top.common_name or top.species or "?",
        top.score, elapsed,
    )


def _pred_to_dict(p: Prediction) -> dict:
    return {
        "common_name": p.common_name,
        "species": p.species,
        "genus": p.genus,
        "family": p.family,
        "order": p.order,
        "class": p.class_,
        "score": p.score,
        "geofenced": p.geofenced,
    }


def subscribe_loop(
    redis_cfg: dict,
    settings_ref: AtomicRef[SpeciesNetSettings],
    client_ref: AtomicRef[SpeciesNetClient | None],
    pool: ThreadPoolExecutor,
    shutdown_event: threading.Event,
    publish_client: redis_lib.Redis,
) -> None:
    """Connect to Redis, listen for detections, dispatch to the worker pool.

    *publish_client* is a separate Redis connection used by worker threads
    to publish ``scarguard:species`` results — kept distinct from the
    subscriber connection so a subscribe-side reconnect does not break
    in-flight worker publishes.
    """
    from event_signing import load_key_from_env, verify_event

    host = redis_cfg.get("host", "redis")
    port = int(redis_cfg.get("port", 6379))
    hmac_key = load_key_from_env()
    if hmac_key is None:
        logger.warning("DETECTION_HMAC_KEY not set — accepting unsigned events")

    delay = _REDIS_RECONNECT_DELAY
    invalid_warned = False
    unsigned_warned = False

    while not shutdown_event.is_set():
        client: redis_lib.Redis | None = None
        pubsub: redis_lib.client.PubSub | None = None
        try:
            redis_password = os.environ.get("REDIS_PASSWORD", "") or None
            client = redis_lib.Redis(
                host=host, port=port,
                password=redis_password, decode_responses=True,
            )
            pubsub = client.pubsub()
            pubsub.subscribe(CHANNEL)
            logger.info("Subscribed to %s", CHANNEL)
            delay = _REDIS_RECONNECT_DELAY
            pathlib.Path("/tmp/healthy").touch(exist_ok=True)

            for message in pubsub.listen():
                if shutdown_event.is_set():
                    break
                if message["type"] != "message":
                    continue
                pathlib.Path("/tmp/healthy").touch(exist_ok=True)
                try:
                    event = json.loads(message["data"])
                except json.JSONDecodeError:
                    logger.warning("Malformed message dropped")
                    continue

                if hmac_key is not None:
                    if not verify_event(event, hmac_key):
                        if not invalid_warned:
                            logger.error(
                                "Rejecting detection event with invalid signature — "
                                "NOT classifying. Further invalid events at DEBUG.",
                            )
                            invalid_warned = True
                        else:
                            logger.debug("Invalid-signature event rejected")
                        continue
                elif not unsigned_warned:
                    unsigned_warned = True
                    logger.warning("Accepting unsigned event. Further unsigned events at DEBUG.")

                settings = settings_ref.get()
                speciesnet_client = client_ref.get()
                if speciesnet_client is None:
                    continue
                if not settings.applies_to(
                    event.get("class_name", ""),
                    float(event.get("confidence", 0.0) or 0.0),
                ):
                    logger.debug(
                        "Skipping %s — does not match trigger filter",
                        event.get("class_name"),
                    )
                    continue

                try:
                    pool.submit(
                        _classify_one, event, settings, speciesnet_client, publish_client,
                    )
                except RuntimeError:
                    # Pool is shutting down
                    break

        except redis_lib.RedisError:
            if shutdown_event.is_set():
                break
            logger.exception("Redis connection lost — retrying in %ds", delay)
            time.sleep(delay)
            delay = min(delay * 2, _REDIS_MAX_RECONNECT_DELAY)
        finally:
            if pubsub is not None:
                try:
                    pubsub.unsubscribe()
                    pubsub.close()
                except Exception:
                    pass
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass

    logger.info("Subscription loop exited")


def main() -> None:
    cfg = load_config()
    setup_logging(cfg.get("system", {}).get("log_level", "info"))
    logger.info("ScarGuard speciesnet starting")

    species_db.init_db()
    start_heartbeat()

    settings = from_yaml(cfg)
    client = _build_client(settings)
    settings_ref: AtomicRef[SpeciesNetSettings] = AtomicRef(settings)
    client_ref: AtomicRef[SpeciesNetClient | None] = AtomicRef(client)

    pool = ThreadPoolExecutor(
        max_workers=max(1, settings.max_concurrent),
        thread_name_prefix="speciesnet-worker",
    )

    shutdown_event = threading.Event()

    def _shutdown(sig: int, _frame: object) -> None:
        logger.info("Received signal %s — shutting down", sig)
        shutdown_event.set()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    def _on_config_change(new_cfg: dict) -> None:
        new_settings = from_yaml(new_cfg)
        new_client = _build_client(new_settings)
        settings_ref.set(new_settings)
        client_ref.set(new_client)
        logger.info(
            "Config reloaded — speciesnet %s (api=%s, triggers=%s)",
            "enabled" if new_settings.enabled and new_client else "disabled",
            new_settings.api_url or "(unset)",
            new_settings.trigger_classes,
        )

    watcher = ConfigWatcher(CONFIG_PATH, _on_config_change)
    watcher.start()

    redis_cfg = cfg.get("redis", {})
    publish_client = redis_lib.Redis(
        host=redis_cfg.get("host", "redis"),
        port=int(redis_cfg.get("port", 6379)),
        password=os.environ.get("REDIS_PASSWORD", "") or None,
        decode_responses=True,
    )
    try:
        subscribe_loop(
            redis_cfg, settings_ref, client_ref, pool, shutdown_event, publish_client,
        )
    finally:
        try:
            publish_client.close()
        except Exception:
            pass

    watcher.stop()
    pool.shutdown(wait=True, cancel_futures=False)
    logger.info("speciesnet stopped cleanly")


if __name__ == "__main__":
    main()
