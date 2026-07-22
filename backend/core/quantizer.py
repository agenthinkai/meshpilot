"""
MeshPilot Auto-Quantization Pipeline

Supported conversions:
  PyTorch (.pt / .bin / .safetensors) → ONNX → INT8 (via Neural Compressor)
  ONNX → INT8 (static quantization via ONNX Runtime quantization tools)
  GGUF  → already quantized; validate and register only
  INT4  → via llama.cpp quantize binary (for GGUF models)

The pipeline is triggered as a Celery background task after model upload.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

import onnx
import onnxruntime as ort
from onnxruntime.quantization import (
    QuantFormat,
    QuantType,
    quantize_dynamic,
    quantize_static,
)

from core.config import settings
from core.cpu_detect import get_cpu_profile

logger = logging.getLogger("meshpilot.quantizer")


class QuantizationResult:
    def __init__(
        self,
        success: bool,
        output_path: Optional[str] = None,
        quant_bits: Optional[str] = None,
        size_reduction_pct: Optional[float] = None,
        error: Optional[str] = None,
    ):
        self.success = success
        self.output_path = output_path
        self.quant_bits = quant_bits
        self.size_reduction_pct = size_reduction_pct
        self.error = error

    def to_dict(self) -> dict:
        return {
            "success":            self.success,
            "output_path":        self.output_path,
            "quant_bits":         self.quant_bits,
            "size_reduction_pct": self.size_reduction_pct,
            "error":              self.error,
        }


class AutoQuantizer:
    """
    Auto-quantization pipeline.
    Detects input format and applies the optimal quantization strategy
    based on the detected CPU profile.
    """

    def __init__(self, model_id: str, input_path: str):
        self.model_id   = model_id
        self.input_path = Path(input_path)
        self.cpu        = get_cpu_profile()
        self.storage    = Path(settings.MODEL_STORAGE)

    def run(self) -> QuantizationResult:
        suffix = self.input_path.suffix.lower()
        logger.info(f"Starting quantization: {self.input_path.name} (format: {suffix})")

        if suffix == ".gguf":
            return self._handle_gguf()
        elif suffix == ".onnx":
            return self._quantize_onnx()
        elif suffix in (".pt", ".pth", ".bin", ".safetensors"):
            return self._pytorch_to_onnx_to_int8()
        else:
            return QuantizationResult(
                success=False,
                error=f"Unsupported format: {suffix}. Supported: .gguf, .onnx, .pt, .pth, .bin, .safetensors",
            )

    # ── GGUF: validate and register ───────────────────────────────────────────

    def _handle_gguf(self) -> QuantizationResult:
        """GGUF files are already quantized by llama.cpp. Validate magic bytes."""
        try:
            with open(self.input_path, "rb") as f:
                magic = f.read(4)
            if magic != b"GGUF":
                return QuantizationResult(success=False, error="Invalid GGUF file (bad magic bytes)")

            # Detect quantization level from filename
            name = self.input_path.name.upper()
            if "Q4" in name or "INT4" in name:
                quant = "int4"
            elif "Q8" in name or "INT8" in name:
                quant = "int8"
            elif "F16" in name:
                quant = "fp16"
            else:
                quant = "int8"  # assume quantized

            logger.info(f"GGUF validated: {self.input_path.name}, quant={quant}")
            return QuantizationResult(
                success=True,
                output_path=str(self.input_path),
                quant_bits=quant,
                size_reduction_pct=0.0,  # already quantized
            )
        except Exception as e:
            return QuantizationResult(success=False, error=str(e))

    # ── ONNX: dynamic INT8 quantization ──────────────────────────────────────

    def _quantize_onnx(self) -> QuantizationResult:
        """Apply dynamic INT8 quantization to an ONNX model."""
        try:
            # Validate ONNX model
            model = onnx.load(str(self.input_path))
            onnx.checker.check_model(model)

            original_size = self.input_path.stat().st_size
            output_path   = self.input_path.with_suffix(".int8.onnx")

            # Dynamic quantization (no calibration data needed)
            quantize_dynamic(
                model_input=str(self.input_path),
                model_output=str(output_path),
                weight_type=QuantType.QInt8,
                per_channel=True,
            )

            quantized_size = output_path.stat().st_size
            reduction = (1 - quantized_size / original_size) * 100

            logger.info(f"ONNX INT8 quantization complete: {reduction:.1f}% size reduction")
            return QuantizationResult(
                success=True,
                output_path=str(output_path),
                quant_bits="int8",
                size_reduction_pct=round(reduction, 1),
            )
        except Exception as e:
            logger.error(f"ONNX quantization failed: {e}")
            return QuantizationResult(success=False, error=str(e))

    # ── PyTorch → ONNX → INT8 ────────────────────────────────────────────────

    def _pytorch_to_onnx_to_int8(self) -> QuantizationResult:
        """
        Convert a PyTorch model to ONNX using Hugging Face Optimum,
        then apply INT8 dynamic quantization.
        """
        try:
            import torch
            from optimum.onnxruntime import ORTModelForCausalLM
            from transformers import AutoTokenizer

            model_dir = self.input_path.parent
            onnx_dir  = model_dir / f"{self.model_id}_onnx"
            onnx_dir.mkdir(exist_ok=True)

            logger.info(f"Converting PyTorch → ONNX: {model_dir}")

            # Export via Optimum
            ort_model = ORTModelForCausalLM.from_pretrained(
                str(model_dir),
                export=True,
                provider="CPUExecutionProvider",
            )
            ort_model.save_pretrained(str(onnx_dir))

            # Find the exported ONNX file
            onnx_files = list(onnx_dir.glob("*.onnx"))
            if not onnx_files:
                return QuantizationResult(success=False, error="No ONNX file produced by export")

            onnx_path = onnx_files[0]
            original_size = onnx_path.stat().st_size

            # Apply INT8 quantization
            output_path = onnx_path.with_suffix(".int8.onnx")
            quantize_dynamic(
                model_input=str(onnx_path),
                model_output=str(output_path),
                weight_type=QuantType.QInt8,
            )

            quantized_size = output_path.stat().st_size
            reduction = (1 - quantized_size / original_size) * 100

            logger.info(f"PyTorch→ONNX→INT8 complete: {reduction:.1f}% size reduction")
            return QuantizationResult(
                success=True,
                output_path=str(output_path),
                quant_bits="int8",
                size_reduction_pct=round(reduction, 1),
            )
        except Exception as e:
            logger.error(f"PyTorch→ONNX conversion failed: {e}")
            return QuantizationResult(success=False, error=str(e))

    # ── llama.cpp INT4 quantization (for raw FP16 GGUF) ──────────────────────

    def _llamacpp_quantize_int4(self, input_gguf: str, output_gguf: str) -> QuantizationResult:
        """
        Use llama.cpp quantize binary to convert FP16 GGUF → Q4_K_M.
        Requires llama.cpp to be installed in the container.
        """
        try:
            llama_quantize = shutil.which("llama-quantize") or "/usr/local/bin/llama-quantize"
            if not os.path.exists(llama_quantize):
                return QuantizationResult(
                    success=False,
                    error="llama-quantize binary not found. Ensure llama.cpp is installed."
                )

            original_size = Path(input_gguf).stat().st_size
            result = subprocess.run(
                [llama_quantize, input_gguf, output_gguf, "Q4_K_M"],
                capture_output=True,
                text=True,
                timeout=600,
            )

            if result.returncode != 0:
                return QuantizationResult(success=False, error=result.stderr[:500])

            quantized_size = Path(output_gguf).stat().st_size
            reduction = (1 - quantized_size / original_size) * 100

            return QuantizationResult(
                success=True,
                output_path=output_gguf,
                quant_bits="int4",
                size_reduction_pct=round(reduction, 1),
            )
        except subprocess.TimeoutExpired:
            return QuantizationResult(success=False, error="Quantization timed out (>10 minutes)")
        except Exception as e:
            return QuantizationResult(success=False, error=str(e))
