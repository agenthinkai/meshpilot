"""
MeshPilot CPU Detection — auto-detect instruction set extensions
and select the optimal inference backend and quantization strategy.

Detected features:
  x86:  AMX, AVX-512, AVX2, SSE4.2
  ARM:  SVE2, NEON, DOTPROD
  RISC-V: RVV (Vector Extension)

Backend selection priority:
  1. llama.cpp  — GGUF models, best for LLMs, uses BLAS + AVX/NEON
  2. ONNX Runtime + OpenVINO EP — ONNX models, best for encoder/classifier
  3. ONNX Runtime (CPU EP) — fallback for any ONNX model
"""

from __future__ import annotations

import platform
import subprocess
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import cpuinfo
import psutil


@dataclass
class CPUProfile:
    vendor:         str
    brand:          str
    arch:           str          # "x86_64" | "aarch64" | "riscv64"
    physical_cores: int
    logical_cores:  int
    base_freq_mhz:  Optional[float]
    l3_cache_mb:    Optional[float]
    ram_gb:         float

    # Instruction set extensions
    has_avx512:     bool = False
    has_avx2:       bool = False
    has_amx:        bool = False   # Intel Advanced Matrix Extensions (Sapphire Rapids+)
    has_sse42:      bool = False
    has_neon:       bool = False   # ARM NEON / AdvSIMD
    has_sve2:       bool = False   # ARM Scalable Vector Extension 2
    has_dotprod:    bool = False   # ARM DOT product
    has_rvv:        bool = False   # RISC-V Vector Extension
    has_openvino:   bool = False   # OpenVINO EP available

    # Derived recommendations
    recommended_backend:  str = "llamacpp"
    recommended_quant:    str = "int8"
    optimal_threads:      int = 4
    notes:                List[str] = field(default_factory=list)

    def to_dict(self) -> Dict:
        return {
            "vendor":              self.vendor,
            "brand":               self.brand,
            "arch":                self.arch,
            "physical_cores":      self.physical_cores,
            "logical_cores":       self.logical_cores,
            "base_freq_mhz":       self.base_freq_mhz,
            "l3_cache_mb":         self.l3_cache_mb,
            "ram_gb":              round(self.ram_gb, 1),
            "extensions": {
                "avx512":   self.has_avx512,
                "avx2":     self.has_avx2,
                "amx":      self.has_amx,
                "sse42":    self.has_sse42,
                "neon":     self.has_neon,
                "sve2":     self.has_sve2,
                "dotprod":  self.has_dotprod,
                "rvv":      self.has_rvv,
                "openvino": self.has_openvino,
            },
            "recommended_backend": self.recommended_backend,
            "recommended_quant":   self.recommended_quant,
            "optimal_threads":     self.optimal_threads,
            "notes":               self.notes,
        }


def detect_cpu() -> CPUProfile:
    """Detect CPU capabilities and return a CPUProfile with backend recommendations."""
    info     = cpuinfo.get_cpu_info()
    mem      = psutil.virtual_memory()
    arch     = platform.machine().lower()
    flags    = set(info.get("flags", []))

    profile = CPUProfile(
        vendor        = info.get("vendor_id_raw", "Unknown"),
        brand         = info.get("brand_raw", "Unknown CPU"),
        arch          = arch,
        physical_cores= psutil.cpu_count(logical=False) or 1,
        logical_cores = psutil.cpu_count(logical=True)  or 1,
        base_freq_mhz = info.get("hz_advertised_friendly", None),
        l3_cache_mb   = _parse_cache(info.get("l3_cache_size", None)),
        ram_gb        = mem.total / (1024 ** 3),
    )

    # ── x86_64 feature detection ──────────────────────────────────────────────
    if "x86" in arch:
        profile.has_avx512  = "avx512f" in flags
        profile.has_avx2    = "avx2" in flags
        profile.has_amx     = "amx_bf16" in flags or "amx_int8" in flags
        profile.has_sse42   = "sse4_2" in flags

        # Try reading /proc/cpuinfo for AMX (not always in py-cpuinfo)
        try:
            with open("/proc/cpuinfo") as f:
                cpuinfo_text = f.read()
            if "amx_bf16" in cpuinfo_text or "amx_int8" in cpuinfo_text:
                profile.has_amx = True
        except Exception:
            pass

    # ── ARM feature detection ─────────────────────────────────────────────────
    elif "aarch64" in arch or "arm" in arch:
        profile.has_neon    = "asimd" in flags or "neon" in flags
        profile.has_sve2    = "sve2" in flags
        profile.has_dotprod = "asimddp" in flags

    # ── RISC-V ───────────────────────────────────────────────────────────────
    elif "riscv" in arch:
        try:
            lscpu = subprocess.check_output(["lscpu"], text=True)
            profile.has_rvv = "V" in lscpu
        except Exception:
            pass

    # ── OpenVINO EP availability ──────────────────────────────────────────────
    try:
        import onnxruntime as ort
        profile.has_openvino = "OpenVINOExecutionProvider" in ort.get_available_providers()
    except Exception:
        pass

    # ── Backend & quantization recommendations ────────────────────────────────
    _apply_recommendations(profile)

    return profile


def _parse_cache(val) -> Optional[float]:
    if val is None:
        return None
    try:
        if isinstance(val, (int, float)):
            return val / (1024 * 1024)
        s = str(val).lower().replace(" ", "")
        if "mb" in s:
            return float(s.replace("mb", ""))
        if "kb" in s:
            return float(s.replace("kb", "")) / 1024
        return float(s) / (1024 * 1024)
    except Exception:
        return None


def _apply_recommendations(p: CPUProfile) -> None:
    """Fill in recommended_backend, recommended_quant, optimal_threads, notes."""

    # Thread count: leave 1 core for OS, use P-cores if available
    p.optimal_threads = max(1, p.physical_cores - 1)

    # Backend selection
    if p.has_amx:
        p.recommended_backend = "llamacpp"
        p.recommended_quant   = "int4"
        p.notes.append("AMX detected — llama.cpp with INT4 GGUF delivers peak throughput on this CPU.")
    elif p.has_avx512:
        p.recommended_backend = "llamacpp"
        p.recommended_quant   = "int4"
        p.notes.append("AVX-512 detected — llama.cpp INT4 is optimal.")
    elif p.has_avx2:
        p.recommended_backend = "llamacpp"
        p.recommended_quant   = "int8"
        p.notes.append("AVX2 detected — llama.cpp INT8 recommended for accuracy/speed balance.")
    elif p.has_sve2:
        p.recommended_backend = "llamacpp"
        p.recommended_quant   = "int4"
        p.notes.append("ARM SVE2 detected — llama.cpp INT4 GGUF is optimal.")
    elif p.has_neon:
        p.recommended_backend = "llamacpp"
        p.recommended_quant   = "int8"
        p.notes.append("ARM NEON detected — llama.cpp INT8 recommended.")
    elif p.has_openvino:
        p.recommended_backend = "onnx_openvino"
        p.recommended_quant   = "int8"
        p.notes.append("OpenVINO EP available — ONNX Runtime with OpenVINO backend selected.")
    else:
        p.recommended_backend = "onnx"
        p.recommended_quant   = "int8"
        p.notes.append("Baseline CPU — ONNX Runtime INT8 with CPU EP.")

    # RAM warnings
    if p.ram_gb < 8:
        p.notes.append("WARNING: < 8 GB RAM detected. Limit model size to 3B parameters or fewer.")
    elif p.ram_gb < 16:
        p.notes.append("16 GB RAM — suitable for models up to 7B parameters at INT4.")
    else:
        p.notes.append(f"{p.ram_gb:.0f} GB RAM — can serve 13B+ parameter models at INT4.")


# Singleton — detect once at startup
_cpu_profile: Optional[CPUProfile] = None


def get_cpu_profile() -> CPUProfile:
    global _cpu_profile
    if _cpu_profile is None:
        _cpu_profile = detect_cpu()
    return _cpu_profile
