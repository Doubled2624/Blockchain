from __future__ import annotations

import base64
import csv
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock, Thread
from typing import Any

import cv2
import numpy as np
import supervision as sv
from flask import Flask, Response, jsonify, redirect, request, send_from_directory
from ultralytics import YOLO

from backend.hash_utils import generate_evidence_hash
from backend.sqlserver_violations_db import SqlServerViolationStore
from backend.violations_db import BLOCKCHAIN_STATUSES, ViolationStore
from config import IN_VIDEO_PATH, YOLO_MODEL_PATH
from src import SpeedEstimator, ViewTransformer


ROOT = Path(__file__).resolve().parent
WEB_DIR = ROOT / "web"
DATA_DIR = ROOT / "data"
BLOCKCHAIN_DIR = ROOT / "blockchain"
VIOLATION_IMAGE_DIR = DATA_DIR / "violations"
VIOLATION_DB = DATA_DIR / "violations.sqlite3"
PLATE_TRACKING_CSV = DATA_DIR / "license_plate_tracking.csv"
PLATE_TRACKING_JSON = DATA_DIR / "license_plate_tracking.json"
MODEL_OPTIONS = {
    "UAV trained best.pt": ROOT / "runs" / "uav_benchmark_m_visdrone_turbo_to_200" / "weights" / "best.pt",
    "VisDrone_YOLO_x2.pt": ROOT / "models" / "VisDrone_YOLO_x2.pt",
    "yolov8n.pt": ROOT / "models" / "yolov8n.pt",
    "Custom YOLO model": Path(YOLO_MODEL_PATH),
}
LICENSE_PLATE_MODEL_CANDIDATES = [
    ROOT / "models" / "license_plate.pt",
    ROOT / "models" / "license_plate_best.pt",
]


@dataclass
class RuntimeConfig:
    source_type: str = "video"
    video_path: Path = ROOT / IN_VIDEO_PATH
    camera_index: int = 0
    camera_url: str = ""
    model_name: str = "UAV trained best.pt"
    confidence: float = 0.45
    speed_limit: int = 60
    line_y: int = 480
    real_width: float = 25.0
    real_height: float = 100.0
    frame_skip: int = 1
    stream_fps: int = 0
    inference_width: int = 640
    jpeg_quality: int = 88
    license_plate_enabled: bool = True
    license_plate_confidence: float = 0.35
    license_plate_ocr_enabled: bool = False
    license_plate_ocr_confidence: float = 0.6
    license_plate_scan_interval: int = 5
    zone_canvas: list[dict[str, float]] = field(default_factory=list)
    canvas_size: tuple[float, float] = (1.0, 1.0)
    stats: dict[str, Any] = field(default_factory=dict)
    version: int = 0


app = Flask(__name__, static_folder=None)
state = RuntimeConfig()
state_lock = Lock()
model_cache_lock = Lock()
warmup_lock = Lock()
model_cache: dict[str, YOLO] = {}
ocr_reader: Any | None = None
ocr_load_failed = False
cuda_available: bool | None = None
warmup_started = False
warmup_done = False
warmup_error = ""
warmup_thread: Thread | None = None


def create_violation_store() -> Any:
    provider = os.getenv("VIOLATION_DB_PROVIDER", "sqlite").strip().lower()
    if provider == "sqlserver":
        return SqlServerViolationStore(os.getenv("SQLSERVER_CONNECTION_STRING", ""))
    return ViolationStore(VIOLATION_DB)


violation_store = create_violation_store()


@app.after_request
def add_dev_cors_headers(response: Response) -> Response:
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "GET,POST,PATCH,DELETE,OPTIONS"
    return response


def empty_stats() -> dict[str, Any]:
    return {
        "total": 0,
        "avgSpeed": 0,
        "violations": 0,
        "fps": 0,
        "sampleSize": 0,
        "flow": [0] * 24,
        "speedBuckets": {"0-30": 0, "31-50": 0, "51-70": 0, "71-90": 0, ">90": 0},
        "vehicleLog": [],
        "events": [],
        "licensePlateModel": "",
        "licensePlateEnabled": True,
        "licensePlateOcrEnabled": False,
        "visiblePlates": 0,
    }


state.stats = empty_stats()


def default_zone_points(canvas_width: float, canvas_height: float) -> list[dict[str, float]]:
    width = max(float(canvas_width or 960), 320.0)
    height = max(float(canvas_height or 540), 240.0)
    return [
        {"x": width * 0.06, "y": height * 0.42},
        {"x": width * 0.94, "y": height * 0.42},
        {"x": width * 0.96, "y": height * 0.94},
        {"x": width * 0.04, "y": height * 0.94},
    ]


def normalize_zone_points(zone: Any, canvas_width: float, canvas_height: float) -> list[dict[str, float]]:
    if not isinstance(zone, list) or len(zone) != 4:
        return default_zone_points(canvas_width, canvas_height)

    points: list[dict[str, float]] = []
    try:
        for point in zone:
            points.append({"x": float(point["x"]), "y": float(point["y"])})
    except (TypeError, KeyError, ValueError):
        return default_zone_points(canvas_width, canvas_height)

    xs = [point["x"] for point in points]
    ys = [point["y"] for point in points]
    if max(xs) - min(xs) < 50 or max(ys) - min(ys) < 50:
        return default_zone_points(canvas_width, canvas_height)
    return points


@app.get("/")
def index() -> Response:
    return redirect("/web/index.html")


@app.get("/web")
@app.get("/index.html")
def index_alias() -> Response:
    return redirect("/web/index.html")


@app.get("/web/<path:filename>")
def web_asset(filename: str) -> Response:
    return send_from_directory(WEB_DIR, filename)


@app.get("/data/<path:filename>")
def data_asset(filename: str) -> Response:
    return send_from_directory(DATA_DIR, filename)


@app.get("/blockchain/deployments/<path:filename>")
def blockchain_deployment_asset(filename: str) -> Response:
    return send_from_directory(BLOCKCHAIN_DIR / "deployments", filename)


@app.get("/blockchain/artifacts/SpeedViolationEvidence.json")
def blockchain_contract_artifact() -> Response:
    artifact_path = BLOCKCHAIN_DIR / "artifacts" / "contracts" / "SpeedViolationEvidence.sol"
    return send_from_directory(artifact_path, "SpeedViolationEvidence.json")


@app.post("/api/blockchain/deployment")
def save_blockchain_deployment() -> Response:
    payload = request.get_json(force=True)
    address = str(payload.get("address") or "").strip()
    network = str(payload.get("network") or "sepolia").strip().lower()
    chain_id = int(payload.get("chainId") or 0)

    if not re.fullmatch(r"0x[a-fA-F0-9]{40}", address):
        return jsonify({"error": "Invalid contract address"}), 400
    if network != "sepolia" or chain_id != 11155111:
        return jsonify({"error": "Only Sepolia deployment is supported from the web UI"}), 400

    deployments_dir = BLOCKCHAIN_DIR / "deployments"
    deployments_dir.mkdir(parents=True, exist_ok=True)
    address_file = deployments_dir / "contract-address.json"
    abi_file = deployments_dir / "SpeedViolationEvidence.json"
    artifact_file = BLOCKCHAIN_DIR / "artifacts" / "contracts" / "SpeedViolationEvidence.sol" / "SpeedViolationEvidence.json"

    address_file.write_text(
        json.dumps({"address": address, "network": network, "chainId": chain_id}, indent=2),
        encoding="utf-8",
    )

    if artifact_file.exists():
        artifact = json.loads(artifact_file.read_text(encoding="utf-8"))
        abi_file.write_text(json.dumps({"abi": artifact.get("abi", [])}, indent=2), encoding="utf-8")

    return jsonify({"address": address, "network": network, "chainId": chain_id})


@app.post("/api/upload")
def upload_video() -> Response:
    file = request.files.get("video")
    if file is None or not file.filename:
        return jsonify({"error": "Missing video file"}), 400

    DATA_DIR.mkdir(exist_ok=True)
    suffix = Path(file.filename).suffix or ".mp4"
    target = DATA_DIR / f"uploaded_{int(time.time())}{suffix}"
    file.save(target)

    with state_lock:
        state.source_type = "video"
        state.video_path = target
        state.version += 1
        state.stats = empty_stats()

    return jsonify({"ok": True, "filename": target.name})


@app.post("/api/source")
def update_source() -> Response:
    payload = request.get_json(force=True)
    source_type = str(payload.get("sourceType", "video"))

    if source_type not in {"video", "webcam", "camera_url"}:
        return jsonify({"error": "Unsupported source type"}), 400

    if source_type == "webcam":
        camera_index = clamp_int(int(payload.get("cameraIndex", 0)), 0, 16)
        test_cap = cv2.VideoCapture(camera_index, cv2.CAP_DSHOW)
        opened = test_cap.isOpened()
        test_cap.release()
        if not opened:
            return jsonify({"error": f"Cannot open webcam index {camera_index}"}), 400

    with state_lock:
        state.source_type = source_type
        if source_type == "webcam":
            state.camera_index = clamp_int(int(payload.get("cameraIndex", 0)), 0, 16)
        elif source_type == "camera_url":
            state.camera_url = str(payload.get("cameraUrl", "")).strip()
            if not state.camera_url:
                return jsonify({"error": "Camera URL is required"}), 400
        elif source_type == "video":
            state.video_path = ROOT / IN_VIDEO_PATH
        state.version += 1
        state.stats = empty_stats()

    return jsonify({"ok": True, "sourceType": source_type, "version": state.version})


@app.post("/api/reset-default-source")
def reset_default_source() -> Response:
    with state_lock:
        state.source_type = "video"
        state.video_path = ROOT / IN_VIDEO_PATH
        state.camera_url = ""
        state.version += 1
        state.stats = empty_stats()

    return jsonify(
        {
            "ok": True,
            "sourceType": state.source_type,
            "videoPath": str(state.video_path),
            "version": state.version,
        }
    )


@app.post("/api/config")
def update_config() -> Response:
    payload = request.get_json(force=True)
    canvas_width = max(float(payload.get("canvasWidth") or 960), 320.0)
    canvas_height = max(float(payload.get("canvasHeight") or 540), 240.0)
    zone = normalize_zone_points(payload.get("zonePoints", []), canvas_width, canvas_height)

    with state_lock:
        state.zone_canvas = zone
        state.canvas_size = (canvas_width, canvas_height)
        state.confidence = float(payload.get("confidence", state.confidence))
        state.speed_limit = int(payload.get("speedLimit", state.speed_limit))
        state.line_y = int(payload.get("lineY", state.line_y))
        state.real_width = float(payload.get("realWidth", state.real_width))
        state.real_height = float(payload.get("realHeight", state.real_height))
        state.model_name = str(payload.get("modelName", state.model_name))
        state.frame_skip = clamp_int(int(payload.get("frameSkip", state.frame_skip)), 1, 8)
        state.stream_fps = clamp_int(int(payload.get("streamFps", state.stream_fps)), 0, 60)
        state.inference_width = clamp_int(int(payload.get("inferenceWidth", state.inference_width)), 0, 4096)
        state.jpeg_quality = clamp_int(int(payload.get("jpegQuality", state.jpeg_quality)), 45, 95)
        state.license_plate_enabled = bool(payload.get("licensePlateEnabled", state.license_plate_enabled))
        state.license_plate_confidence = float(
            payload.get("licensePlateConfidence", state.license_plate_confidence)
        )
        state.license_plate_ocr_enabled = bool(
            payload.get("licensePlateOcrEnabled", state.license_plate_ocr_enabled)
        )
        state.license_plate_ocr_confidence = float(
            payload.get("licensePlateOcrConfidence", state.license_plate_ocr_confidence)
        )
        state.license_plate_scan_interval = clamp_int(
            int(payload.get("licensePlateScanInterval", state.license_plate_scan_interval)),
            1,
            30,
        )
        state.version += 1
        state.stats = empty_stats()

    return jsonify({"ok": True, "version": state.version})


@app.post("/api/zone")
def update_zone() -> Response:
    payload = request.get_json(force=True)
    zone = payload.get("zonePoints", [])
    canvas_width = max(float(payload.get("canvasWidth") or 960), 320.0)
    canvas_height = max(float(payload.get("canvasHeight") or 540), 240.0)

    if len(zone) not in {0, 4}:
        return jsonify({"error": "The processing zone must be empty or contain exactly 4 points"}), 400

    with state_lock:
        state.zone_canvas = normalize_zone_points(zone, canvas_width, canvas_height) if zone else []
        state.canvas_size = (canvas_width, canvas_height)
        state.line_y = int(payload.get("lineY", state.line_y))

    return jsonify({"ok": True, "version": state.version})


@app.get("/api/auto-zone")
def auto_zone() -> Response:
    canvas_width = float(request.args.get("canvasWidth") or 1)
    canvas_height = float(request.args.get("canvasHeight") or 1)

    snapshot = get_snapshot()

    cap = open_capture(snapshot)
    if not cap.isOpened():
        return jsonify({"error": "Cannot open video source"}), 400

    frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    if snapshot["source_type"] == "video" and frame_count > 20:
        cap.set(cv2.CAP_PROP_POS_FRAMES, min(frame_count // 4, 60))

    ok, frame = cap.read()
    cap.release()
    if not ok:
        return jsonify({"error": "Cannot read frame from video source"}), 400

    polygon, confidence_score, method = estimate_road_polygon(frame)
    frame_height, frame_width = frame.shape[:2]
    scale_x = canvas_width / max(frame_width, 1)
    scale_y = canvas_height / max(frame_height, 1)
    points = [{"x": float(x * scale_x), "y": float(y * scale_y)} for x, y in polygon]

    return jsonify({"points": points, "confidence": confidence_score, "method": method})


@app.get("/api/stats")
def get_stats() -> Response:
    with state_lock:
        return jsonify(state.stats)


@app.get("/api/warmup")
@app.post("/api/warmup")
def warmup() -> Response:
    wait = str(request.args.get("wait", "0")).lower() in {"1", "true", "yes"}
    return jsonify(ensure_model_warmup(wait=wait))


@app.get("/api/license-plates")
def get_license_plate_records() -> Response:
    if not PLATE_TRACKING_JSON.exists():
        return jsonify([])

    try:
        return jsonify(json.loads(PLATE_TRACKING_JSON.read_text(encoding="utf-8")))
    except json.JSONDecodeError:
        return jsonify([])


@app.get("/api/violations")
def list_violations() -> Response:
    return jsonify(violation_store.list_violations())


@app.get("/api/violations/<violation_id>")
def get_violation(violation_id: str) -> Response:
    violation = violation_store.get_violation(violation_id)
    if violation is None:
        return jsonify({"error": "Violation not found"}), 404
    return jsonify(violation)


@app.delete("/api/violations")
def clear_violations() -> Response:
    deleted = violation_store.clear_violations()
    return jsonify(
        {
            "deleted": deleted,
            "note": "Local database records were deleted. Blockchain transactions remain immutable.",
        }
    )


@app.delete("/api/violations/<violation_id>")
def delete_violation(violation_id: str) -> Response:
    deleted = violation_store.delete_violation(violation_id)
    if not deleted:
        return jsonify({"error": "Violation not found"}), 404
    return jsonify(
        {
            "deleted": True,
            "violation_id": violation_id,
            "note": "Local database record was deleted. Blockchain transaction remains immutable.",
        }
    )


@app.post("/api/violations/<violation_id>/generate-hash")
def generate_violation_hash(violation_id: str) -> Response:
    violation = violation_store.regenerate_hash(violation_id)
    if violation is None:
        return jsonify({"error": "Violation not found"}), 404
    return jsonify(violation)


@app.post("/api/violations/<violation_id>/verify-local")
def verify_local_hash(violation_id: str) -> Response:
    result = violation_store.verify_local_hash(violation_id)
    if result is None:
        return jsonify({"error": "Violation not found"}), 404
    return jsonify(result)


@app.patch("/api/violations/<violation_id>/blockchain")
def update_violation_blockchain(violation_id: str) -> Response:
    payload = request.get_json(force=True)
    tx_hash = str(payload.get("blockchain_tx_hash") or payload.get("txHash") or "").strip()
    status = str(payload.get("blockchain_status") or payload.get("status") or "CONFIRMED").strip().upper()
    if not tx_hash:
        return jsonify({"error": "blockchain_tx_hash is required"}), 400
    if status not in BLOCKCHAIN_STATUSES:
        return jsonify({"error": "Unsupported blockchain_status"}), 400

    violation = violation_store.update_blockchain(violation_id, tx_hash, status)
    if violation is None:
        return jsonify({"error": "Violation not found"}), 404
    return jsonify(violation)


@app.get("/stream")
def stream() -> Response:
    return Response(frame_generator(), mimetype="multipart/x-mixed-replace; boundary=frame")


@app.get("/preview")
def preview() -> Response:
    return Response(preview_generator(), mimetype="multipart/x-mixed-replace; boundary=frame")


def frame_generator():
    local_version = None
    cap = None
    pipeline = None
    frame_interval = 1 / 30

    while True:
        snapshot = get_snapshot()
        if local_version != snapshot["version"]:
            try:
                if cap is not None:
                    cap.release()
                cap = open_capture(snapshot)
                snapshot["source_fps"] = float(cap.get(cv2.CAP_PROP_FPS) or 30)
                target_fps = float(snapshot["stream_fps"] or snapshot["source_fps"] or 30)
                frame_interval = 1 / max(1.0, min(target_fps, 120.0))
                pipeline = RealtimePipeline(snapshot)
                local_version = snapshot["version"]
            except Exception as exc:
                app.logger.exception("Failed to initialize realtime pipeline")
                yield encode_placeholder(f"Loi khoi dong pipeline: {str(exc)[:80]}")
                time.sleep(0.8)
                continue

        if cap is None or not cap.isOpened():
            yield encode_placeholder("Khong mo duoc nguon video")
            time.sleep(0.3)
            continue

        ok, frame = cap.read()
        if not ok:
            if snapshot["source_type"] == "video":
                cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            else:
                time.sleep(0.05)
            continue

        for _ in range(max(0, snapshot["frame_skip"] - 1)):
            if not cap.grab():
                if snapshot["source_type"] != "video":
                    break
                cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                break

        if pipeline is None:
            yield encode_placeholder("Pipeline chua san sang")
            time.sleep(0.3)
            continue

        started = time.perf_counter()
        try:
            processed = pipeline.process(frame)
            ok, buffer = cv2.imencode(".jpg", processed, [cv2.IMWRITE_JPEG_QUALITY, snapshot["jpeg_quality"]])
            if not ok:
                continue
        except Exception as exc:
            app.logger.exception("Failed to process realtime frame")
            yield encode_placeholder(f"Loi xu ly frame: {str(exc)[:80]}")
            time.sleep(0.8)
            continue

        yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + buffer.tobytes() + b"\r\n"

        elapsed = time.perf_counter() - started
        delay = max(0, frame_interval - elapsed)
        if delay:
            time.sleep(delay)


def preview_generator():
    local_version = None
    cap = None

    while True:
        snapshot = get_snapshot()
        if local_version != snapshot["version"]:
            if cap is not None:
                cap.release()
            cap = open_capture(snapshot)
            local_version = snapshot["version"]

        if cap is None or not cap.isOpened():
            yield encode_placeholder("Khong mo duoc camera/video")
            time.sleep(0.3)
            continue

        ok, frame = cap.read()
        if not ok:
            if snapshot["source_type"] == "video":
                cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            else:
                time.sleep(0.05)
            continue

        draw_preview_pipeline(frame, snapshot)
        ok, buffer = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, snapshot["jpeg_quality"]])
        if not ok:
            continue

        yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + buffer.tobytes() + b"\r\n"

        if snapshot["stream_fps"] > 0:
            time.sleep(max(0, 1 / snapshot["stream_fps"]))


def get_snapshot() -> dict[str, Any]:
    with state_lock:
        return {
            "source_type": state.source_type,
            "video_path": state.video_path,
            "camera_index": state.camera_index,
            "camera_url": state.camera_url,
            "model_name": state.model_name,
            "confidence": state.confidence,
            "speed_limit": state.speed_limit,
            "line_y": state.line_y,
            "real_width": state.real_width,
            "real_height": state.real_height,
            "frame_skip": state.frame_skip,
            "stream_fps": state.stream_fps,
            "inference_width": state.inference_width,
            "jpeg_quality": state.jpeg_quality,
            "license_plate_enabled": state.license_plate_enabled,
            "license_plate_confidence": state.license_plate_confidence,
            "license_plate_ocr_enabled": state.license_plate_ocr_enabled,
            "license_plate_ocr_confidence": state.license_plate_ocr_confidence,
            "license_plate_scan_interval": state.license_plate_scan_interval,
            "zone_canvas": list(state.zone_canvas),
            "canvas_size": state.canvas_size,
            "version": state.version,
        }


def open_capture(snapshot: dict[str, Any]) -> cv2.VideoCapture:
    if snapshot["source_type"] == "webcam":
        cap = cv2.VideoCapture(int(snapshot["camera_index"]), cv2.CAP_DSHOW)
        configure_realtime_capture(cap)
        return cap
    if snapshot["source_type"] == "camera_url":
        cap = cv2.VideoCapture(str(snapshot["camera_url"]))
        configure_realtime_capture(cap)
        return cap
    cap = cv2.VideoCapture(str(snapshot["video_path"]))
    configure_realtime_capture(cap)
    return cap


def configure_realtime_capture(cap: cv2.VideoCapture) -> None:
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    if hasattr(cv2, "CAP_PROP_HW_ACCELERATION") and hasattr(cv2, "VIDEO_ACCELERATION_ANY"):
        cap.set(cv2.CAP_PROP_HW_ACCELERATION, cv2.VIDEO_ACCELERATION_ANY)


def draw_preview_pipeline(frame: np.ndarray, snapshot: dict[str, Any]) -> None:
    polygon = zone_canvas_to_frame(snapshot, frame)
    if polygon is None:
        return

    overlay = frame.copy()
    cv2.fillPoly(overlay, [polygon], (32, 134, 92))
    cv2.addWeighted(overlay, 0.14, frame, 0.86, 0, frame)
    cv2.polylines(frame, [polygon], True, (32, 134, 92), 3)

    y = int((snapshot["line_y"] / 720) * frame.shape[0])
    mask = np.zeros(frame.shape[:2], dtype=np.uint8)
    cv2.fillPoly(mask, [polygon], 255)
    line_layer = np.zeros_like(frame)
    cv2.line(line_layer, (0, y), (frame.shape[1], y), (0, 204, 255), 3)
    frame[mask > 0] = np.where(line_layer[mask > 0] > 0, line_layer[mask > 0], frame[mask > 0])

    cv2.putText(
        frame,
        "Pipeline speed zone",
        (max(12, int(polygon[:, 0].min())), max(34, int(polygon[:, 1].min()) - 10)),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.7,
        (32, 134, 92),
        2,
    )


def zone_canvas_to_frame(snapshot: dict[str, Any], frame: np.ndarray) -> np.ndarray | None:
    if len(snapshot["zone_canvas"]) != 4:
        return None

    height, width = frame.shape[:2]
    canvas_width, canvas_height = snapshot["canvas_size"]
    scale_x = width / max(canvas_width, 1)
    scale_y = height / max(canvas_height, 1)
    points = [
        [float(point["x"]) * scale_x, float(point["y"]) * scale_y]
        for point in snapshot["zone_canvas"]
    ]
    return np.array(points, dtype=np.int32)


def save_violation_image(violation_id: str, frame: np.ndarray, xyxy: np.ndarray) -> str:
    crop = crop_region(frame, xyxy, padding_ratio=0.16)
    if crop.size == 0:
        return ""

    VIOLATION_IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{violation_id}.jpg"
    target = VIOLATION_IMAGE_DIR / filename
    cv2.imwrite(str(target), crop, [cv2.IMWRITE_JPEG_QUALITY, 92])
    return f"/data/violations/{filename}"


def snapshot_location(cfg: dict[str, Any]) -> str:
    if cfg.get("source_type") == "camera_url":
        return str(cfg.get("camera_url") or "Camera URL")
    if cfg.get("source_type") == "webcam":
        return f"Webcam {cfg.get('camera_index', 0)}"
    return "Configured speed zone"


def snapshot_video_path(cfg: dict[str, Any]) -> str:
    if cfg.get("source_type") == "video":
        return str(cfg.get("video_path") or "")
    if cfg.get("source_type") == "camera_url":
        return str(cfg.get("camera_url") or "")
    return ""


class RealtimePipeline:
    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg
        model_path = MODEL_OPTIONS.get(cfg["model_name"], Path(YOLO_MODEL_PATH))
        self.model = get_model(str(model_path))
        self.license_plate_model_path = find_license_plate_model()
        self.license_plate_model = (
            get_model(str(self.license_plate_model_path))
            if cfg.get("license_plate_enabled") and self.license_plate_model_path is not None
            else None
        )
        self.tracker = sv.ByteTrack()
        self.box_annotator = sv.BoxAnnotator(thickness=2)
        self.trace_annotator = sv.TraceAnnotator(thickness=2, trace_length=25)
        self.speed_estimator: SpeedEstimator | None = None
        self.zone_frame: np.ndarray | None = None
        self.counted_ids: set[int] = set()
        self.recent_speeds: list[int] = []
        self.flow = [0] * 24
        self.events: list[dict[str, Any]] = []
        self.vehicle_log: list[dict[str, Any]] = []
        self.vehicle_log_by_id: dict[int, dict[str, Any]] = {}
        self.plate_records_by_id: dict[int, dict[str, Any]] = {}
        self.plate_cache_by_id: dict[int, dict[str, Any]] = {}
        self.alerted_ids: set[int] = set()
        self.last_tick = time.perf_counter()
        self.last_frame_count = 0
        self.frame_count = 0
        self.fps = 0
        self.current_plate_detections: list[dict[str, Any]] = []

    def process(self, frame: np.ndarray) -> np.ndarray:
        self.frame_count += 1
        self.prepare_geometry(frame)
        annotated = frame.copy()

        if self.zone_frame is None or self.speed_estimator is None:
            cv2.putText(annotated, "Chua co vung 4 diem", (24, 40), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 180, 255), 2)
            return annotated

        infer_frame, infer_scale = resize_for_inference(annotated, int(self.cfg["inference_width"]))
        results = self.model(
            infer_frame,
            conf=self.cfg["confidence"],
            verbose=False,
            **yolo_predict_kwargs(),
        )[0]
        detections = sv.Detections.from_ultralytics(results)
        if infer_scale != 1.0 and len(detections) > 0:
            detections.xyxy[:, [0, 2]] /= infer_scale
            detections.xyxy[:, [1, 3]] /= infer_scale
        detections = self.tracker.update_with_detections(detections)
        detections = self.filter_to_zone(detections)
        detections = self.speed_estimator.update(detections)

        speeds = []
        plate_count = 0
        self.current_plate_detections = []
        vehicle_labels = []
        for tracker_id, class_name, speed, xyxy in zip(
            detections.tracker_id,
            detections.data.get("class_name", []),
            detections.data.get("speed", []),
            detections.xyxy,
        ):
            speed_value = int(speed)
            speeds.append(speed_value)
            label = f"{class_name} #{tracker_id}"
            if speed_value:
                label = f"{class_name} #{tracker_id} {speed_value} km/h"
            plate_info = self.detect_license_plate(int(tracker_id), frame, xyxy)
            if plate_info is not None:
                plate_count += 1
                if not plate_info.get("stale"):
                    self.current_plate_detections.append(plate_info)
                if plate_info.get("text"):
                    label = f"{label} | {plate_info['text']}"
            vehicle_labels.append({"xyxy": xyxy, "text": label, "speed": speed_value})
            self.track_event(int(tracker_id), str(class_name), speed_value, xyxy, frame, plate_info)

        annotated = self.trace_annotator.annotate(annotated, detections)
        annotated = self.box_annotator.annotate(annotated, detections)
        self.draw_license_plates(annotated)
        self.draw_vehicle_labels(annotated, vehicle_labels)
        self.draw_zone(annotated)
        self.update_runtime_stats(speeds, len(detections), plate_count)
        return annotated

    def detect_license_plate(
        self,
        tracker_id: int,
        frame: np.ndarray,
        vehicle_xyxy: np.ndarray,
    ) -> dict[str, Any] | None:
        if self.license_plate_model is None:
            return None

        cached = self.plate_cache_by_id.get(tracker_id)
        scan_interval = max(1, int(self.cfg.get("license_plate_scan_interval", 5)))
        if cached and self.frame_count - int(cached.get("frame", 0)) < scan_interval:
            stale_cached = dict(cached)
            stale_cached["stale"] = True
            return stale_cached

        height, width = frame.shape[:2]
        x1, y1, x2, y2 = [int(value) for value in vehicle_xyxy]
        pad_x = int((x2 - x1) * 0.08)
        pad_y = int((y2 - y1) * 0.08)
        x1 = clamp_int(x1 - pad_x, 0, width - 1)
        y1 = clamp_int(y1 - pad_y, 0, height - 1)
        x2 = clamp_int(x2 + pad_x, x1 + 1, width)
        y2 = clamp_int(y2 + pad_y, y1 + 1, height)
        crop = frame[y1:y2, x1:x2]
        if crop.size == 0:
            return None

        result = self.license_plate_model(
            crop,
            conf=float(self.cfg.get("license_plate_confidence", 0.35)),
            verbose=False,
            **yolo_predict_kwargs(),
        )[0]
        plates = sv.Detections.from_ultralytics(result)
        if len(plates) == 0:
            return cached

        confidences = plates.confidence if plates.confidence is not None else np.ones(len(plates))
        best_index = int(np.argmax(confidences))
        px1, py1, px2, py2 = plates.xyxy[best_index]
        absolute_xyxy = np.array([px1 + x1, py1 + y1, px2 + x1, py2 + y1], dtype=np.float32)
        plate_crop = crop_region(frame, absolute_xyxy, padding_ratio=0.18)
        enhanced_plate_crop = enhance_license_plate_crop(plate_crop)

        plate_text = str(cached.get("text", "")) if cached else ""
        ocr_confidence = float(cached.get("ocrConfidence", 0.0)) if cached else 0.0
        should_read_ocr = (
            self.cfg.get("license_plate_ocr_enabled", True)
            and enhanced_plate_crop.size
            and (not plate_text or self.frame_count - int(cached.get("ocrFrame", 0) if cached else 0) >= scan_interval * 6)
        )
        if should_read_ocr:
            plate_text, ocr_confidence = read_license_plate_text(enhanced_plate_crop)
            if ocr_confidence < float(self.cfg.get("license_plate_ocr_confidence", 0.25)):
                plate_text = ""

        plate_info = {
            "xyxy": absolute_xyxy.tolist(),
            "confidence": float(confidences[best_index]),
            "image": image_to_data_uri(
                enhanced_plate_crop,
                target_width=260,
                jpeg_quality=96,
                allow_upscale=True,
            ),
            "text": plate_text,
            "ocrConfidence": round(ocr_confidence, 3),
            "frame": self.frame_count,
            "ocrFrame": self.frame_count if should_read_ocr else int(cached.get("ocrFrame", 0) if cached else 0),
            "stale": False,
        }
        self.plate_cache_by_id[tracker_id] = plate_info
        return plate_info

    def draw_license_plates(self, frame: np.ndarray) -> None:
        if self.license_plate_model is None:
            return

        for plate in self.current_plate_detections:
            x1, y1, x2, y2 = [int(value) for value in plate["xyxy"]]
            cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 204, 255), 2)
            if plate.get("text"):
                draw_label(
                    frame,
                    str(plate["text"]),
                    x1,
                    y1,
                    bg=(0, 204, 255),
                    fg=(22, 32, 38),
                    scale=0.46,
                )

    def draw_vehicle_labels(self, frame: np.ndarray, labels: list[dict[str, Any]]) -> None:
        for item in labels:
            x1, y1, x2, _ = [int(value) for value in item["xyxy"]]
            is_alert = int(item.get("speed", 0)) > int(self.cfg["speed_limit"])
            draw_label(
                frame,
                str(item["text"]),
                x1,
                y1,
                bg=(42, 103, 199) if is_alert else (32, 134, 92),
                fg=(255, 255, 255),
                scale=0.52,
            )

    def prepare_geometry(self, frame: np.ndarray) -> None:
        if self.zone_frame is not None:
            return

        height, width = frame.shape[:2]
        canvas_width, canvas_height = self.cfg["canvas_size"]
        scale_x = width / max(canvas_width, 1)
        scale_y = height / max(canvas_height, 1)
        points = [[point["x"] * scale_x, point["y"] * scale_y] for point in self.cfg["zone_canvas"]]
        self.zone_frame = np.array(points, dtype=np.float32)

        target = np.array(
            [
                [0, 0],
                [self.cfg["real_width"], 0],
                [self.cfg["real_width"], self.cfg["real_height"]],
                [0, self.cfg["real_height"]],
            ],
            dtype=np.float32,
        )
        source_fps = float(self.cfg.get("source_fps") or 30)
        video_fps = max(1, source_fps / max(1, int(self.cfg["frame_skip"])))
        self.speed_estimator = SpeedEstimator(video_fps, ViewTransformer(self.zone_frame, target))

    def filter_to_zone(self, detections: sv.Detections) -> sv.Detections:
        if len(detections) == 0:
            return detections

        anchors = detections.get_anchors_coordinates(anchor=sv.Position.BOTTOM_CENTER)
        mask = np.array(
            [cv2.pointPolygonTest(self.zone_frame, (float(point[0]), float(point[1])), False) >= 0 for point in anchors],
            dtype=bool,
        )
        return detections[mask]

    def draw_zone(self, frame: np.ndarray) -> None:
        polygon = self.zone_frame.astype(np.int32)
        overlay = frame.copy()
        cv2.fillPoly(overlay, [polygon], (32, 134, 92))
        cv2.addWeighted(overlay, 0.16, frame, 0.84, 0, frame)
        cv2.polylines(frame, [polygon], True, (32, 134, 92), 3)

        y = int((self.cfg["line_y"] / 720) * frame.shape[0])
        mask = np.zeros(frame.shape[:2], dtype=np.uint8)
        cv2.fillPoly(mask, [polygon], 255)
        line_layer = np.zeros_like(frame)
        cv2.line(line_layer, (0, y), (frame.shape[1], y), (0, 204, 255), 3)
        frame[mask > 0] = np.where(line_layer[mask > 0] > 0, line_layer[mask > 0], frame[mask > 0])

    def track_event(
        self,
        tracker_id: int,
        class_name: str,
        speed: int,
        xyxy: np.ndarray,
        frame: np.ndarray,
        plate_info: dict[str, Any] | None,
    ) -> None:
        if tracker_id not in self.counted_ids:
            self.counted_ids.add(tracker_id)

        if tracker_id not in self.vehicle_log_by_id:
            item = {
                "time": time.strftime("%H:%M:%S"),
                "id": f"#{tracker_id}",
                "type": class_name,
                "speed": speed,
                "status": "Dang theo doi",
                "image": crop_vehicle_thumbnail(frame, xyxy),
                "plateStatus": "Chua phat hien",
                "plateImage": "",
                "plateConfidence": 0,
                "plateText": "",
                "plateOcrConfidence": 0,
            }
            self.vehicle_log_by_id[tracker_id] = item
            self.vehicle_log.insert(0, item)

        item = self.vehicle_log_by_id[tracker_id]
        if plate_info is not None:
            item["plate"] = plate_info
            item["plateText"] = plate_info.get("text", "")
            item["plateOcrConfidence"] = plate_info.get("ocrConfidence", 0)
            item["plateStatus"] = f"Da doc: {item['plateText']}" if item["plateText"] else "Da phat hien, chua doc ro"
            item["plateImage"] = plate_info["image"]
            item["plateConfidence"] = round(plate_info["confidence"], 2)
            self.persist_plate_record(tracker_id, class_name, speed, item)

        if speed > 0:
            item["speed"] = speed
            item["status"] = "Vuot nguong" if speed > self.cfg["speed_limit"] else "Hop le"
            self.recent_speeds.append(speed)
            self.recent_speeds = self.recent_speeds[-160:]

        if speed > self.cfg["speed_limit"] and tracker_id not in self.alerted_ids:
            self.alerted_ids.add(tracker_id)
            violation_id = f"VIO-{int(time.time() * 1000)}-{tracker_id}"
            captured_at = time.strftime("%Y-%m-%d %H:%M:%S")
            image_path = save_violation_image(violation_id, frame, xyxy)
            violation_record = {
                "violation_id": violation_id,
                "vehicle_id": f"#{tracker_id}",
                "vehicle_type": class_name,
                "speed": speed,
                "speed_limit": self.cfg["speed_limit"],
                "violation_type": "SPEEDING",
                "time": captured_at,
                "location": snapshot_location(self.cfg),
                "image_path": image_path,
                "video_path": snapshot_video_path(self.cfg),
                "confidence": 0,
                "blockchain_tx_hash": "",
                "blockchain_status": "NOT_SUBMITTED",
            }
            violation_record["evidence_hash"] = generate_evidence_hash(violation_record)
            stored_violation = violation_store.create_violation(violation_record)
            self.events.insert(
                0,
                {
                    "time": time.strftime("%H:%M:%S"),
                    "violation_id": stored_violation["violation_id"],
                    "id": f"#{tracker_id}",
                    "type": class_name,
                    "speed": speed,
                    "speed_limit": self.cfg["speed_limit"],
                    "status": "Vuot nguong",
                    "image": stored_violation.get("image_path") or item["image"],
                    "evidence_hash": stored_violation.get("evidence_hash", ""),
                    "blockchain_status": stored_violation.get("blockchain_status", "NOT_SUBMITTED"),
                    "blockchain_tx_hash": stored_violation.get("blockchain_tx_hash", ""),
                    "plateStatus": item.get("plateStatus", "Chua phat hien"),
                    "plateImage": item.get("plateImage", ""),
                    "plateConfidence": item.get("plateConfidence", 0),
                    "plateText": item.get("plateText", ""),
                    "plateOcrConfidence": item.get("plateOcrConfidence", 0),
                },
            )

        self.vehicle_log = self.vehicle_log[:100]
        self.events = self.events[:30]

    def persist_plate_record(self, tracker_id: int, class_name: str, speed: int, item: dict[str, Any]) -> None:
        current_text = str(item.get("plateText", ""))
        previous = self.plate_records_by_id.get(tracker_id)
        current_confidence = item.get("plateConfidence", 0)
        if (
            previous
            and previous.get("plateText") == current_text
            and previous.get("plateDetectionConfidence") == current_confidence
        ):
            return

        record = {
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "trackerId": tracker_id,
            "vehicleId": f"#{tracker_id}",
            "type": class_name,
            "speed": speed,
            "status": item.get("status", "Dang theo doi"),
            "plateText": current_text or "UNREAD",
            "plateDetectionConfidence": current_confidence,
            "plateOcrConfidence": item.get("plateOcrConfidence", 0),
        }
        self.plate_records_by_id[tracker_id] = record
        append_plate_tracking_record(record)

    def update_runtime_stats(self, speeds: list[int], visible_count: int, plate_count: int) -> None:
        now = time.perf_counter()
        if now - self.last_tick >= 1:
            self.fps = int((self.frame_count - self.last_frame_count) / (now - self.last_tick))
            self.last_tick = now
            self.last_frame_count = self.frame_count
            self.flow.append(visible_count)
            self.flow = self.flow[-24:]

        buckets = {"0-30": 0, "31-50": 0, "51-70": 0, "71-90": 0, ">90": 0}
        for speed in self.recent_speeds:
            if speed <= 30:
                buckets["0-30"] += 1
            elif speed <= 50:
                buckets["31-50"] += 1
            elif speed <= 70:
                buckets["51-70"] += 1
            elif speed <= 90:
                buckets["71-90"] += 1
            else:
                buckets[">90"] += 1

        stats = {
            "total": len(self.counted_ids),
            "avgSpeed": int(np.mean(self.recent_speeds)) if self.recent_speeds else 0,
            "violations": sum(1 for speed in self.recent_speeds if speed > self.cfg["speed_limit"]),
            "fps": self.fps,
            "sampleSize": len(self.recent_speeds),
            "flow": self.flow,
            "speedBuckets": buckets,
            "vehicleLog": self.vehicle_log,
            "events": self.events,
            "licensePlateModel": str(self.license_plate_model_path) if self.license_plate_model_path else "",
            "licensePlateEnabled": self.license_plate_model is not None,
            "licensePlateOcrEnabled": bool(self.cfg.get("license_plate_ocr_enabled")) and not ocr_load_failed,
            "visiblePlates": plate_count,
        }
        with state_lock:
            state.stats = stats


def get_model(model_path: str) -> YOLO:
    with model_cache_lock:
        cached = model_cache.get(model_path)
        if cached is not None:
            return cached

        model = YOLO(model_path)
        if should_use_cuda():
            model.to("cuda")
            try:
                model.fuse()
            except Exception:
                pass
        model_cache[model_path] = model
        return model


def has_cuda() -> bool:
    global cuda_available
    if cuda_available is not None:
        return cuda_available

    try:
        import torch

        cuda_available = bool(torch.cuda.is_available())
        if cuda_available:
            torch.backends.cudnn.benchmark = True
            torch.set_float32_matmul_precision("high")
    except Exception:
        cuda_available = False
    return cuda_available


def should_use_cuda() -> bool:
    return os.getenv("USE_CUDA", "0").strip().lower() in {"1", "true", "yes"} and has_cuda()


def yolo_predict_kwargs() -> dict[str, Any]:
    if not should_use_cuda():
        return {}
    return {"device": 0, "half": True}


def ensure_model_warmup(wait: bool = False) -> dict[str, Any]:
    global warmup_started, warmup_thread

    with warmup_lock:
        if not warmup_started:
            warmup_started = True
            warmup_thread = Thread(target=warmup_models, name="yolo-warmup", daemon=True)
            warmup_thread.start()
        thread = warmup_thread

    if wait and thread is not None:
        thread.join(timeout=60)

    return {"started": warmup_started, "done": warmup_done, "error": warmup_error}


def warmup_models() -> None:
    global warmup_done, warmup_error

    try:
        snapshot = get_snapshot()
        model_path = MODEL_OPTIONS.get(snapshot["model_name"], Path(YOLO_MODEL_PATH))
        model = get_model(str(model_path))
        dummy = np.zeros((640, 640, 3), dtype=np.uint8)
        model(dummy, conf=0.25, verbose=False, **yolo_predict_kwargs())

        plate_path = find_license_plate_model()
        if plate_path is not None and snapshot.get("license_plate_enabled", True):
            plate_model = get_model(str(plate_path))
            plate_dummy = np.zeros((160, 320, 3), dtype=np.uint8)
            plate_model(plate_dummy, conf=0.25, verbose=False, **yolo_predict_kwargs())
    except Exception as exc:
        warmup_error = str(exc)
    finally:
        warmup_done = True


def find_license_plate_model() -> Path | None:
    for candidate in LICENSE_PLATE_MODEL_CANDIDATES:
        if candidate.exists():
            return candidate

    matches = []
    search_roots = [ROOT / "runs", ROOT.parent / "runs", ROOT.parent.parent]
    for runs_dir in search_roots:
        if not runs_dir.exists():
            continue
        for path in runs_dir.rglob("best.pt"):
            text = str(path).lower()
            if "license" in text or "plate" in text:
                matches.append(path)

    if not matches:
        return None
    return max(matches, key=lambda item: item.stat().st_mtime)


def get_ocr_reader() -> Any | None:
    global ocr_reader, ocr_load_failed
    if ocr_reader is not None:
        return ocr_reader
    if ocr_load_failed:
        return None

    try:
        import easyocr

        ocr_reader = easyocr.Reader(["en"], gpu=has_cuda(), verbose=False)
        return ocr_reader
    except Exception:
        ocr_load_failed = True
        return None


def read_license_plate_text(image: np.ndarray) -> tuple[str, float]:
    reader = get_ocr_reader()
    if reader is None or image.size == 0:
        return "", 0.0

    candidates = []
    for prepared in prepare_plate_ocr_variants(image):
        try:
            results = reader.readtext(
                prepared,
                detail=1,
                paragraph=False,
                allowlist="ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-. ",
                decoder="beamsearch",
            )
        except Exception:
            continue

        for result in results:
            if len(result) < 3:
                continue
            text = normalize_plate_text(str(result[1]))
            confidence = float(result[2])
            if text and is_plausible_plate_text(text, confidence):
                candidates.append((text, confidence))

    if not candidates:
        return "", 0.0
    return max(candidates, key=lambda item: (item[1], len(item[0])))


def prepare_plate_ocr_variants(image: np.ndarray) -> list[np.ndarray]:
    if image.ndim == 3:
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    else:
        gray = image.copy()

    scale = max(3, int(520 / max(gray.shape[1], 1)))
    if scale > 1:
        gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    gray = cv2.bilateralFilter(gray, 7, 45, 45)
    equalized = cv2.equalizeHist(gray)
    adaptive = cv2.adaptiveThreshold(
        equalized,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        31,
        7,
    )
    inverted = cv2.bitwise_not(adaptive)
    sharpen_kernel = np.array([[0, -1, 0], [-1, 5, -1], [0, -1, 0]])
    sharpened = cv2.filter2D(equalized, -1, sharpen_kernel)
    return [gray, equalized, sharpened, adaptive, inverted]


def normalize_plate_text(text: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]", "", text).upper()
    replacements = str.maketrans({"O": "0", "I": "1", "L": "1"})
    if len(cleaned) >= 5 and sum(char.isdigit() for char in cleaned) >= 3:
        cleaned = cleaned.translate(replacements)
    if len(cleaned) < 5:
        return ""
    return cleaned[:12]


def is_plausible_plate_text(text: str, confidence: float) -> bool:
    if confidence < 0.55:
        return False

    if not 5 <= len(text) <= 10:
        return False

    digit_count = sum(char.isdigit() for char in text)
    letter_count = sum(char.isalpha() for char in text)
    if digit_count < 3:
        return False

    if letter_count == 0 and len(text) < 7:
        return False

    if re.search(r"(.)\1{3,}", text):
        return False

    if re.fullmatch(r"\d{2}[A-Z]{1,2}\d{4,5}", text):
        return True

    return digit_count >= 4 and letter_count <= 4


def append_plate_tracking_record(record: dict[str, Any]) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    csv_exists = PLATE_TRACKING_CSV.exists()
    fields = [
        "time",
        "trackerId",
        "vehicleId",
        "type",
        "speed",
        "status",
        "plateText",
        "plateDetectionConfidence",
        "plateOcrConfidence",
    ]

    with PLATE_TRACKING_CSV.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        if not csv_exists:
            writer.writeheader()
        writer.writerow({field: record.get(field, "") for field in fields})

    records = []
    if PLATE_TRACKING_JSON.exists():
        try:
            records = json.loads(PLATE_TRACKING_JSON.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            records = []
    records.append(record)
    PLATE_TRACKING_JSON.write_text(json.dumps(records[-1000:], ensure_ascii=False, indent=2), encoding="utf-8")


def estimate_road_polygon(frame: np.ndarray) -> tuple[list[tuple[int, int]], float, str]:
    height, width = frame.shape[:2]
    fallback = [
        (int(width * 0.34), int(height * 0.42)),
        (int(width * 0.66), int(height * 0.42)),
        (int(width * 0.94), int(height * 0.94)),
        (int(width * 0.06), int(height * 0.94)),
    ]

    hls = cv2.cvtColor(frame, cv2.COLOR_BGR2HLS)
    lightness = hls[:, :, 1]
    saturation = hls[:, :, 2]
    lower_half = np.zeros((height, width), dtype=np.uint8)
    lower_half[int(height * 0.32) :, :] = 255

    road_mask = cv2.inRange(saturation, 0, 85)
    light_mask = cv2.inRange(lightness, 35, 220)
    mask = cv2.bitwise_and(road_mask, light_mask)
    mask = cv2.bitwise_and(mask, lower_half)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (17, 17))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=2)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel, iterations=1)

    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return fallback, 0.35, "fallback-trapezoid"

    largest = max(contours, key=cv2.contourArea)
    area_ratio = cv2.contourArea(largest) / float(width * height)
    if area_ratio < 0.08:
        return fallback, round(float(area_ratio), 2), "fallback-trapezoid"

    filled = np.zeros_like(mask)
    cv2.drawContours(filled, [largest], -1, 255, -1)
    y_top = int(height * 0.42)
    y_bottom = int(height * 0.94)
    top_range = scan_mask_x_range(filled, y_top, int(height * 0.035))
    bottom_range = scan_mask_x_range(filled, y_bottom, int(height * 0.035))

    if top_range is None or bottom_range is None:
        return fallback, round(float(area_ratio), 2), "fallback-trapezoid"

    top_left, top_right = top_range
    bottom_left, bottom_right = bottom_range
    min_width = width * 0.12
    if top_right - top_left < min_width or bottom_right - bottom_left < min_width:
        return fallback, round(float(area_ratio), 2), "fallback-trapezoid"

    polygon = [
        (clamp_int(top_left, 0, width - 1), y_top),
        (clamp_int(top_right, 0, width - 1), y_top),
        (clamp_int(bottom_right, 0, width - 1), y_bottom),
        (clamp_int(bottom_left, 0, width - 1), y_bottom),
    ]
    return polygon, round(min(0.95, 0.45 + area_ratio), 2), "road-surface-mask"


def scan_mask_x_range(mask: np.ndarray, y: int, band: int) -> tuple[int, int] | None:
    height, _ = mask.shape
    y1 = max(0, y - band)
    y2 = min(height, y + band + 1)
    rows = mask[y1:y2, :]
    columns = np.where(np.any(rows > 0, axis=0))[0]
    if columns.size == 0:
        return None
    return int(columns[0]), int(columns[-1])


def clamp_int(value: int, min_value: int, max_value: int) -> int:
    return max(min_value, min(max_value, int(value)))


def draw_label(
    frame: np.ndarray,
    text: str,
    x: int,
    y: int,
    bg: tuple[int, int, int],
    fg: tuple[int, int, int],
    scale: float = 0.52,
) -> None:
    if not text:
        return

    height, width = frame.shape[:2]
    font = cv2.FONT_HERSHEY_SIMPLEX
    thickness = 1
    padding_x = 7
    padding_y = 5
    max_chars = 28
    display_text = text if len(text) <= max_chars else f"{text[: max_chars - 1]}..."
    (text_width, text_height), baseline = cv2.getTextSize(display_text, font, scale, thickness)
    box_width = text_width + padding_x * 2
    box_height = text_height + baseline + padding_y * 2

    left = clamp_int(x, 0, max(0, width - box_width - 1))
    top = y - box_height - 4
    if top < 2:
        top = clamp_int(y + 4, 2, max(2, height - box_height - 1))

    right = min(width - 1, left + box_width)
    bottom = min(height - 1, top + box_height)
    cv2.rectangle(frame, (left, top), (right, bottom), bg, -1)
    cv2.rectangle(frame, (left, top), (right, bottom), bg, 1)
    cv2.putText(
        frame,
        display_text,
        (left + padding_x, bottom - padding_y - baseline),
        font,
        scale,
        fg,
        thickness,
        cv2.LINE_AA,
    )


def crop_vehicle_thumbnail(frame: np.ndarray, xyxy: np.ndarray) -> str:
    return crop_region_thumbnail(frame, xyxy, target_width=140, padding_ratio=0.12)


def crop_region(frame: np.ndarray, xyxy: np.ndarray, padding_ratio: float = 0.12) -> np.ndarray:
    height, width = frame.shape[:2]
    x1, y1, x2, y2 = [int(value) for value in xyxy]
    pad_x = int((x2 - x1) * padding_ratio)
    pad_y = int((y2 - y1) * padding_ratio)
    x1 = clamp_int(x1 - pad_x, 0, width - 1)
    y1 = clamp_int(y1 - pad_y, 0, height - 1)
    x2 = clamp_int(x2 + pad_x, x1 + 1, width)
    y2 = clamp_int(y2 + pad_y, y1 + 1, height)
    return frame[y1:y2, x1:x2]


def crop_region_thumbnail(
    frame: np.ndarray,
    xyxy: np.ndarray,
    target_width: int = 140,
    padding_ratio: float = 0.12,
    jpeg_quality: int = 78,
    allow_upscale: bool = False,
) -> str:
    crop = crop_region(frame, xyxy, padding_ratio=padding_ratio)
    if crop.size == 0:
        return ""

    return image_to_data_uri(crop, target_width, jpeg_quality, allow_upscale)


def image_to_data_uri(
    image: np.ndarray,
    target_width: int = 140,
    jpeg_quality: int = 78,
    allow_upscale: bool = False,
) -> str:
    if image.size == 0:
        return ""

    thumb = resize_to_width(image, target_width, allow_upscale=allow_upscale)
    ok, buffer = cv2.imencode(".jpg", thumb, [cv2.IMWRITE_JPEG_QUALITY, jpeg_quality])
    if not ok:
        return ""
    encoded = base64.b64encode(buffer).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


def enhance_license_plate_crop(image: np.ndarray) -> np.ndarray:
    if image.size == 0:
        return image

    target_width = 420
    enhanced = resize_to_width(image, target_width, allow_upscale=True)
    if enhanced.ndim == 2:
        enhanced = cv2.cvtColor(enhanced, cv2.COLOR_GRAY2BGR)

    lab = cv2.cvtColor(enhanced, cv2.COLOR_BGR2LAB)
    lightness, a_channel, b_channel = cv2.split(lab)
    clahe = cv2.createCLAHE(clipLimit=2.2, tileGridSize=(8, 8))
    lightness = clahe.apply(lightness)
    enhanced = cv2.cvtColor(cv2.merge((lightness, a_channel, b_channel)), cv2.COLOR_LAB2BGR)
    enhanced = cv2.bilateralFilter(enhanced, 5, 35, 35)
    blur = cv2.GaussianBlur(enhanced, (0, 0), 1.1)
    enhanced = cv2.addWeighted(enhanced, 1.65, blur, -0.65, 0)
    return enhanced


def resize_to_width(image: np.ndarray, target_width: int, allow_upscale: bool = False) -> np.ndarray:
    height, width = image.shape[:2]
    if width <= target_width and not allow_upscale:
        return image
    ratio = target_width / width
    interpolation = cv2.INTER_CUBIC if ratio > 1 else cv2.INTER_AREA
    return cv2.resize(image, (target_width, max(1, int(height * ratio))), interpolation=interpolation)


def resize_for_inference(frame: np.ndarray, target_width: int) -> tuple[np.ndarray, float]:
    if target_width <= 0:
        return frame, 1.0

    height, width = frame.shape[:2]
    if width <= target_width:
        return frame, 1.0

    scale = target_width / width
    resized = cv2.resize(
        frame,
        (target_width, max(1, int(height * scale))),
        interpolation=cv2.INTER_AREA,
    )
    return resized, scale


def encode_placeholder(message: str) -> bytes:
    frame = np.zeros((540, 960, 3), dtype=np.uint8)
    cv2.putText(frame, message, (40, 270), cv2.FONT_HERSHEY_SIMPLEX, 1.1, (255, 255, 255), 2)
    ok, buffer = cv2.imencode(".jpg", frame)
    if not ok:
        return b""
    return b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + buffer.tobytes() + b"\r\n"


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5050, threaded=True)
