"""Isolated RapidOCR backend. JSON lines on stdin/stdout; diagnostics on stderr."""
import argparse
import base64
import contextlib
import json
from pathlib import Path
import sys
import time
import uuid


class Backend:
    def __init__(self, device="dml", profile_dir=None):
        import onnxruntime as ort
        from rapidocr import RapidOCR
        from rapidocr.inference_engine.onnxruntime.main import OrtInferSession

        if device == "dml" and "DmlExecutionProvider" not in ort.get_available_providers():
            raise RuntimeError("DirectML unavailable; install onnxruntime-directml in the isolated environment")
        original = OrtInferSession._init_sess_opts

        def options(cfg):
            opts = original(cfg)
            opts.enable_mem_pattern = False
            opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
            if profile_dir:
                opts.enable_profiling = True
                opts.profile_file_prefix = str(Path(profile_dir) / ("ort_" + uuid.uuid4().hex))
            return opts

        # The pinned RapidOCR release has no public session-options configuration.
        OrtInferSession._init_sess_opts = staticmethod(options)
        self.device = device
        self.engine = RapidOCR(params={
            "EngineConfig.onnxruntime.use_dml": device == "dml",
            "EngineConfig.onnxruntime.intra_op_num_threads": 4,
            "EngineConfig.onnxruntime.inter_op_num_threads": 1,
            "Global.log_level": "warning",
            "Det.limit_type": "max",
            "Det.limit_side_len": 960,
        })
        # Load eagerly so provider failures are reported before processing requests.
        try:
            self.sessions = {name: getattr(self.engine, f"_load_{name}_model")().session.session
                             for name in ("det", "cls", "rec")}
        finally:
            OrtInferSession._init_sess_opts = staticmethod(original)
        if device == "dml":
            for name, session in self.sessions.items():
                if "DmlExecutionProvider" not in session.get_providers():
                    raise RuntimeError(f"{name} silently fell back to CPU")
        for session in self.sessions.values():
            session.disable_fallback()

    def info(self):
        return {"device": self.device, "providers": {
            name: session.get_providers() for name, session in self.sessions.items()}}

    def recognize(self, image):
        start = time.perf_counter()
        output = self.engine(image)
        lines = []
        if output.boxes is not None and output.txts is not None:
            for box, text, score in zip(output.boxes, output.txts, output.scores):
                points = box.tolist()
                xs, ys = zip(*points)
                lines.append({"text": text, "confidence": float(score), "box": points,
                              "x": min(xs), "y": min(ys),
                              "width": max(xs) - min(xs), "height": max(ys) - min(ys)})
        return {"lines": lines, "elapsed_ms": (time.perf_counter() - start) * 1000}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", choices=("dml", "cpu"), default="dml")
    args = parser.parse_args()
    with contextlib.redirect_stdout(sys.stderr):
        backend = Backend(args.device)
    print(json.dumps({"ready": True, **backend.info()}), flush=True)
    input_stream = sys.stdin.buffer
    while line := input_stream.readline():
        request = {}
        try:
            request = json.loads(line)
            if "image_bytes" in request:
                length = request["image_bytes"]
                if type(length) is not int or not 0 < length <= 32 * 1024 * 1024:
                    # Cannot resynchronize an invalid binary payload safely.
                    raise SystemExit("Invalid binary image length")
                image = input_stream.read(length)
                if len(image) != length:
                    raise SystemExit("Truncated binary image")
            else:
                image = base64.b64decode(request["image_base64"], validate=True)
            if len(image) > 32 * 1024 * 1024:
                raise ValueError("Image exceeds 32 MiB")
            with contextlib.redirect_stdout(sys.stderr):
                result = backend.recognize(image)
            response = {"id": request.get("id"), **result}
        except Exception as exc:
            response = {"id": request.get("id") if isinstance(request, dict) else None,
                        "error": str(exc)}
        print(json.dumps(response, ensure_ascii=True), flush=True)


if __name__ == "__main__":
    main()
